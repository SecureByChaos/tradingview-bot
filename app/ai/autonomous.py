"""Autonomous AI -- a fourth AI subsystem, deliberately independent of the
other three (see CLAUDE.md's "Three AI subsystems, deliberately independent"
section), built for one explicit request: "AI will take its own decision
regardless of any adx or signal. Just it take trades and exit on its own."

WHAT MAKES THIS DIFFERENT FROM AI ORIGINATION
------------------------------------------------
AI Origination (app.ai.originator) already opens trades entirely on its own
judgment, with no TradingView signal involved -- so most of "regardless of
any signal" already describes it. Two things genuinely don't:

1. AI Origination's entry prompt is built from a rich technical-indicator
   context (regime, ADX, ATR, Supertrend, EMA stack, CPR, setups -- see
   app.market_context.build_market_context). This module's entry prompt
   deliberately gives the model NONE of that -- only the same current
   price / today's range a human glancing at the dashboard's price ticker
   would see (app.platform.get_index_live_figures, already-tested,
   already-cheap infrastructure -- prefers the free WebSocket feed, reused
   here rather than adding a second candle-fetch cost alongside AI
   Origination's own).
2. AI Origination's exit is mechanical: the model proposes a stop/target
   percentage once at entry, and monitor_open_trades closes the trade when
   either is hit -- it is never asked again. This module re-asks the model,
   every cycle, whether to HOLD or EXIT an open position, and an EXIT answer
   actually closes the trade. This is real, ongoing LLM cost per open
   position per cycle that AI Origination does not have -- flagged directly
   to the user before this was built, who chose it anyway over the cheaper
   fixed-stop/target alternative.

A KNOWN, NAMED RISK -- NOT SWEPT UNDER THE RUG
--------------------------------------------------
This project's own 30 Jul 2026 two-year backtest ("AI Origination's entry
signal does not work", CLAUDE.md) found ZERO positive directional edge in a
45-minute price-drift rule, on either index, at any horizon, in any drift
band -- six bands were reliably NEGATIVE. That finding is about a mechanical
rule, not genuine LLM judgment with the freedom to say NONE most of the time,
so it does not mechanically transfer to what this module does -- but the
INPUT this module gives the model (recent price action, nothing else) is the
same class of signal that backtest already found does not predict direction.
This module's own real results are therefore a genuinely new, live question
this project has not already answered, not a settled one -- read them with
exactly the same "not yet enough evidence" discipline as everything else
here, and do not be surprised if they land where the 30 Jul finding would
predict.

STRUCTURALLY PAPER-ONLY
-------------------------
Same construction as app.validated_signal: mode is hardcoded to
TradingMode.PAPER, smartapi.place_market_order is never called anywhere in
this module. No live path exists to gate, not just one that's off by
default -- of every experimental strategy in this project, this is the
least validated and the most expensive to run wrong, so it gets the
strictest treatment.

SAFETY BACKSTOP -- THE MODEL'S OWN EXIT CALL IS NOT THE ONLY WAY OUT
-------------------------------------------------------------------------
"Exits on its own" describes the INTENDED path, not the ONLY path. Every
trade still gets a wide stop/target (_BACKSTOP_STOP_PERCENT /
_BACKSTOP_TARGET_PERCENT, CE/PE-rescaled the same way every other strategy's
stop is). sl_mode=SLMode.FIXED with an origin that is neither "SIGNAL" nor
"AI_ORIGIN_*" means app.multi_strategy's shared monitor_open_trades branch
already enforces this mechanically with no code change there -- confirmed by
reading that function, the same way app.validated_signal's module docstring
already confirmed it.

Square-off is two layers, not one. The INTENDED cutoff is _TRADING_END
(15:00 IST, dedicated to this strategy -- see that constant's own comment
for why it is not the shared Settings > General square-off time every other
strategy uses): check_autonomous_exits closes every open position
unconditionally at or after that time, no model call, every ~5-minute
cycle. The shared monitor_open_trades TIME_EXIT at the platform's own
square-off time (15:15 default) is a second, later fallback only -- it
would catch a position this module's own cutoff somehow missed (the
scheduler down, this job erroring out for a whole cycle), not the normal
path. These backstops are meant to almost never fire; the model's own
per-cycle EXIT call is expected to act first in the overwhelming
majority of cases. A cycle where the exit call errors or times out leaves
the position open (fails to a no-op, not a forced exit) -- the backstop is
what protects it, not a retry.

ISOLATION
----------
origin="AUTONOMOUS_AI" -- its own population, matched with ==, never counted
in any AI Origination or Validated Signal report/backtest/dashboard filter.
One open position at a time per index (same reasoning as Validated Signal --
this is a single decision-maker, not multiple independent provider slots).
Single-provider only (whichever AISettings.provider is configured) -- this
module deliberately does not add a second Claude/OpenAI dual-slot dimension
on top of an already more expensive per-cycle-exit design; a real scope
decision, not an oversight.

WHERE THIS RUNS
-----------------
Its own scheduler job (app.scheduler's "autonomous-ai-check", same 5-minute
cron/market-hours-gate shape as "ai-origination-check"), NOT hooked into
run_origination_checks -- this needed its own price-context source
(get_index_live_figures, not market_context) and its own per-open-trade exit
loop, different enough in shape from originator.py's cycle that piggybacking
would have meant threading a second, unrelated concern through an already
long function. A separate job is one more scheduler entry, not a larger
change to AI Origination's own cycle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.client import AIClient
from app.ai.json_utils import extract_json_object
from app.ai.repository import get_settings
from app.database import SessionLocal
from app.db_models import AISettings, IndexConfig, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, Signal
from app.option_finder import OptionFinder
from app.platform import get_index_live_figures, get_or_create_settings, list_index_configs, log_event
from app.premium_model import days_to_expiry, symmetric_premium_percent
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIClient
from app.time_utils import parse_hhmm, to_ist, utc_now

logger = logging.getLogger(__name__)

ORIGIN = "AUTONOMOUS_AI"

# Same reasoning as _MIN_DTE_TO_TRADE everywhere else in this project: a
# fixed-percentage stop is a wider, more noise-resistant index distance at
# longer DTE. Contract survivability, not a trading signal.
_MIN_DTE_TO_TRADE = 5

# Wide on purpose -- these are a safety net, not the intended exit mechanism.
# See module docstring's "SAFETY BACKSTOP" section. Not backtested; a
# reasoned starting point, same status every new threshold in this project
# carries before real data looks at it.
_BACKSTOP_STOP_PERCENT_NOMINAL = 35.0
_BACKSTOP_TARGET_PERCENT_NOMINAL = 50.0

_DEFAULT_TRADING_START = (9, 45)

# 31 Aug 2026: a dedicated, EARLIER cutoff for this strategy specifically --
# "should not take trades after 3pm and square off at 3pm only". Deliberately
# NOT the shared Settings > General square-off time (PlatformSettings.
# square_off_time, 15:15 default) every other strategy in this app reads --
# that setting is shared by the rule-based strategies, AI Origination, and
# Validated Signal, and changing it would have moved their exit/square-off
# time too. A plain module constant rather than a new admin-configurable
# field, since only Autonomous AI needs it and this keeps the change scoped
# to this module alone. Used both to block new entries at/after 15:00 and,
# in check_autonomous_exits, to unconditionally square off every open
# AUTONOMOUS_AI position at that time -- a hard cutoff the model's own HOLD
# answer cannot override.
_TRADING_END = (15, 0)

SYSTEM_PROMPT_ENTRY = (
    "You are an autonomous options trading assistant running an independent, "
    "paper-trading-only experiment. You are given ONLY the current price and "
    "today's trading range for one index -- no technical indicators (no ADX, "
    "EMA, RSI, Supertrend, pivot levels), no trend or regime classification, "
    "and no external trading signal of any kind. Decide for yourself, using "
    "whatever judgment you have, whether to open a fresh CE (bullish) or PE "
    "(bearish) position right now, or do nothing. You will be asked again "
    "every cycle whether to hold or exit any position you open, and that "
    "later decision is the one that actually manages the trade -- a wide "
    "safety-net stop/target exists in the system in case you are never asked "
    "again for some reason, but it is not the exit mechanism you should rely "
    "on. NONE is a completely acceptable answer most of the time -- only "
    "open a position when you have a genuine, articulable reason from the "
    "numbers given. Respond with a single valid JSON object only, no "
    "markdown, code fences, or extra text: "
    '{"decision": "BUY_CE"|"BUY_PE"|"NONE", "confidence": 0-1, "reasoning": "one or two sentences"}.'
)

SYSTEM_PROMPT_EXIT = (
    "You are managing an options position you opened yourself, in the same "
    "autonomous paper-trading experiment, no technical indicators and no "
    "external trading signal available now either. You are given only the "
    "position's own numbers. Decide whether to EXIT now or HOLD. This "
    "decision is real: EXIT actually closes the position immediately at the "
    "current price -- you are not being asked for an opinion. Respond with a "
    "single valid JSON object only, no markdown, code fences, or extra text: "
    '{"decision": "EXIT"|"HOLD", "confidence": 0-1, "reasoning": "one or two sentences"}.'
)


@dataclass(frozen=True)
class _ProviderView:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int


@dataclass(frozen=True)
class _RawCall:
    text: str | None
    error: str | None
    latency_ms: float | None


def _call_openai(view: _ProviderView, system_prompt: str, user_prompt: str) -> _RawCall:
    if not view.api_key or not view.model:
        return _RawCall(None, "OpenAI API key/model not configured.", None)
    endpoint = (view.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    response = AIClient().send(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {view.api_key}", "Content-Type": "application/json"},
        payload={
            "model": view.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=view.timeout_seconds,
    )
    if response.error:
        return _RawCall(None, response.error, response.latency_ms)
    try:
        content = response.response_body["choices"][0]["message"]["content"]
    except Exception as exc:
        return _RawCall(
            None,
            f"Unexpected OpenAI response shape ({type(exc).__name__}: {exc})",
            response.latency_ms,
        )
    return _RawCall(content, None, response.latency_ms)


def _call_claude(view: _ProviderView, system_prompt: str, user_prompt: str) -> _RawCall:
    if not view.api_key or not view.model:
        return _RawCall(None, "Claude API key/model not configured.", None)
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
            "max_tokens": 512,
            "system": system_prompt
            + "\n\nRespond with JSON only, no markdown or code fences. Keep any internal "
            "reasoning brief -- decide directly without lengthy deliberation.",
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=view.timeout_seconds,
    )
    if response.error:
        return _RawCall(None, response.error, response.latency_ms)
    try:
        blocks = response.response_body.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    except Exception as exc:
        return _RawCall(
            None,
            f"Unexpected Claude response shape ({type(exc).__name__}: {exc})",
            response.latency_ms,
        )
    if not text:
        return _RawCall(
            None,
            f"Claude returned no text content (stop_reason={(response.response_body or {}).get('stop_reason')!r})",
            response.latency_ms,
        )
    return _RawCall(text, None, response.latency_ms)


def _call_provider(provider: str, view: _ProviderView, system_prompt: str, user_prompt: str) -> _RawCall:
    normalized = (provider or "").strip().lower()
    if normalized == "claude":
        return _call_claude(view, system_prompt, user_prompt)
    if normalized == "openai":
        return _call_openai(view, system_prompt, user_prompt)
    return _RawCall(None, f"Unrecognised provider {provider!r}", None)


def _provider_view(settings: AISettings) -> _ProviderView:
    return _ProviderView(
        provider=settings.provider,
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
    )


@dataclass(frozen=True)
class _EntryDecision:
    action: str  # BUY_CE / BUY_PE / NONE / ERROR
    confidence: float | None
    reasoning: str


@dataclass(frozen=True)
class _ExitDecision:
    action: str  # EXIT / HOLD / ERROR
    confidence: float | None
    reasoning: str


def _parse_confidence(value: object) -> float | None:
    if value is None:
        return None
    try:
        confidence = float(value)
        if confidence > 1.0:
            confidence = confidence / 100.0
        return min(1.0, max(0.0, confidence))
    except (TypeError, ValueError):
        return None


def _parse_entry_response(text: str | None) -> _EntryDecision:
    if not text:
        return _EntryDecision("ERROR", None, "No response text")
    try:
        data = json.loads(extract_json_object(text)) if isinstance(text, str) else text
        if not isinstance(data, dict):
            return _EntryDecision("ERROR", None, "Invalid AI response (not a JSON object)")
        action = str(data.get("decision") or "").strip().upper()
        if action not in {"BUY_CE", "BUY_PE", "NONE"}:
            return _EntryDecision("ERROR", None, f"Unrecognised decision value {action!r}")
        return _EntryDecision(action, _parse_confidence(data.get("confidence")), str(data.get("reasoning") or ""))
    except Exception as exc:
        return _EntryDecision("ERROR", None, f"Could not parse AI response ({type(exc).__name__}: {exc})")


def _parse_exit_response(text: str | None) -> _ExitDecision:
    if not text:
        return _ExitDecision("ERROR", None, "No response text")
    try:
        data = json.loads(extract_json_object(text)) if isinstance(text, str) else text
        if not isinstance(data, dict):
            return _ExitDecision("ERROR", None, "Invalid AI response (not a JSON object)")
        action = str(data.get("decision") or "").strip().upper()
        if action not in {"EXIT", "HOLD"}:
            return _ExitDecision("ERROR", None, f"Unrecognised decision value {action!r}")
        return _ExitDecision(action, _parse_confidence(data.get("confidence")), str(data.get("reasoning") or ""))
    except Exception as exc:
        return _ExitDecision("ERROR", None, f"Could not parse AI response ({type(exc).__name__}: {exc})")


def _build_entry_prompt(figures_row: dict, now_ist, end_hm: tuple[int, int]) -> str:
    end_minutes = end_hm[0] * 60 + end_hm[1]
    now_minutes = now_ist.hour * 60 + now_ist.minute
    minutes_to_close = max(end_minutes - now_minutes, 0)
    lines = [
        f"Index: {figures_row['display_name']}",
        f"Current price: {figures_row['price']}",
    ]
    if figures_row.get("change_abs") is not None and figures_row.get("change_percent") is not None:
        lines.append(f"Change vs previous close: {figures_row['change_abs']:+.2f} ({figures_row['change_percent']:+.2f}%)")
    if figures_row.get("day_low") is not None and figures_row.get("day_high") is not None:
        lines.append(f"Today's range so far: {figures_row['day_low']} - {figures_row['day_high']}")
    lines.append(f"Time: {now_ist.strftime('%H:%M')} IST, ~{minutes_to_close} minutes until square-off")
    return "\n".join(lines) + "\n\nDecide: BUY_CE, BUY_PE, or NONE?"


def _build_exit_prompt(trade: StrategyTrade, now_ist) -> str:
    entry_ist = to_ist(trade.entry_time)
    holding_minutes = max(int((now_ist - entry_ist).total_seconds() // 60), 0) if entry_ist is not None else None
    lines = [
        f"Index: {trade.index_symbol}",
        f"Position: {trade.option_type} ({trade.signal})",
        f"Entry premium: {trade.entry_price}",
        f"Current premium: {trade.current_premium}",
        f"Running P&L: {trade.pnl_percent}%",
        f"Holding time: {holding_minutes} minutes" if holding_minutes is not None else "Holding time: unknown",
        f"Safety-net stop / target: {trade.stoploss} / {trade.target} (informational -- you decide independently)",
    ]
    return "\n".join(lines) + "\n\nDecide: EXIT now, or HOLD?"


def _has_open_autonomous_trade(db: Session, index_symbol: str) -> bool:
    return (
        db.scalar(
            select(StrategyTrade.id)
            .where(
                StrategyTrade.index_symbol == index_symbol,
                StrategyTrade.origin == ORIGIN,
                StrategyTrade.status == TradeStatus.OPEN,
            )
            .limit(1)
        )
        is not None
    )


def open_autonomous_trade(
    db: Session,
    index: IndexConfig,
    action: str,
    reasoning: str,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[StrategyTrade]:
    option_type = "CE" if action == "BUY_CE" else "PE"
    signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE

    try:
        contract = option_finder.find_atm_contract(signal, index, 0, min_dte=_MIN_DTE_TO_TRADE)
    except Exception as exc:
        logger.info("[AUTONOMOUS_AI] %s: Skipped, could not resolve contract (%s)", index.symbol, exc)
        return None

    dte = days_to_expiry(contract.expiry, to_ist(utc_now()).date())
    if dte < _MIN_DTE_TO_TRADE:
        logger.info(
            "[AUTONOMOUS_AI] %s: no expiry at least %s DTE out (nearest %s at %s DTE) -- skipping",
            index.symbol, _MIN_DTE_TO_TRADE, contract.expiry, dte,
        )
        return None

    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[AUTONOMOUS_AI] %s: Skipped, could not resolve price (%s)", index.symbol, exc)
        return None
    if not entry_price:
        logger.info("[AUTONOMOUS_AI] %s: Skipped, LTP came back empty", index.symbol)
        return None

    stop_percent, stop_matched = symmetric_premium_percent(
        _BACKSTOP_STOP_PERCENT_NOMINAL, index.symbol, contract.option_type, dte
    )
    target_percent, _ = symmetric_premium_percent(
        _BACKSTOP_TARGET_PERCENT_NOMINAL, index.symbol, contract.option_type, dte
    )
    stoploss = round(entry_price * (1 - stop_percent / 100), 2)
    target = round(entry_price * (1 + target_percent / 100), 2)
    strategy_name = f"Autonomous AI - {index.display_name or index.symbol}"

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
        investment_amount=round(entry_price * contract.lot_size, 2),
        entry_price=round(entry_price, 2),
        current_premium=round(entry_price, 2),
        stoploss=stoploss,
        target=target,
        entry_time=utc_now(),
        # Structurally paper-only -- see module docstring. No live order path
        # exists anywhere in this module.
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        highest_price=round(entry_price, 2),
        lowest_price=round(entry_price, 2),
        trailing_active=False,
        # FIXED + this origin (not AI_ORIGIN_*) means monitor_open_trades'
        # shared branch gives this a plain stop/target/time-exit backstop
        # only -- see module docstring's "SAFETY BACKSTOP" section.
        sl_mode=SLMode.FIXED,
        calibration_bucket_matched=stop_matched,
        origin=ORIGIN,
        ai_action=action,
        ai_reasoning=reasoning,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    log_event(
        db,
        "AUTONOMOUS_AI",
        f"[{strategy_name}] Autonomous AI opened {signal.value} @ strike {trade.strike}",
        payload={"trade_id": trade.trade_id, "reasoning": reasoning},
    )
    logger.info("[AUTONOMOUS_AI] %s opened %s for %s", ORIGIN, signal.value, index.symbol)
    return trade


def check_autonomous_entry(
    db: Session,
    index: IndexConfig,
    figures_row: dict | None,
    now_ist,
    end_hm: tuple[int, int],
    settings: AISettings,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[StrategyTrade]:
    if _has_open_autonomous_trade(db, index.symbol):
        return None
    if figures_row is None or figures_row.get("price") is None:
        logger.info("[AUTONOMOUS_AI] %s: Skipped, no live price available", index.symbol)
        return None
    user_prompt = _build_entry_prompt(figures_row, now_ist, end_hm)
    raw = _call_provider(settings.provider, _provider_view(settings), SYSTEM_PROMPT_ENTRY, user_prompt)
    if raw.error:
        logger.error("[AUTONOMOUS_AI] %s entry call FAILED: %s", index.symbol, raw.error)
        log_event(db, "AUTONOMOUS_AI", f"[{index.symbol}] entry call failed: {raw.error}", level="ERROR")
        return None
    decision = _parse_entry_response(raw.text)
    if decision.action == "ERROR":
        logger.error("[AUTONOMOUS_AI] %s entry response unparseable: %s", index.symbol, decision.reasoning)
        return None
    logger.info("[AUTONOMOUS_AI] %s -> %s", index.symbol, decision.action)
    if decision.action == "NONE":
        return None
    return open_autonomous_trade(db, index, decision.action, decision.reasoning, smartapi, option_finder)


def check_autonomous_exits(db: Session, trade_manager, settings: AISettings, end_hm: tuple[int, int]) -> None:
    """Re-asks the model, for every currently open AUTONOMOUS_AI trade,
    whether to HOLD or EXIT -- and actually closes the trade on EXIT via
    trade_manager.close_trade (the same helper monitor_open_trades' own
    backstop uses), so the two exit paths never disagree about how a close
    is recorded. A call that errors leaves the trade open -- see module
    docstring's "SAFETY BACKSTOP" section for why that's the safe default,
    not a forced exit.

    At or past end_hm, every open position is squared off unconditionally
    (ExitReason.TIME_EXIT) with no model call -- this is a hard cutoff, not
    a suggestion the model can override by saying HOLD. See _TRADING_END's
    own comment for why this is a dedicated, earlier cutoff rather than the
    shared Settings > General square-off time every other strategy uses."""
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == ORIGIN,
            )
        )
    )
    now_ist = to_ist(utc_now())
    past_cutoff = (now_ist.hour, now_ist.minute) >= end_hm
    for trade in trades:
        try:
            if trade.current_premium is None:
                continue
            if past_cutoff:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.TIME_EXIT)
                log_event(
                    db, "AUTONOMOUS_AI",
                    f"[{trade.strategy_name}] squared off at {end_hm[0]:02d}:{end_hm[1]:02d} IST cutoff",
                    payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
                )
                logger.info(
                    "[AUTONOMOUS_AI] %s squared off at %02d:%02d IST cutoff (%.2f%%)",
                    trade.trade_id, end_hm[0], end_hm[1], trade.pnl_percent or 0.0,
                )
                continue
            user_prompt = _build_exit_prompt(trade, now_ist)
            raw = _call_provider(settings.provider, _provider_view(settings), SYSTEM_PROMPT_EXIT, user_prompt)
            if raw.error:
                logger.error("[AUTONOMOUS_AI] %s exit call FAILED: %s", trade.trade_id, raw.error)
                continue
            decision = _parse_exit_response(raw.text)
            if decision.action == "ERROR":
                logger.error("[AUTONOMOUS_AI] %s exit response unparseable: %s", trade.trade_id, decision.reasoning)
                continue
            log_event(
                db, "AUTONOMOUS_AI",
                f"[{trade.strategy_name}] exit check: {decision.action}",
                payload={"trade_id": trade.trade_id, "reasoning": decision.reasoning, "pnl_percent": trade.pnl_percent},
            )
            if decision.action == "EXIT":
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.AI_DISCRETION_EXIT)
                logger.info(
                    "[AUTONOMOUS_AI] %s closed at model's own discretion (%.2f%%)",
                    trade.trade_id, trade.pnl_percent or 0.0,
                )
        except Exception:
            logger.exception("[AUTONOMOUS_AI] exit check failed for trade %s", trade.trade_id)


def run_autonomous_checks(
    smartapi: Optional[SmartAPIClient] = None,
    option_finder: Optional[OptionFinder] = None,
    trade_manager=None,
    feed_store: object | None = None,
    db: Session | None = None,
) -> None:
    """Scheduler entry point (see app.scheduler's "autonomous-ai-check" job).
    Owns its own DB session when called from the scheduler; accepts an
    existing session in tests."""
    if smartapi is None or option_finder is None or trade_manager is None:
        logger.info("[AUTONOMOUS_AI] Skipped: no smartapi/option_finder/trade_manager available in this context")
        return
    closed_reason = check_market_hours(utc_now())
    if closed_reason is not None:
        logger.info("[AUTONOMOUS_AI] Cycle skipped -- %s", closed_reason.replace("Signal received ", "", 1))
        return
    owns_session = db is None
    session = db or SessionLocal()
    try:
        settings: AISettings | None = get_settings(session)
        if settings is None or not settings.enabled or settings.mode == "DISABLED":
            logger.info("[AUTONOMOUS_AI] Skipped: AI disabled")
            return
        if (settings.provider or "").strip().lower() not in {"openai", "claude"}:
            logger.info("[AUTONOMOUS_AI] Skipped: no real provider configured (provider=%s)", settings.provider)
            return

        # Exits first: closing a stale position before considering a fresh
        # entry means a freed-up index slot can be re-entered the same cycle
        # rather than waiting a full 5 minutes.
        check_autonomous_exits(session, trade_manager, settings, _TRADING_END)

        platform_settings = get_or_create_settings(session)
        start_hm = parse_hhmm(platform_settings.trading_start_time, _DEFAULT_TRADING_START)
        end_hm = _TRADING_END
        now_ist = to_ist(utc_now())
        if (now_ist.hour, now_ist.minute) < start_hm or (now_ist.hour, now_ist.minute) >= end_hm:
            return

        figures = {row["symbol"]: row for row in get_index_live_figures(session, smartapi, feed_store)}
        for index in list_index_configs(session):
            if not index.enabled:
                continue
            try:
                check_autonomous_entry(
                    session, index, figures.get(index.symbol), now_ist, end_hm, settings, smartapi, option_finder,
                )
            except Exception:
                logger.exception("[AUTONOMOUS_AI] entry check failed for %s", index.symbol)
    finally:
        if owns_session:
            session.close()
