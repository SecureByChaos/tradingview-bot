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
from app.ai.json_utils import extract_json_object
from app.ai.repository import get_settings
from app.database import SessionLocal
from app.db_models import AISettings, IndexConfig, IndexPriceTick, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import Signal
from app.option_finder import OptionFinder
from app.platform import list_index_configs, log_event, record_index_tick_if_stale
from app.smartapi_client import SmartAPIClient
from app.time_utils import format_ist, to_ist, utc_now

logger = logging.getLogger(__name__)

_LOOKBACK_MINUTES = 45
_MIN_TICKS_REQUIRED = 3
# Higher than the 0.3 floor used for alternative-call trades: those are
# adjusting a setup TradingView/the AI already flagged, this is fabricating a
# brand-new position from momentum data alone, with nothing else to anchor
# it -- a materially bigger claim, so it should clear a materially higher bar.
_MIN_CONFIDENCE_TO_ACT = 0.55
# Applies to every index (Bank Nifty, Nifty, and Sensex whenever it's added) --
# the first 15 minutes after the 9:15 open are the noisiest, whippiest part of
# the session. Origination keeps recording price ticks during this window so
# there's already real momentum history by the time trading is allowed, it
# just doesn't call the AI or act on anything until the window closes.
_TRADING_START_HOUR = 9
_TRADING_START_MINUTE = 30


def _still_observing(now_ist) -> bool:
    return (now_ist.hour, now_ist.minute) < (_TRADING_START_HOUR, _TRADING_START_MINUTE)

SYSTEM_PROMPT = (
    "You are an options entry-timing assistant running an independent, "
    "paper-trading-only experiment. You are given recent spot-price history for "
    "one index, and when available, today's real exchange-reported session "
    "open/high/low -- but no volume, no options chain data, no technical "
    "indicators, no support/resistance, nothing else. This is deliberately "
    "limited data. Decide whether there is a genuinely clear momentum case for "
    "opening a fresh CE (bullish) or PE (bearish) position right now, or "
    "whether the data is too thin/ambiguous to justify one -- in which case "
    "choose NONE. Do not invent data you were not given, and do not feel "
    "pressured to pick a side; NONE is the correct answer most of the time. "
    "sl_percent and target_percent are PERCENTAGE POINTS on the option premium, "
    "e.g. 10 means a 10% stop-loss, NOT a 0-1 fraction -- unlike confidence, "
    "which IS 0-1. A typical sl_percent is 8-15 and target_percent is 15-30; "
    "keep both between 5 and 50 -- options premiums move several percent on "
    "ordinary noise, so anything below 5 will just close instantly on nothing, "
    "and anything above 50 is barely a risk control at all. If you can't "
    "propose a sane value in that range, this trade will automatically fall "
    "back to trailing-stop management instead of your fixed band. "
    "Respond with a single valid JSON object only, no markdown, code fences, or "
    "extra text: {\"decision\": \"BUY_CE\"|\"BUY_PE\"|\"NONE\", \"confidence\": 0-1, "
    "\"sl_percent\": number, \"target_percent\": number, \"reasoning\": \"one or two sentences\"}."
)

# Defensive bounds independent of the prompt wording above -- LLMs aren't 100%
# reliable at following stated scales/units, and a too-tight SL/target band on
# a naturally noisy option premium means the position closes almost instantly
# on ordinary noise, not on the AI actually being wrong (too-wide is the
# opposite failure -- an unreasonably large band that's barely a risk control
# at all, often a sign the model misread the units). AI Origin trades run
# entirely on the AI's own risk judgment where it gives a sane one -- when it
# doesn't (missing, or outside these bounds), the trade still opens, but on
# trailing-stop methodology instead of a fixed number we picked ourselves.
_MIN_SL_TARGET_PERCENT = 5.0
_MAX_SL_TARGET_PERCENT = 50.0
# Trailing fallback's own parameters -- these aren't "correcting" the AI's
# entry/exit judgment, they're the same trailing-engine knobs every other
# trailing-mode strategy in this app already uses (StrategyConfig.trailing_*),
# applied here because AI Origin trades have no StrategyConfig row of their
# own to source them from.
_TRAILING_INITIAL_SL_PERCENT = 10.0
_TRAILING_FALLBACK_TARGET_PERCENT = 20.0
# Flat pause after ANY AI Origination close (win or loss) before that same
# index can originate again -- matches the 30-min post-loss cooldown real
# SIGNAL trades already get, applied here regardless of result since the
# problem is reopen velocity, not just losing streaks.
_REOPEN_COOLDOWN_MINUTES = 30


