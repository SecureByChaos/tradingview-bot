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
from app.db_models import AISettings, IndexConfig, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_context import ADX_NO_TREND, ADX_TRENDING, CPR, MarketContext, build_market_context
from app.market_data import (
    FIFTEEN_MINUTE,
    FIVE_MINUTE,
    ONE_MINUTE,
    load_bars,
    parse_smartapi_row,
    resample,
    store_bars,
)
from app.models import Signal
from app.option_finder import OptionFinder
from app.premium_model import days_to_expiry, to_risk_units
from app.platform import list_index_configs, log_event, record_index_tick_if_stale
from app.smartapi_client import SmartAPIClient
from app.time_utils import format_ist, to_ist, utc_now

logger = logging.getLogger(__name__)

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
# Moved from 09:30 to 09:45 in Phase 1. The opening range is 09:15-09:45, so
# entries starting at 09:30 left a 15-minute window in which every
# opening-range-derived setup was undefined -- precisely when breakout logic
# matters most. Waiting until the range has actually closed means every entry
# is considered against complete structural context. It also cuts trade count
# slightly, which is the direction the cost arithmetic wants anyway.
_TRADING_START_MINUTE = 45
# Mirrors the 15:15 end-of-day square-off every other trade in this app is
# already subject to (see monitor_open_trades in multi_strategy.py) -- without
# this, origination had a start gate but no end gate, so it kept opening brand
# new trades all evening (observed well past 9 PM IST). Each one got caught by
# that same 15:15 TIME_EXIT check on the very next 30s monitor cycle and
# closed instantly at breakeven, since that check re-evaluates every open
# trade's age against wall-clock time on every cycle, not just once at 15:15.
_TRADING_END_HOUR = 15
_TRADING_END_MINUTE = 15


def _still_observing(now_ist) -> bool:
    return (now_ist.hour, now_ist.minute) < (_TRADING_START_HOUR, _TRADING_START_MINUTE)


def _past_trading_end(now_ist) -> bool:
    return (now_ist.hour, now_ist.minute) >= (_TRADING_END_HOUR, _TRADING_END_MINUTE)

