from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import AIClient
from app.ai.repository import get_settings
from app.database import SessionLocal
from app.db_models import AISettings, IndexConfig, IndexPriceTick, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import Signal
from app.option_finder import OptionFinder
from app.platform import list_index_configs, log_event, record_index_tick_if_stale
from app.smartapi_client import SmartAPIClient
from app.time_utils import format_ist, utc_now

logger = logging.getLogger(__name__)

_LOOKBACK_MINUTES = 45
_MIN_TICKS_REQUIRED = 3
_DEFAULT_SL_PERCENT = 10.0
_DEFAULT_TARGET_PERCENT = 20.0
# Higher than the 0.3 floor used for alternative-call trades: those are
# adjusting a setup TradingView/the AI already flagged, this is fabricating a
# brand-new position from momentum data alone, with nothing else to anchor
# it -- a materially bigger claim, so it should clear a materially higher bar.
_MIN_CONFIDENCE_TO_ACT = 0.55

SYSTEM_PROMPT = (
    "You are an options entry-timing assistant running an independent, "
    "paper-trading-only experiment. You are given nothing but recent spot-price "
    "history for one index -- no volume, no options chain data, no technical "
    "indicators, no support/resistance, nothing else. This is deliberately "
    "limited data. Decide whether there is a genuinely clear momentum case for "
    "opening a fresh CE (bullish) or PE (bearish) position right now, or "
    "whether the data is too thin/ambiguous to justify one -- in which case "
    "choose NONE. Do not invent data you were not given, and do not feel "
    "pressured to pick a side; NONE is the correct answer most of the time. "
    "Respond with a single valid JSON object only, no markdown, code fences, or "
    "extra text: {\"decision\": \"BUY_CE\"|\"BUY_PE\"|\"NONE\", \"confidence\": 0-1, "
    "\"sl_percent\": number, \"target_percent\": number, \"reasoning\": \"one or two sentences\"}."
)


@dataclass(frozen=True)
class _ProviderView:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int


def _build_user_prompt(index: IndexConfig, current_price: float, ticks: list[IndexPriceTick]) -> str:
    prices = [tick.price for tick in ticks] + [current_price]
    earliest = prices[0]
    change_percent = round(((current_price - earliest) / earliest) * 100, 3) if earliest else 0.0
    up_moves = sum(1 for a, b in zip(prices, prices[1:]) if b > a)
    down_moves = sum(1 for a, b in zip(prices, prices[1:]) if b < a)
    lines = [
        f"Index: {index.display_name or index.symbol}",
        f"Lookback window: last {_LOOKBACK_MINUTES} minutes, {len(prices)} price samples",
        f"Earliest price in window: {earliest}",
        f"Current price: {current_price}",
        f"Change over window: {change_percent}%",
        f"Window high: {round(max(prices), 2)}",
        f"Window low: {round(min(prices), 2)}",
        f"Up moves: {up_moves}, Down moves: {down_moves} (sample-to-sample)",
    ]
    return "\n".join(lines) + "\n\nDecide: BUY_CE, BUY_PE, or NONE?"


@dataclass(frozen=True)
class _Decision:
    action: str
    confidence: float | None
    sl_percent: float | None
    target_percent: float | None
    reasoning: str


def _parse_response(text: str) -> _Decision:
    try:
        data = json.loads(text) if isinstance(text, str) else text
        if not isinstance(data, dict):
            return _Decision("ERROR", None, None, None, "Invalid AI response (not a JSON object).")
        decision = str(data.get("decision") or "").strip().upper()
        if decision not in {"BUY_CE", "BUY_PE", "NONE"}:
            decision = "ERROR"
        confidence = data.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
                if confidence > 1.0:
                    confidence = confidence / 100.0
                confidence = min(1.0, max(0.0, confidence))
            except (TypeError, ValueError):
                confidence = None

        def _percent(value: object) -> float | None:
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return _Decision(
            decision,
            confidence,
            _percent(data.get("sl_percent")),
            _percent(data.get("target_percent")),
            str(data.get("reasoning") or ""),
        )
    except Exception:
        return _Decision("ERROR", None, None, None, "Invalid AI response.")