@dataclass(frozen=True)
class _ProviderView:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int


def _build_user_prompt(
    index: IndexConfig, current_price: float, ticks: list[IndexPriceTick], day_ohlc: dict[str, float] | None = None
) -> str:
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
    if day_ohlc is not None:
        # Exchange-reported full-session range, independent of our own
        # tick-sampling gaps -- gives real context for where "current price"
        # sits within today's actual range, not just within our lookback
        # window. Not always available (see SmartAPIClient.get_index_ohlc);
        # omitted entirely rather than guessed at when missing.
        lines.append(
            f"Today's session range so far: open {day_ohlc['open']}, high {day_ohlc['high']}, "
            f"low {day_ohlc['low']}, previous close {day_ohlc['close']}"
        )
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
        data = json.loads(extract_json_object(text)) if isinstance(text, str) else text
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


def _build_provider_order(settings: AISettings, cycle_toggle: int) -> list[tuple[str, str, _ProviderView]]:
    """Builds the (label, provider_name, view) attempt order for this cycle.
    cycle_toggle alternates 0/1 every 5 minutes (see caller) -- when it's 1 and
    both providers are actually configured, secondary attempts first instead
    of primary, so first-mover advantage doesn't structurally favor whichever
    provider happens to sit in the "primary" slot in AI Settings."""
    order: list[tuple[str, str, _ProviderView]] = [(
        "primary",
        settings.provider,
        _ProviderView(
            provider=settings.provider,
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
        ),
    )]
    if (
        settings.secondary_enabled
        and settings.secondary_provider
        and settings.secondary_provider.strip().lower() != settings.provider.strip().lower()
    ):
        order.append((
            "secondary",
            settings.secondary_provider,
            _ProviderView(
                provider=settings.secondary_provider,
                model=settings.secondary_model,
                api_key=settings.secondary_api_key,
                base_url=settings.secondary_base_url,
                timeout_seconds=settings.timeout_seconds,
            ),
        ))
    if cycle_toggle == 1 and len(order) == 2:
        order = [order[1], order[0]]
    return order


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