SYSTEM_PROMPT = (
    "You are an options entry-timing assistant running an independent, "
    "paper-trading-only experiment. You are given today's market structure and "
    "technical context for one index: regime measures (trend strength via ADX, "
    "volatility via ATR, and the prior session's pivot-range classification), "
    "key price levels (today's opening range, the previous session's "
    "high/low/close, and today's range so far), trend indicators (moving "
    "averages, Supertrend on two timeframes, RSI), how far price has extended "
    "from its short-term mean, and price drift over several lookback windows. "
    "This is your complete picture of the current setup -- you are not given "
    "an options chain, open interest, PCR, India VIX, news, or a record of "
    "your own past trades. Decide whether there is a genuinely clear momentum "
    "case for opening a fresh CE (bullish) or PE (bearish) position right now, "
    "or whether the data is too thin/ambiguous to justify one -- in which case "
    "choose NONE. Do not invent data you were not given, and do not feel "
    "pressured to pick a side; NONE is the correct answer most of the time.\n\n"
    "Being at the top or bottom of a range is not by itself directional "
    "evidence. It is equally consistent with continuation and with exhaustion. "
    "A breakout means a completed bar has closed beyond a pre-defined level -- "
    "the opening range or the previous day's high/low -- and held there. Price "
    "merely sitting near the highest point of a recent window is not a "
    "breakout; over any rising window that is true by construction.\n\n"
    "Weigh ADX before acting on trend. Below 20 there is no established trend "
    "to continue, and extremes are more likely to reverse than extend. Between "
    "20 and 25 a trend is developing. Above 25 continuation is better "
    "supported.\n\n"
    "On a wide-CPR day, expect range-bound conditions and treat breakout "
    "signals with particular scepticism. On a narrow-CPR day, trending "
    "conditions are more likely.\n\n"
    "Treat large extension from EMA21 in ATR terms as a caution rather than "
    "confirmation, especially when ADX is weak -- a fast move that has already "
    "travelled several ATR is more likely to be spent than to continue.\n\n"
    "NONE remains the correct answer most of the time. "
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
# Week 2 roadmap, Section 3. find_atm_contract always takes the nearest
# available expiry with no offset, so whatever DTE happens to be listed is
# whatever gets traded. CLAUDE.md's days-to-expiry finding: an identical 12%
# stop, breached by noise within 60 min, hits 36.5% of the time at 2-5 DTE
# versus 23.4% at 6-10 DTE on Bank Nifty calls -- the same risk band is a
# meaningfully worse bet close to expiry, independent of whether the entry
# signal is right. 5 matches the roadmap's own "~5 DTE" floor; note this
# still allows DTE=5 itself, which sits in premium_model's higher-risk "2-5"
# bucket rather than "6-10" -- raise to 6 here if the intent was to exclude
# that bucket entirely rather than approximately.
_MIN_DTE_TO_TRADE = 5
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

# Candle warm-up. ADX(14) and ATR(14) on resampled 5-minute bars need roughly
# 28 bars minimum and closer to 100 to stabilise; 5 trading days of 1-minute
# data yields ~375 five-minute bars, comfortably inside SmartAPI's ~30-day
# 1-minute limit. It also delivers previous-day high/low/close and multi-day
# range context for free, which the CPR classifier needs.
_CANDLE_WARMUP_DAYS = 7
# Bars loaded per context build. 7 days x 375 one-minute bars ~= 2600.
_CANDLE_LOAD_LIMIT = 3000


@dataclass(frozen=True)
class _ProviderView:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: int


def _position_line(
    label: str, spot: float, high: float | None, low: float | None, atr_value: float | None
) -> str | None:
    """'above/below by X pts (Y ATR)' relative to a high/low pair, or None when
    the levels aren't available. Shared by the opening-range and previous-day
    STRUCTURE lines -- both are "where does price sit relative to a bracket"
    questions with the same shape."""
    if high is None or low is None:
        return None
    if spot > high:
        distance = spot - high
        atr_txt = f" ({distance / atr_value:.2f} ATR)" if atr_value else ""
        return f"{label}: above high by {distance:.2f} pts{atr_txt}"
    if spot < low:
        distance = low - spot
        atr_txt = f" ({distance / atr_value:.2f} ATR)" if atr_value else ""
        return f"{label}: below low by {distance:.2f} pts{atr_txt}"
    return f"{label}: inside range"


def _breakout_state_text(ctx: MarketContext) -> str:
    """Direct counter to the diagnosed defect: distinguishes a held breakout
    from a failed one from no breakout at all, rather than letting the model
    infer breakout-ness from "price is near a window extreme" (see
    app/market_context.py's module docstring for the failure mode)."""
    if ctx.setups.get("ORB_BREAK_UP"):
        return f"closed above OR high, held {int(ctx.setup_strength.get('orb_bars_held_up', 0.0))} bar(s)"
    if ctx.setups.get("ORB_BREAK_DOWN"):
        return f"closed below OR low, held {int(ctx.setup_strength.get('orb_bars_held_down', 0.0))} bar(s)"
    if ctx.setups.get("FAILED_BREAKOUT_UP"):
        return "failed breakout above OR high, closed back inside"
    if ctx.setups.get("FAILED_BREAKOUT_DOWN"):
        return "failed breakout below OR low, closed back inside"
    return "no breakout"


def _ema_stack_text(ema9: float | None, ema21: float | None, ema50: float | None) -> str | None:
    if ema9 is None or ema21 is None or ema50 is None:
        return None
    if ema9 > ema21 > ema50:
        return "stacked up"
    if ema9 < ema21 < ema50:
        return "stacked down"
    return "mixed"


def _adx_regime_text(adx_value: float | None) -> str | None:
    if adx_value is None:
        return None
    if adx_value < ADX_NO_TREND:
        return f"{adx_value:.1f}  -> no established trend (<{ADX_NO_TREND:.0f})"
    if adx_value < ADX_TRENDING:
        return f"{adx_value:.1f}  -> developing trend ({ADX_NO_TREND:.0f}-{ADX_TRENDING:.0f})"
    return f"{adx_value:.1f}  -> established trend (>{ADX_TRENDING:.0f})"


def _cpr_regime_text(cpr: CPR | None) -> str | None:
    if cpr is None:
        return None
    if cpr.classification == "NARROW":
        note = "narrow, trending day more likely"
    elif cpr.classification == "WIDE":
        note = "wide, range-bound day more likely"
    else:
        note = "moderate"
    return f"{cpr.width_percent:.2f}% -> {note}"


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _drift_text(value: float | None) -> str:
    # "not available" rather than omitting the line entirely -- DRIFT is
    # always a live dimension of the prompt; a missing window (e.g. the
    # 3-hour window before 12:15 IST) should read as "not yet possible to
    # know", not as though the concept doesn't apply this cycle.
    return f"{value:+.2f}%" if value is not None else "not available"


def _build_user_prompt(index: IndexConfig, current_price: float, ctx: MarketContext, now_ist: datetime) -> str:
    """Structural market-context prompt (Phase 1b). Replaces the Phase 0/1
    eight-line tick-window prompt, whose "window high/low" and "up/down move
    count" lines were the direct cause of the diagnosed failure mode: over any
    window sampled while price drifts, the latest price *is* the window high
    by construction, so "price is at the window high" restated "price went
    up" rather than carrying real information. See app/market_context.py's
    module docstring and docs/ai-origination-roadmap.md Phase 2 for the
    losing-trade evidence this fixes.

    Every section is built defensively: a value that isn't available (warm-up
    incomplete, previous-day record stale, opening range not yet closed) omits
    its line rather than rendering a fabricated zero or the literal string
    "None" -- a phantom level is worse than no level. See _prompt_has_defect,
    which is the backstop if this ever fails to hold.
    """
    session_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    session_close = now_ist.replace(hour=_TRADING_END_HOUR, minute=_TRADING_END_MINUTE, second=0, microsecond=0)
    minutes_since_open = max(int((now_ist - session_open).total_seconds() // 60), 0)
    minutes_to_close = max(int((session_close - now_ist).total_seconds() // 60), 0)

    lines = [
        f"Index: {index.display_name or index.symbol}",
        f"Current price: {current_price:.2f}",
        f"Time: {now_ist.strftime('%H:%M')} IST ({minutes_since_open} min since open, {minutes_to_close} min to close)",
        "",
        "REGIME",
    ]
    adx_text = _adx_regime_text(ctx.adx)
    if adx_text is not None:
        lines.append(f"  ADX(14) 5-min: {adx_text}")
    cpr_text = _cpr_regime_text(ctx.cpr)
    if cpr_text is not None:
        lines.append(f"  CPR width: {cpr_text}")
    if ctx.atr_value is not None:
        atr_pct_txt = f" ({ctx.atr_percent:.2f}% of price)" if ctx.atr_percent is not None else ""
        lines.append(f"  ATR(14): {ctx.atr_value:.2f} pts{atr_pct_txt}")

    levels = ctx.levels
    structure_lines: list[str] = []
    if levels.opening_range_complete and levels.opening_range_high is not None and levels.opening_range_low is not None:
        structure_lines.append(
            f"  Opening range (09:15-09:45): high {levels.opening_range_high:.2f}, low {levels.opening_range_low:.2f}"
        )
        or_position = _position_line(
            "Position vs opening range", current_price, levels.opening_range_high, levels.opening_range_low, ctx.atr_value
        )
        if or_position is not None:
            structure_lines.append(f"  {or_position}")
        structure_lines.append(f"  Breakout state: {_breakout_state_text(ctx)}")
    if levels.previous_day_high is not None and levels.previous_day_low is not None and levels.previous_day_close is not None:
        structure_lines.append(
            f"  Previous day: high {levels.previous_day_high:.2f}, low {levels.previous_day_low:.2f}, "
            f"close {levels.previous_day_close:.2f}"
        )
        pd_position = _position_line(
            "Position vs previous day", current_price, levels.previous_day_high, levels.previous_day_low, ctx.atr_value
        )
        if pd_position is not None:
            structure_lines.append(f"  {pd_position}")
    if levels.day_open is not None and levels.day_high is not None and levels.day_low is not None:
        structure_lines.append(
            f"  Today: open {levels.day_open:.2f}, high {levels.day_high:.2f}, low {levels.day_low:.2f}"
        )
        if levels.day_high > levels.day_low:
            percentile = round((current_price - levels.day_low) / (levels.day_high - levels.day_low) * 100)
            structure_lines.append(
                f"  Position in today's range: {_ordinal(percentile)} percentile (0 = low, 100 = high)"
            )
    if structure_lines:
        lines += ["", "STRUCTURE", *structure_lines]

    trend_lines: list[str] = []
    if ctx.supertrend_15m is not None:
        direction = "up" if ctx.supertrend_15m == 1 else "down"
        value_txt = f" ({ctx.supertrend_15m_value:.2f})" if ctx.supertrend_15m_value is not None else ""
        trend_lines.append(f"  Supertrend 15-min: {direction}{value_txt}")
    if ctx.supertrend_5m is not None:
        direction = "up" if ctx.supertrend_5m == 1 else "down"
        value_txt = f" ({ctx.supertrend_5m_value:.2f})" if ctx.supertrend_5m_value is not None else ""
        trend_lines.append(f"  Supertrend 5-min: {direction}{value_txt}")
    if ctx.supertrend_5m is not None and ctx.supertrend_15m is not None:
        trend_lines.append(f"  Aligned: {'yes' if ctx.supertrend_5m == ctx.supertrend_15m else 'no'}")
    stack_text = _ema_stack_text(ctx.ema9, ctx.ema21, ctx.ema50)
    if stack_text is not None:
        trend_lines.append(
            f"  EMA9 {ctx.ema9:.2f} / EMA21 {ctx.ema21:.2f} / EMA50 {ctx.ema50:.2f} -> {stack_text}"
        )
    if ctx.rsi_value is not None:
        trend_lines.append(f"  RSI(14): {ctx.rsi_value:.2f}")
    if trend_lines:
        lines += ["", "TREND", *trend_lines]

    extension_lines: list[str] = []
    if ctx.ema21 is not None:
        distance = round(current_price - ctx.ema21, 2)
        atr_txt = f" ({ctx.distance_from_ema21_atr:.2f} ATR)" if ctx.distance_from_ema21_atr is not None else ""
        extension_lines.append(f"  Distance from EMA21: {distance:+.2f} pts{atr_txt}")
    if levels.day_high is not None and levels.day_low is not None:
        day_range = round(levels.day_high - levels.day_low, 2)
        atr_txt = f" ({ctx.day_range_atr_multiple:.2f} ATR)" if ctx.day_range_atr_multiple is not None else ""
        extension_lines.append(f"  Today's range: {day_range:.2f} pts{atr_txt}")
    if extension_lines:
        lines += ["", "EXTENSION", *extension_lines]

    lines += [
        "",
        "DRIFT",
        f"  15 min: {_drift_text(ctx.drift_15m)}",
        f"  45 min: {_drift_text(ctx.drift_45m)}",
        f"  3 hours: {_drift_text(ctx.drift_180m)}",
        f"  Since open: {_drift_text(ctx.drift_since_open)}",
    ]

    lines += ["", "Decide: BUY_CE, BUY_PE, or NONE?"]
    return "\n".join(lines)


def _prompt_has_defect(prompt: str) -> bool:
    """Backstop for the omit-don't-fabricate rule above: if a None, nan, or a
    literal zero-with-no-real-meaning distance still made it into the rendered
    text despite every guard in _build_user_prompt, treat the prompt as
    malformed rather than send it -- a prompt that still parses is the failure
    mode most likely to go unnoticed, per the spec this implements."""
    return "None" in prompt or "nan" in prompt or "0.00 pts" in prompt


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


def _has_open_origination(db: Session, index_symbol: str, provider: str | None = None) -> bool:
    """Whether there's already an open AI Origination trade on this index.
    provider=None checks across any provider (used to decide whether it's even
    worth fetching price/ticks this cycle); a specific provider checks only
    that provider's own slot -- each provider gets its own independent trade
    per index rather than competing for one shared slot, so e.g. Claude can
    open and hold its own Bank Nifty trade at the same time OpenAI has one
    open too, instead of whichever provider goes first each cycle blocking
    the other out entirely."""
    conditions = [
        StrategyTrade.index_symbol == index_symbol,
        StrategyTrade.status == TradeStatus.OPEN,
    ]
    if provider:
        conditions.append(StrategyTrade.origin == f"AI_ORIGIN_{provider.strip().upper()}")
    else:
        conditions.append(StrategyTrade.origin.like("AI_ORIGIN_%"))
    return db.scalar(select(StrategyTrade.id).where(*conditions).limit(1)) is not None


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


def _load_market_context(
    db: Session, index: IndexConfig, spot: float, now_ist, smartapi: SmartAPIClient
) -> tuple[MarketContext | None, bool]:
    """Refresh 1-minute candles for this index, then build the structural
    context from them. Returns (None, False) on any shortfall -- callers fail
    closed.

    Candles replace IndexPriceTick as the market-data input specifically
    because tick density varied with whether a dashboard tab happened to be
    open, silently changing the AI's input resolution between ~3 and 100+
    samples. Exchange candles don't vary with anything.

    The second element of the return tuple is `data_stale`: True when the
    live refresh call itself failed (rate limit, network, auth) and this
    context was built entirely from whatever candles were already stored.
    That fallback is deliberately still allowed to produce a context rather
    than failing the cycle outright -- stored history is often still good
    enough -- but the caller must not treat "a context was returned" as proof
    the refresh succeeded. See the Friday rate-limit incident this was added
    for: a failed refresh was previously invisible past an INFO log line, so
    there was no way to tell which trades, if any, were opened on data that
    was minutes-to-hours old rather than fresh.
    """
    if not index.spot_token:
        return None, False
    data_stale = False
    try:
        # Pull a rolling window rather than only the newest bar: cheap (one
        # call), self-healing after any gap, and the upsert makes re-fetching
        # overlapping minutes free.
        from_dt = (now_ist - timedelta(days=_CANDLE_WARMUP_DAYS)).strftime("%Y-%m-%d %H:%M")
        to_dt = now_ist.strftime("%Y-%m-%d %H:%M")
        rows = smartapi.get_candles(
            exchange=index.spot_exchange,
            symboltoken=index.spot_token,
            interval=ONE_MINUTE,
            from_dt=from_dt,
            to_dt=to_dt,
        )
        if rows:
            store_bars(db, index.symbol, ONE_MINUTE, [parse_smartapi_row(row) for row in rows])
    except Exception as exc:
        # Non-fatal here: stored history may still be sufficient. The
        # sufficiency check below is what actually decides whether to proceed
        # at all; data_stale is what tells the caller it proceeded on old data.
        data_stale = True
        logger.info("[AI][ORIGIN] %s: candle refresh failed (%s), using stored history", index.symbol, exc)

    bars_1m = load_bars(db, index.symbol, ONE_MINUTE, limit=_CANDLE_LOAD_LIMIT)
    if not bars_1m:
        return None, data_stale
    bars_5m = resample(bars_1m, FIVE_MINUTE)
    bars_15m = resample(bars_1m, FIFTEEN_MINUTE)
    context = build_market_context(
        index_symbol=index.symbol,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_15m=bars_15m,
        spot=spot,
        as_of=now_ist.replace(tzinfo=None),
    )
    return context, data_stale


def _open_trade(
    db: Session,
    index: IndexConfig,
    provider: str,
    decision: _Decision,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
    spot_at_entry: float | None = None,
    day_ohlc_present: bool | None = None,
    tick_sample_count: int | None = None,
    market_context: MarketContext | None = None,
    data_stale: bool = False,
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
    except Exception as exc:
        logger.info("[AI][ORIGIN] Skipped: could not resolve contract for %s (%s)", index.symbol, exc)
        return None

    # DTE floor, checked before spending a quote-budget LTP call: whatever
    # find_atm_contract's nearest-available-expiry-no-offset selection
    # happens to return is not exempt from the noise-stop-rate finding above
    # just because it's close to expiry. -1 (unparseable expiry) fails this
    # too, deliberately -- an expiry we can't verify meets the floor doesn't
    # get the benefit of the doubt.
    dte = days_to_expiry(contract.expiry, to_ist(utc_now()).date())
    if dte < _MIN_DTE_TO_TRADE:
        logger.info(
            "[AI][ORIGIN] %s: nearest expiry %s is %s DTE, below the %s-DTE floor -- skipping",
            index.symbol, contract.expiry, dte, _MIN_DTE_TO_TRADE,
        )
        return None

    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[AI][ORIGIN] Skipped: could not resolve price for %s (%s)", index.symbol, exc)
        return None

    sl_percent = _TRAILING_INITIAL_SL_PERCENT if use_trailing else decision.sl_percent
    target_percent = _TRAILING_FALLBACK_TARGET_PERCENT if use_trailing else decision.target_percent
    stoploss = round(entry_price * (1 - sl_percent / 100), 2)
    target = round(entry_price * (1 + target_percent / 100), 2)
    origin = f"AI_ORIGIN_{provider.strip().upper()}"
    strategy_name = f"AI Origination - {index.display_name or index.symbol}"

    # Risk in comparable units. The premium percentages above are what the
    # engine acts on and nothing here changes that -- but they are not
    # comparable across option types, since an identical percentage is a much
    # tighter index distance on a put. Recording the index-point and ATR
    # equivalents makes the actual bet visible.
    atr_at_entry = market_context.atr_value if market_context else None
    stop_units = to_risk_units(
        abs(sl_percent or 0.0), index.symbol, contract.option_type, dte,
        spot_at_entry, atr_at_entry,
    )
    target_units = to_risk_units(
        abs(target_percent or 0.0), index.symbol, contract.option_type, dte,
        spot_at_entry, atr_at_entry,
    )

    # Paper by default, everywhere. Goes LIVE only when BOTH this specific
    # index has ai_origination_live_trade explicitly checked in Settings >
    # Instruments AND the server-side SMARTAPI_LIVE_TRADING switch is on --
    # the same two-key pattern every other live-capable strategy in this app
    # already uses (see MultiStrategyManager.resolve_mode). place_market_order
    # itself independently no-ops back to a "PAPER_ORDER" id if the server
    # switch is off, so this can never place a real order on its own even if
    # the index flag alone were somehow wrong.
    mode = TradingMode.LIVE if index.ai_origination_live_trade and smartapi.settings.live_trading else TradingMode.PAPER
    entry_order_id = None
    if mode == TradingMode.LIVE:
        entry_order_id = smartapi.place_market_order(contract, "BUY", contract.lot_size)

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
        mode=mode,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        entry_order_id=entry_order_id,
        highest_price=round(entry_price, 2),
        lowest_price=round(entry_price, 2),
        trailing_active=False,
        sl_mode=SLMode.TRAILING if use_trailing else SLMode.FIXED,
        spot_at_entry=spot_at_entry,
        day_ohlc_present=day_ohlc_present,
        tick_sample_count=tick_sample_count,
        market_context_json=json.dumps(market_context.as_dict()) if market_context else None,
        data_stale=data_stale,
        stop_index_points=stop_units.index_points,
        stop_atr_multiple=stop_units.atr_multiple,
        target_index_points=target_units.index_points,
        target_atr_multiple=target_units.atr_multiple,
        risk_units_extrapolated=stop_units.extrapolated if stop_units.index_points is not None else None,
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
    """Independent AI entry-origination check for the AI Origination page.
    Paper by default on every index; goes live only for an index that's had
    ai_origination_live_trade explicitly checked in Settings > Instruments,
    and even then only if SMARTAPI_LIVE_TRADING is also on server-side (see
    _open_trade). Every resulting trade carries an AI_ORIGIN_* origin,
    the same isolation convention used by app/ai/alternative_trader.py and
    app/ai/exit_shadow.py, so it's still fully separated from real SIGNAL
    trades regardless of paper/live mode. Owns its own DB session when called
    from the scheduler; smartapi/option_finder must be supplied there since
    this module has no app-context access of its own."""
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
                # Skip the whole index this cycle only if every configured
                # provider already has its own open trade here -- if even one
                # provider has room, it's still worth fetching price/ticks so
                # that provider gets its turn below.
                if all(_has_open_origination(session, index.symbol, provider_name) for _, provider_name, _ in provider_order):
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
                now_ist = to_ist(utc_now())
                if _still_observing(now_ist):
                    logger.info(
                        "[AI][ORIGIN] %s: still observing (market open until %02d:%02d IST), recording ticks only",
                        index.symbol, _TRADING_START_HOUR, _TRADING_START_MINUTE,
                    )
                    continue
                if _past_trading_end(now_ist):
                    logger.info(
                        "[AI][ORIGIN] %s: past trading end (%02d:%02d IST), no new entries -- "
                        "a trade opened now would just be caught by the 15:15 square-off "
                        "check and closed at breakeven a monitor cycle later",
                        index.symbol, _TRADING_END_HOUR, _TRADING_END_MINUTE,
                    )
                    continue
                # PHASE 1b: now the sole source of the entry prompt (see
                # _build_user_prompt). Fail closed on any shortfall -- a
                # partial context still reads as authoritative, and at
                # ~0.6-1.8% round-trip cost a marginal trade is negative
                # expectancy, so skipping is the cheaper error.
                market_context, data_stale = _load_market_context(session, index, price, now_ist, smartapi)
                if market_context is None:
                    logger.info(
                        "[AI][ORIGIN] %s: insufficient candle history for market context, skipping cycle",
                        index.symbol,
                    )
                    continue
                if data_stale:
                    # Made visible rather than only the INFO line inside
                    # _load_market_context -- this is the exact gap the Friday
                    # rate-limit incident exposed: a failed refresh was
                    # otherwise indistinguishable from a fresh one to anything
                    # reading logs/dashboard/exports after the fact.
                    logger.warning(
                        "[AI][ORIGIN] %s: candle refresh failed this cycle -- market context built from STALE "
                        "stored history, not a fresh pull", index.symbol,
                    )
                    log_event(
                        session, "AI_ORIGIN",
                        f"[{index.display_name or index.symbol}] Candle refresh failed, using stale stored history",
                        level="WARNING",
                    )
                if market_context.adx is None or market_context.atr_value is None or (
                    market_context.supertrend_5m is None or market_context.supertrend_15m is None
                ):
                    # ADX/ATR/Supertrend are the core regime/trend read the
                    # prompt is built around -- a degraded prompt missing them
                    # is worse than skipping the cycle, same fail-closed rule
                    # as a missing market_context entirely.
                    logger.info(
                        "[AI][ORIGIN] %s: ADX/ATR/Supertrend not yet warmed up, skipping cycle", index.symbol
                    )
                    continue
                logger.info(
                    "[AI][ORIGIN][CTX] %s regime=%s adx=%s cpr=%s setups=%s",
                    index.symbol, market_context.regime, market_context.adx,
                    market_context.cpr.classification if market_context.cpr else None,
                    sorted(k for k, v in market_context.setups.items() if v),
                )

                user_prompt = _build_user_prompt(index, price, market_context, now_ist)
                if _prompt_has_defect(user_prompt):
                    logger.error(
                        "[AI][ORIGIN] %s: malformed prompt (contains None/nan/0.00 pts), skipping cycle", index.symbol
                    )
                    log_event(
                        session, "AI_ORIGIN",
                        f"[{index.display_name or index.symbol}] Malformed prompt detected, cycle skipped",
                        level="ERROR",
                    )
                    continue
                for turn, provider_name, view in provider_order:
                    # Each provider gets its own independent trade slot per
                    # index -- Claude and OpenAI can each hold their own open
                    # Bank Nifty trade at the same time, rather than racing for
                    # one shared slot where whichever goes first blocks the
                    # other out entirely.
                    if _has_open_origination(session, index.symbol, provider_name):
                        continue
                    decision = _call_provider(provider_name, view, user_prompt)
                    if decision is None:
                        continue
                    logger.info("[AI][ORIGIN] %s -> %s (%s, %s)", index.symbol, decision.action, provider_name, turn)
                    if decision.action == "NONE":
                        # Only forward-facing signal of whether the model is
                        # well-judged-conservative or just quiet -- previously
                        # ai_reasoning only persisted for trades that opened,
                        # so a session of all-NONE had no record of why.
                        logger.debug(
                            "[AI][ORIGIN] %s NONE reasoning (%s): %s", index.symbol, provider_name, decision.reasoning
                        )
                    if decision.action in ("BUY_CE", "BUY_PE") and (decision.confidence or 0) >= _MIN_CONFIDENCE_TO_ACT:
                        _open_trade(
                            session, index, provider_name, decision, smartapi, option_finder,
                            # Snapshot of the prompt inputs this specific
                            # decision was made on -- see StrategyTrade's
                            # spot_at_entry comment. day_ohlc_present and
                            # tick_sample_count are no longer computed as of
                            # Phase 1b: the prompt input is now market_context
                            # (stored in full via market_context_json), not
                            # ticks/day OHLC, so those two diagnostic columns
                            # are left at their default (None) for new trades
                            # rather than fed a value that no longer means
                            # anything.
                            spot_at_entry=price,
                            market_context=market_context,
                            data_stale=data_stale,
                        )
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