def _call_openai(view: _ProviderView, user_prompt: str) -> _Decision:
    if not view.api_key or not view.model:
        return _Decision("ERROR", None, None, None, "OpenAI API key/model not configured.")
    endpoint = (view.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    response = AIClient().send(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {view.api_key}", "Content-Type": "application/json"},
        payload={
            "model": view.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=view.timeout_seconds,
    )
    if response.error:
        return _Decision("ERROR", None, None, None, response.error)
    try:
        content = response.response_body["choices"][0]["message"]["content"]
    except Exception:
        return _Decision("ERROR", None, None, None, "Unexpected OpenAI response shape.")
    return _parse_response(content)


def _call_claude(view: _ProviderView, user_prompt: str) -> _Decision:
    if not view.api_key or not view.model:
        return _Decision("ERROR", None, None, None, "Claude API key/model not configured.")
    endpoint = (view.base_url or "https://api.anthropic.com/v1").rstrip("/") + "/messages"
    response = AIClient().send(
        endpoint=endpoint,
        headers={
            "x-api-key": view.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload={
            "model": view.model,
            "max_tokens": 256,
            "system": SYSTEM_PROMPT + "\n\nRespond with JSON only, no markdown or code fences.",
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=view.timeout_seconds,
    )
    if response.error:
        return _Decision("ERROR", None, None, None, response.error)
    try:
        blocks = response.response_body.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
    except Exception:
        return _Decision("ERROR", None, None, None, "Unexpected Claude response shape.")
    return _parse_response(text)


def _call_provider(provider: str, view: _ProviderView, user_prompt: str) -> Optional[_Decision]:
    normalized = (provider or "").strip().lower()
    if normalized == "claude":
        return _call_claude(view, user_prompt)
    if normalized == "openai":
        return _call_openai(view, user_prompt)
    return None


def _has_open_origination(db: Session, index_symbol: str) -> bool:
    return (
        db.scalar(
            select(StrategyTrade.id).where(
                StrategyTrade.index_symbol == index_symbol,
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin.like("AI_ORIGIN_%"),
            ).limit(1)
        )
        is not None
    )


def _open_paper_trade(
    db: Session,
    index: IndexConfig,
    provider: str,
    decision: _Decision,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[StrategyTrade]:
    option_type = "CE" if decision.action == "BUY_CE" else "PE"
    signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE
    try:
        contract = option_finder.find_atm_contract(signal, index, 0)
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[AI][ORIGIN] Skipped: could not resolve contract/price for %s (%s)", index.symbol, exc)
        return None

    sl_percent = decision.sl_percent if decision.sl_percent and decision.sl_percent > 0 else _DEFAULT_SL_PERCENT
    target_percent = decision.target_percent if decision.target_percent and decision.target_percent > 0 else _DEFAULT_TARGET_PERCENT
    stoploss = round(entry_price * (1 - sl_percent / 100), 2)
    target = round(entry_price * (1 + target_percent / 100), 2)
    origin = f"AI_ORIGIN_{provider.strip().upper()}"
    strategy_name = f"AI Origination - {index.display_name or index.symbol}"

    trade = StrategyTrade(
        trade_id=uuid4().hex,
        strategy_name=strategy_name,
        signal=signal.value,
        index_symbol=index.symbol,
        exchange=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        symboltoken=contract.symboltoken,
        strike=contract.strike,
        expiry=contract.expiry,
        option_type=contract.option_type,
        quantity=contract.lot_size,
        entry_price=round(entry_price, 2),
        current_premium=round(entry_price, 2),
        stoploss=stoploss,
        target=target,
        entry_time=utc_now(),
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        entry_order_id=None,
        highest_price=round(entry_price, 2),
        lowest_price=round(entry_price, 2),
        trailing_active=False,
        origin=origin,
        ai_action=decision.action,
        ai_confidence=decision.confidence,
        ai_reasoning=decision.reasoning,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    log_event(
        db,
        "AI_ORIGIN",
        f"[{strategy_name}] {origin} originated: {signal.value}",
        payload={
            "trade_id": trade.trade_id,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "entry_time_ist": format_ist(trade.entry_time),
        },
    )
    logger.info(
        "[AI][ORIGIN] %s opened %s for %s (confidence=%.2f)",
        origin, signal.value, index.symbol, decision.confidence or 0.0,
    )
    return trade


def run_origination_checks(
    smartapi: Optional[SmartAPIClient] = None,
    option_finder: Optional[OptionFinder] = None,
    db: Session | None = None,
) -> None:
    """Independent, paper-only AI entry-origination check for the AI Origination
    page. Never touches real risk/state/stats/telegram -- every resulting trade
    carries an AI_ORIGIN_* origin, the same isolation convention used by
    app/ai/alternative_trader.py and app/ai/exit_shadow.py. Owns its own DB
    session when called from the scheduler; smartapi/option_finder must be
    supplied there since this module has no app-context access of its own."""
    if smartapi is None or option_finder is None:
        logger.info("[AI][ORIGIN] Skipped: no smartapi/option_finder available in this context")
        return
    owns_session = db is None
    session = db or SessionLocal()
    try:
        settings: AISettings | None = get_settings(session)
        if settings is None or not settings.enabled or settings.mode == "DISABLED":
            logger.info("[AI][ORIGIN] Skipped: AI disabled")
            return
        if (settings.provider or "").strip().lower() not in {"openai", "claude"}:
            logger.info("[AI][ORIGIN] Skipped: no real provider configured (provider=%s)", settings.provider)
            return

        for index in list_index_configs(session):
            if not index.enabled:
                continue
            try:
                if _has_open_origination(session, index.symbol):
                    continue
                price = round(smartapi.get_index_spot(index), 2)
                record_index_tick_if_stale(session, index.symbol, price)
                cutoff = utc_now() - timedelta(minutes=_LOOKBACK_MINUTES)
                ticks = list(
                    session.scalars(
                        select(IndexPriceTick)
                        .where(IndexPriceTick.index_symbol == index.symbol, IndexPriceTick.recorded_at >= cutoff)
                        .order_by(IndexPriceTick.recorded_at)
                    )
                )
                if len(ticks) < _MIN_TICKS_REQUIRED:
                    logger.info("[AI][ORIGIN] %s: not enough tick history yet (%s samples)", index.symbol, len(ticks))
                    continue

                user_prompt = _build_user_prompt(index, price, ticks)
                primary_view = _ProviderView(
                    provider=settings.provider,
                    model=settings.model,
                    api_key=settings.api_key,
                    base_url=settings.base_url,
                    timeout_seconds=settings.timeout_seconds,
                )
                decision = _call_provider(settings.provider, primary_view, user_prompt)
                if decision is not None:
                    logger.info("[AI][ORIGIN] %s -> %s (%s)", index.symbol, decision.action, settings.provider)
                    if decision.action in ("BUY_CE", "BUY_PE") and (decision.confidence or 0) >= _MIN_CONFIDENCE_TO_ACT:
                        _open_paper_trade(session, index, settings.provider, decision, smartapi, option_finder)

                if (
                    settings.secondary_enabled
                    and settings.secondary_provider
                    and settings.secondary_provider.strip().lower() != settings.provider.strip().lower()
                    and not _has_open_origination(session, index.symbol)
                ):
                    secondary_view = _ProviderView(
                        provider=settings.secondary_provider,
                        model=settings.secondary_model,
                        api_key=settings.secondary_api_key,
                        base_url=settings.secondary_base_url,
                        timeout_seconds=settings.timeout_seconds,
                    )
                    secondary_decision = _call_provider(settings.secondary_provider, secondary_view, user_prompt)
                    if secondary_decision is not None:
                        logger.info(
                            "[AI][ORIGIN] %s -> %s (secondary %s)",
                            index.symbol, secondary_decision.action, settings.secondary_provider,
                        )
                        if (
                            secondary_decision.action in ("BUY_CE", "BUY_PE")
                            and (secondary_decision.confidence or 0) >= _MIN_CONFIDENCE_TO_ACT
                        ):
                            _open_paper_trade(session, index, settings.secondary_provider, secondary_decision, smartapi, option_finder)
            except Exception:
                logger.exception("[AI][ORIGIN] Check failed for index %s", index.symbol)
    except Exception:
        logger.exception("[AI][ORIGIN] run_origination_checks failed")
    finally:
        if owns_session:
            session.close()