def _in_reopen_cooldown(db: Session, index_symbol: str) -> bool:
    """Without this, an index sits idle only while a trade is OPEN -- the moment
    one closes (often within minutes, win or loss), the very next 5-min
    scheduler tick is free to open another. On a fast-moving index that
    produces a reopen-immediately-after-close loop all session (e.g. 16
    Bank Nifty originations in one day). This adds a flat post-close pause,
    independent of win/loss, before the same index can originate again."""
    cutoff = utc_now() - timedelta(minutes=_REOPEN_COOLDOWN_MINUTES)
    return (
        db.scalar(
            select(StrategyTrade.id).where(
                StrategyTrade.index_symbol == index_symbol,
                StrategyTrade.origin.like("AI_ORIGIN_%"),
                StrategyTrade.status == TradeStatus.CLOSED,
                StrategyTrade.exit_time.is_not(None),
                StrategyTrade.exit_time >= cutoff,
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
    def _is_sane(value: float | None) -> bool:
        return value is not None and _MIN_SL_TARGET_PERCENT <= value <= _MAX_SL_TARGET_PERCENT

    # AI Origin trades run entirely on the AI's own risk judgment where it
    # gives a sane one. When it doesn't -- missing, too tight (closes on pure
    # noise), or too wide (barely a risk control) -- we don't substitute a
    # fixed number of our own and pretend it's still the AI's call. Instead
    # the trade uses trailing-stop methodology, the same mechanism every other
    # trailing strategy in this app already relies on.
    use_trailing = not (_is_sane(decision.sl_percent) and _is_sane(decision.target_percent))
    if use_trailing:
        logger.info(
            "[AI][ORIGIN] %s sl_percent=%s target_percent=%s outside %.0f-%.0f%% sane range -- "
            "opening on trailing-stop methodology instead of a fixed AI-proposed band",
            index.symbol, decision.sl_percent, decision.target_percent, _MIN_SL_TARGET_PERCENT, _MAX_SL_TARGET_PERCENT,
        )

    option_type = "CE" if decision.action == "BUY_CE" else "PE"
    signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE
    try:
        contract = option_finder.find_atm_contract(signal, index, 0)
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[AI][ORIGIN] Skipped: could not resolve contract/price for %s (%s)", index.symbol, exc)
        return None

    sl_percent = _TRAILING_INITIAL_SL_PERCENT if use_trailing else decision.sl_percent
    target_percent = _TRAILING_FALLBACK_TARGET_PERCENT if use_trailing else decision.target_percent
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
        sl_mode=SLMode.TRAILING if use_trailing else SLMode.FIXED,
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

        # Which provider gets the first attempt each index's single trade slot
        # flips every 5-min cycle -- without this, primary always went first and
        # always got first crack at every index, so it structurally accumulated
        # more trades than secondary regardless of which model actually judges
        # setups better. This doesn't reduce how often trades happen (that was
        # an explicit ask) -- it only changes which provider gets first refusal
        # on a given cycle, so both get a fair share of first attempts over a
        # full day instead of one always winning by default.
        provider_order = _build_provider_order(settings, cycle_toggle=int(utc_now().timestamp() // 300) % 2)

        for index in list_index_configs(session):
            if not index.enabled:
                continue
            try:
                if _has_open_origination(session, index.symbol):
                    continue
                # Cooldown disabled for now, on purpose -- observing raw
                # AI Origination trade volume with no throttle to see where the
                # daily count actually lands before deciding whether the
                # cooldown is needed. _in_reopen_cooldown/_REOPEN_COOLDOWN_MINUTES
                # are left in place; uncomment below to re-enable.
                # if _in_reopen_cooldown(session, index.symbol):
                #     logger.info(
                #         "[AI][ORIGIN] %s: in %s-min post-close cooldown, skipping",
                #         index.symbol, _REOPEN_COOLDOWN_MINUTES,
                #     )
                #     continue
                price = round(smartapi.get_index_spot(index), 2)
                record_index_tick_if_stale(session, index.symbol, price)
                if _still_observing(to_ist(utc_now())):
                    logger.info(
                        "[AI][ORIGIN] %s: still observing (market open until %02d:%02d IST), recording ticks only",
                        index.symbol, _TRADING_START_HOUR, _TRADING_START_MINUTE,
                    )
                    continue
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

                # Best-effort real day OHLC on top of our own tick sampling --
                # never blocks the check if it fails or comes back unusable.
                try:
                    day_ohlc = smartapi.get_index_ohlc(index)
                except Exception as exc:
                    logger.info("[AI][ORIGIN] %s: get_index_ohlc failed (%s)", index.symbol, exc)
                    day_ohlc = None
                user_prompt = _build_user_prompt(index, price, ticks, day_ohlc)
                for turn, provider_name, view in provider_order:
                    # Whichever provider goes first this cycle can fill the
                    # index's one trade slot; if it does, the other one is
                    # skipped for this index this cycle, same as before -- the
                    # only change is who gets first refusal isn't fixed.
                    if _has_open_origination(session, index.symbol):
                        break
                    decision = _call_provider(provider_name, view, user_prompt)
                    if decision is None:
                        continue
                    logger.info("[AI][ORIGIN] %s -> %s (%s, %s)", index.symbol, decision.action, provider_name, turn)
                    if decision.action in ("BUY_CE", "BUY_PE") and (decision.confidence or 0) >= _MIN_CONFIDENCE_TO_ACT:
                        _open_paper_trade(session, index, provider_name, decision, smartapi, option_finder)
            except Exception as exc:
                logger.exception("[AI][ORIGIN] Check failed for index %s", index.symbol)
                # Previously silent beyond the server log file (not reachable from the
                # UI) -- this made it impossible to tell "AI never wants to trade this
                # index" apart from "this index is silently broken" (e.g. a bad spot
                # token) without SSH access. Surface it on the activity log instead.
                try:
                    log_event(
                        session,
                        "AI_ORIGIN",
                        f"[{index.display_name or index.symbol}] Origination check failed: {exc}",
                        level="WARNING",
                    )
                except Exception:
                    logger.exception("[AI][ORIGIN] Also failed to log the above failure for %s", index.symbol)
    except Exception:
        logger.exception("[AI][ORIGIN] run_origination_checks failed")
    finally:
        if owns_session:
            session.close()
