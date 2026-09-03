"""Autonomous AI -- a fourth AI subsystem, deliberately independent of the
other three (see CLAUDE.md's "Three AI subsystems, deliberately independent"
section), built for one explicit request: "AI will take its own decision
regardless of any adx or signal. Just it take trades and exit on its own."

3 SEP 2026 -- REBUILT PER AN EXTERNAL DESIGN DOCUMENT, IMPLEMENTED AS
SPECIFIED RATHER THAN ADAPTED
------------------------------------------------------------------------
The module previously here (shipped the same day, PR #81) deliberately
ADAPTED a pasted external redesign proposal: it kept the one genuinely new,
well-grounded idea (a stagnation exit) and declined four other mechanisms
(a VWAP/ADX pre-filter, a mid-session hard entry block, a fixed-width
trailing stop, an ADX gate) with specific, evidence-based reasons -- VWAP had
no real data source, the ADX gate had already been tested and rejected, the
mid-session block contradicted this project's one Bonferroni-significant
finding, and the fixed-width trail was the same shape a 31 Jul holdout had
already found clips winners early. That reasoning is preserved below where
it still applies, but the user's follow-up instruction was explicit and
unambiguous: "Do whatever said in md file i shared without any judgement" --
a deliberate override of the earlier adapted decision, made with the full
tradeoff breakdown already in hand. This rebuild implements the document as
written, including the four previously-declined mechanisms, because the
person who owns this money asked for exactly that after being told what it
would cost. Two things make that override lower-risk than it would be for
almost any other module in this app: this strategy is structurally
paper-only (see below -- no live order path exists to gate, not just one
that's off by default), and the previously-rejected findings were about
OTHER modules' entries (AI Origination's mechanical drift rule, rule-based
strategies' shared exit branch) -- Autonomous AI's own real track record
(6 closed trades as of 3 Sep) has never been large enough to confirm or
refute any of this on its own terms.

WHAT MAKES THIS DIFFERENT FROM AI ORIGINATION
------------------------------------------------
AI Origination (app.ai.originator) already opens trades entirely on its own
judgment, with no TradingView signal involved. What still makes this module
distinct: the model is re-asked, every cycle, whether to HOLD or EXIT an
open position, and an EXIT answer actually closes the trade immediately --
AI Origination's exit is mechanical (a stop/target proposed once at entry).
This is real, ongoing LLM cost per open position per cycle that AI
Origination does not have -- flagged directly to the user before this module
was first built, who chose it anyway over the cheaper fixed-stop/target
alternative.

Unlike the original build, this module's ENTRY decision is no longer made
on price/range alone. It now runs the same class of deterministic feature
engine AI Origination's own market_context already provides (ADX, EMA9/21,
PDH/PDL, via app.market_context.build_market_context, reused rather than
reimplemented) plus one genuinely new feature this project has never had
before: a real intraday VWAP, computed from a FUTURES contract's actual
volume rather than the index instrument's own candles, which report volume
as zero (see CLAUDE.md -- the same wall BNV5.1/BNV6's VWAP gate hits, and
the reason VWAP could not be backtested for those strategies either). The
model is still the final gate before a trade opens -- SYSTEM_PROMPT_ENTRY
below defaults to NONE and states its own criteria explicitly -- but Python
now blocks the clearly-bad cases (session phase, ADX floor) before spending
an API call on them, per the design document's own "Hard Gating Rules in
Python" stage.

A KNOWN, NAMED RISK -- NOT SWEPT UNDER THE RUG
--------------------------------------------------
This project's own 30 Jul 2026 two-year backtest ("AI Origination's entry
signal does not work", CLAUDE.md) found ZERO positive directional edge in a
45-minute price-drift rule on either index. That finding was about a signal
built from price action ALONE, with no ADX/EMA/VWAP structure behind it --
the same class of input the ORIGINAL version of this module used. This
rebuild's entry gate is structurally different (ADX-gated, VWAP-gated,
session-phase-gated), so the 30 Jul finding does not describe it directly --
but the ADX gate specifically has ALSO already been tested for AI
Origination and found NOT SUPPORTED at any floor, on both real trade history
and the full 2-year index archive (scripts/adx_gate_backtest.py). That
result was for a different population (AI Origination's own decisions, not
this module's), so it does not mechanically transfer either -- but it means
"ADX-gated" is not itself a proven improvement in this codebase's own
history. This module's own real results, on its own real population, remain
the only way to actually answer whether this specific construction works --
read them with the same "not yet enough evidence" discipline as everything
else in this project.

STRUCTURALLY PAPER-ONLY
-------------------------
Same construction as app.validated_signal: mode is hardcoded to
TradingMode.PAPER, smartapi.place_market_order is never called anywhere in
this module. No live path exists to gate, not just one that's off by
default -- of every experimental strategy in this project, this remains the
least validated and the most expensive to run wrong, so it gets the
strictest treatment. This is also the main reason implementing the document
"without judgement" is a reasonable choice: the blast radius of a wrong
design decision here is paper P&L and API spend, not real capital.

FEATURE ENGINE -- WHAT'S DETERMINISTIC, WHAT'S STILL THE MODEL'S CALL
----------------------------------------------------------------------------
Computed fresh in Python every cycle, for every enabled index, before either
the entry or exit prompt is built (see _compute_features):

  - Intraday VWAP and the spot's relation to it (ABOVE_VWAP / BELOW_VWAP /
    AT_VWAP, the last one meaning within 0.1% of VWAP per the design doc).
    Computed from a FUTIDX futures contract's real 1-minute volume (see
    OptionFinder.find_current_futures_contract and _compute_futures_vwap) --
    index instruments cannot support this at all. Fails closed to
    vwap=None / vwap_relation="UNKNOWN" if the futures contract can't be
    resolved or has no volume yet today; this does not fail the rest of the
    feature set, since it is one input among several, not a precondition
    for computing ADX/EMA/PDH-PDL.
  - Fast/slow EMA (9/21, on 5-min bars) and the resulting trend regime
    (BULLISH / BEARISH / NEUTRAL) -- both already computed by
    build_market_context, reused rather than recomputed.
  - ADX(14) on 5-min bars -- same source.
  - Distance to the previous day's high/low (PDH/PDL) -- same source
    (app.market_context.Levels).
  - Session phase, on the design document's own boundaries: OPENING_
    VOLATILITY (<09:30), MORNING_MOMENTUM (09:30-11:15), CHOP_ZONE
    (11:15-13:30), AFTERNOON_TREND (13:30-15:00), SQUARE_OFF_ZONE (>=15:00).

Two deterministic hard gates block a fresh entry before any LLM call, per
the document's own AutonomousOptionsAgent.evaluate_entry sample code
specifically (not every criterion the prompt text lists -- see
_ENTRY_BLOCKED_SESSION_PHASES and _ADX_HARD_FLOOR):
  - session_phase in {CHOP_ZONE, OPENING_VOLATILITY, SQUARE_OFF_ZONE}
  - ADX < 18

Trend regime, VWAP relation, ADX >= 20, and PDH/PDL proximity remain the
model's own judgment call, stated as explicit criteria in SYSTEM_PROMPT_
ENTRY -- the document's own design keeps these as LLM criteria rather than
Python hard gates, and this rebuild preserves that split rather than
tightening it further.

EXIT MATRIX -- DETERMINISTIC RULES CHECKED BEFORE THE MODEL, IN ORDER
--------------------------------------------------------------------------
check_autonomous_exits checks these in sequence, each one closing the trade
immediately and skipping the LLM call entirely if it fires (matching the
document's own "hard code these so network/model downtime never blows up
your account" instruction). Only if NONE of them fire does the model get
asked at all:

  1. The existing unconditional 15:00 IST cutoff (_TRADING_END, unchanged
     from the original build -- see that constant's own comment).
  2. Session-close warning: minutes_to_square_off <= 15 (from 14:45 IST).
     New. ExitReason.AUTONOMOUS_SESSION_CLOSE.
  3. Peak-giveback (fixed-width): peak_pnl_pct >= 20% and a drop of >= 8%
     from that peak. New -- previously declined as "the same shape the 31
     Jul holdout already found clips winners early"; implemented now per
     the explicit override. ExitReason.AUTONOMOUS_TRAIL_EXIT. Deliberately
     distinct from the already-shipped, PROPORTIONAL giveback-ratio stop
     (app/multi_strategy.py, floor=12%/ratio=30% of the peak-to-entry
     distance, validated against 227 real AI Origination trades) -- both
     can fire on the same trade population; keeping them as separate exit
     reasons is what makes either one's real effect measurable on its own.
  4. Break-even violation: peak_pnl_pct >= 15% and current_pnl_pct <= 1%.
     New. ExitReason.AUTONOMOUS_BREAKEVEN_EXIT.
  5. Stagnation: holding_time_mins >= 25 and current_pnl_pct within +-5%.
     Reuses the mechanism the original build already shipped
     (ExitReason.AUTONOMOUS_STALL_EXIT), window shortened from 60 to 25
     minutes per the document's own number -- the original build's 60-
     minute choice was explicitly a reuse of AI Origination's own STALL_
     EXIT window "rather than inventing a new number"; the override
     instruction supersedes that choice with the document's own value.
  6. Structural invalidation: the position's own option_type contradicts
     the underlying's current spot-vs-VWAP relation (holding CE while spot
     is BELOW_VWAP, or PE while ABOVE_VWAP). New. Requires this cycle's
     computed features; skipped (never fires, falls through to the model)
     when features are unavailable rather than guessing.
     ExitReason.AUTONOMOUS_STRUCTURAL_EXIT.

Two of the document's checkpoints needed NO new code, because they already
fall out of existing infrastructure once the backstop's own nominal
percentages were tightened:
  - "Max Loss per Trade -15% to -18%" and "Profit Target" are already
    enforced by app.multi_strategy's shared FIXED-branch monitor (every 30
    seconds, faster than this module's own ~5-minute cycle) via the trade's
    own trade.stoploss/trade.target -- see _BACKSTOP_STOP_PERCENT_NOMINAL /
    _BACKSTOP_TARGET_PERCENT_NOMINAL below, now 15.0/30.0 (was 35.0/50.0),
    matching the document's own PositionTracker defaults exactly.

A cycle where the exit call errors or is skipped for any reason leaves the
position open (fails to a no-op, not a forced exit) -- the deterministic
matrix above and the shared 15%/30% backstop are what protect it, not a
retry.

ISOLATION
----------
origin="AUTONOMOUS_AI" -- its own population, matched with ==, never counted
in any AI Origination or Validated Signal report/backtest/dashboard filter.
One open position at a time per index. Single-provider only (whichever
AISettings.provider is configured).

WHERE THIS RUNS
-----------------
Its own scheduler job (app.scheduler's "autonomous-ai-check", same 5-minute
cron/market-hours-gate shape as "ai-origination-check"), NOT hooked into
run_origination_checks. External signature (smartapi, option_finder,
trade_manager, feed_store, db) is unchanged by this rebuild -- app.main and
app.scheduler needed no changes.
"""

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
from app.market_context import build_market_context
from app.market_data import (
    FIFTEEN_MINUTE,
    FIVE_MINUTE,
    ONE_MINUTE,
    load_bars,
    parse_smartapi_row,
    resample,
    store_bars,
)
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

# Tightened 3 Sep 2026 from 35.0/50.0 to match the design document's own
# PositionTracker(stop_loss_pct=15.0, target_pnl_pct=30.0) defaults exactly,
# per the explicit "implement without judgement" override. These are still a
# safety net, not the intended exit mechanism -- see the module docstring's
# EXIT MATRIX section for why the document's own "Max Loss"/"Profit Target"
# checkpoints need no new code beyond this constant change.
_BACKSTOP_STOP_PERCENT_NOMINAL = 15.0
_BACKSTOP_TARGET_PERCENT_NOMINAL = 30.0

_DEFAULT_TRADING_START = (9, 45)

# 31 Aug 2026: a dedicated, EARLIER cutoff for this strategy specifically --
# "should not take trades after 3pm and square off at 3pm only". Deliberately
# NOT the shared Settings > General square-off time (PlatformSettings.
# square_off_time, 15:15 default) every other strategy in this app reads.
# Unchanged by the 3 Sep rebuild -- also matches the design document's own
# SQUARE_OFF_ZONE boundary (>=15:00) exactly, so no conflict to resolve.
_TRADING_END = (15, 0)

# ---------------------------------------------------------------------------
# Feature engine constants -- from the 3 Sep 2026 external design document,
# implemented as specified. See module docstring's "FEATURE ENGINE" and
# "EXIT MATRIX" sections for what each one does and why.
# ---------------------------------------------------------------------------

# Session phase boundaries (IST). SQUARE_OFF_ZONE's start is _TRADING_END
# itself, not a separate constant, so the two can never drift apart.
_MORNING_MOMENTUM_START = (9, 30)
_CHOP_ZONE_START = (11, 15)
_AFTERNOON_TREND_START = (13, 30)

_ENTRY_BLOCKED_SESSION_PHASES = frozenset({"CHOP_ZONE", "OPENING_VOLATILITY", "SQUARE_OFF_ZONE"})

# "ADX < 18: Choppy, trendless market. Zero option buying allowed" -- the
# document's own Python-level hard gate. ADX >= 20 ("Trending conditions
# suitable for directional CE/PE purchases") is deliberately left as an LLM
# criterion in SYSTEM_PROMPT_ENTRY, not a second Python gate -- the document
# only hard-codes the 18 floor in its own sample evaluate_entry(), so the
# 18-20 band is intentionally left for the model to reject via its own
# stated criteria, not blocked outright in Python.
_ADX_HARD_FLOOR = 18.0
_ADX_LLM_FLOOR = 20.0

# "Chop Zone: Spot within 0.1% of VWAP."
_VWAP_CHOP_BAND_PERCENT = 0.1

# Fixed-width peak-giveback exit. See module docstring's EXIT MATRIX #3.
_PEAK_GIVEBACK_ACTIVATE_PERCENT = 20.0
_PEAK_GIVEBACK_WIDTH_PERCENT = 8.0

# Break-even violation exit. See module docstring's EXIT MATRIX #4.
_BREAKEVEN_VIOLATION_ACTIVATE_PERCENT = 15.0
_BREAKEVEN_VIOLATION_FLOOR_PERCENT = 1.0

# Stagnation / theta-decay exit. Window shortened 60 -> 25 minutes 3 Sep 2026
# per the document's own number (see EXIT MATRIX #5 for why this supersedes
# the original build's reused-AI-Origination-window choice). Band unchanged
# at +-5% -- the document's own number already matched.
_STALL_WINDOW_MINUTES = 25
_STALL_BAND_PERCENT = 5.0

# "If minutes_to_square_off <= 15, EXIT immediately." See EXIT MATRIX #2.
_SESSION_CLOSE_WARNING_MINUTES = 15

# Candle warmup/load, mirroring app.ai.originator's own constants for the
# same purpose -- duplicated rather than imported, per this module's own
# "deliberately independent" convention (see the top-level module docstring
# in the original build).
_CANDLE_WARMUP_DAYS = 7
_CANDLE_LOAD_LIMIT = 3000

# Synthetic index_symbol suffix futures candles are stored under (see
# _compute_futures_vwap) -- keeps them entirely out of the real index candle
# history every other consumer (build_market_context, CPR, PDH/PDL, AI
# Origination) relies on.
_FUTURES_CANDLE_SUFFIX = "_FUT"

SYSTEM_PROMPT_ENTRY = """You are a conservative trade-execution filter for intraday Indian index options (Nifty/Bank Nifty).
Your primary job is capital preservation. Your default decision MUST BE "NONE" unless strict, objective criteria are verified.

Evaluation Protocol:
1. BUY_CE Criteria (ALL must be true):
   - Regime indicates sustained upward momentum (Fast EMA > Slow EMA).
   - Trend strength is confirmed (ADX >= 20).
   - Spot price is trading strictly ABOVE intraday VWAP.
   - Market timing is within active momentum windows (not in mid-day chop).
   - Spot price is not extended directly into major daily resistance (PDH).

2. BUY_PE Criteria (ALL must be true):
   - Regime indicates sustained downward momentum (Fast EMA < Slow EMA).
   - Trend strength is confirmed (ADX >= 20).
   - Spot price is trading strictly BELOW intraday VWAP.
   - Market timing is within active momentum windows (not in mid-day chop).
   - Spot price is not extended directly into major daily support (PDL).

3. Mandatory Reject ("NONE") Conditions:
   - Price is oscillating near VWAP or ADX indicates low trend strength / consolidation (< 20).
   - Current session is "CHOP_ZONE" (11:15 AM - 1:30 PM).
   - Contradictory signals exist (e.g., price above VWAP but momentum trending down).
   - Any required indicator or confirmation is ambiguous or missing.

Do not guess, predict reversals, or anticipate breakouts. If there is any doubt or lack of edge, output NONE.

Respond with a single valid JSON object only, with no markdown fences or extra text:
{"decision": "BUY_CE" | "BUY_PE" | "NONE", "confidence": 0.0-1.0, "reasoning": "Direct citation of matching or failing rules"}"""

SYSTEM_PROMPT_EXIT = """You are a disciplined trade-management execution engine for intraday Indian index options (Nifty/Bank Nifty).
Your mandate is twofold: protect accumulated profits aggressively and kill stagnant trades before theta decay causes irreversible loss.

Evaluation Rules:

1. Mandatory Profit Protection (EXIT):
   - Peak Giveback: If peak_pnl_pct >= 20% and current_pnl_pct drops by more than 8% from peak, EXIT immediately.
   - Profit Target: If current_pnl_pct >= target_pnl_pct, EXIT to lock gains.
   - Break-Even Violation: If peak_pnl_pct >= 15% and current_pnl_pct drops back to <= 1%, EXIT immediately.

2. Theta Decay & Time Stop (EXIT):
   - Stagnation Rule: If holding_time_mins >= 25 minutes and current_pnl_pct is between -5% and +5%, EXIT. Do not pay theta on non-moving trades.
   - Session Close: If minutes_to_square_off <= 15, EXIT immediately.

3. Structural Invalidation (EXIT):
   - Adverse Momentum: If spot_vs_vwap contradicts position side (e.g., holding CE but spot broke below VWAP, or holding PE but spot broke above VWAP), EXIT immediately.
   - Stop-Loss Hit: If current_pnl_pct <= -stop_loss_pct, EXIT.

4. Criteria to HOLD:
   - Trade is active, underlying momentum remains strictly aligned with position side, holding time is under 20 minutes, and no profit-protection triggers have fired.

Do not gamble on reversals or hold through sideways drift. Respond strictly with a single valid JSON object only:
{"decision": "EXIT" | "HOLD", "confidence": 0.0-1.0, "exit_reason": "RULE_NAME_OR_NONE", "reasoning": "Direct explanation based on rules"}"""


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
    # The model's own named rule ("PEAK_GIVEBACK_GUARD", "HARD_STOP_LOSS",
    # etc, per SYSTEM_PROMPT_EXIT's own schema) -- logged for audit, never
    # used to pick the stored ExitReason. A voluntary model EXIT is always
    # recorded as ExitReason.AI_DISCRETION_EXIT regardless of which rule the
    # model cites, so reporting can still tell "the model decided to leave"
    # apart from "a deterministic backstop caught it" -- the deterministic
    # matrix in check_autonomous_exits is what actually enforces those named
    # rules; this field is commentary on top of a decision Python already
    # made independently.
    exit_rule: str | None = None


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
        exit_rule = data.get("exit_reason")
        return _ExitDecision(
            action,
            _parse_confidence(data.get("confidence")),
            str(data.get("reasoning") or ""),
            str(exit_rule) if exit_rule else None,
        )
    except Exception as exc:
        return _ExitDecision("ERROR", None, f"Could not parse AI response ({type(exc).__name__}: {exc})")


@dataclass(frozen=True)
class _Features:
    """The design document's deterministic feature set, computed fresh each
    cycle for one index. See module docstring's FEATURE ENGINE section."""

    spot: float
    vwap: float | None
    vwap_relation: str  # ABOVE_VWAP / BELOW_VWAP / AT_VWAP / UNKNOWN
    fast_ema: float | None  # EMA9
    slow_ema: float | None  # EMA21
    trend_regime: str  # BULLISH / BEARISH / NEUTRAL / UNKNOWN
    adx: float | None
    pdh: float | None
    pdl: float | None
    dist_to_pdh: float | None
    dist_to_pdl: float | None
    session_phase: str
    minutes_to_close: int


def _session_phase(now_ist) -> str:
    """Boundaries verbatim from the design document's AutonomousOptionsAgent.
    get_session_phase. SQUARE_OFF_ZONE's start is _TRADING_END itself, not a
    separate literal, so this can never drift out of sync with the
    unconditional 15:00 cutoff check_autonomous_exits already enforces."""
    t = (now_ist.hour, now_ist.minute)
    if t < _MORNING_MOMENTUM_START:
        return "OPENING_VOLATILITY"
    if t < _CHOP_ZONE_START:
        return "MORNING_MOMENTUM"
    if t < _AFTERNOON_TREND_START:
        return "CHOP_ZONE"
    if t < _TRADING_END:
        return "AFTERNOON_TREND"
    return "SQUARE_OFF_ZONE"


def _vwap_relation(spot: float, vwap: float | None) -> str:
    if vwap is None or vwap == 0:
        return "UNKNOWN"
    diff_percent = abs(spot - vwap) / vwap * 100
    if diff_percent <= _VWAP_CHOP_BAND_PERCENT:
        return "AT_VWAP"
    return "ABOVE_VWAP" if spot > vwap else "BELOW_VWAP"


def _trend_regime(fast_ema: float | None, slow_ema: float | None) -> str:
    if fast_ema is None or slow_ema is None:
        return "UNKNOWN"
    if fast_ema > slow_ema:
        return "BULLISH"
    if fast_ema < slow_ema:
        return "BEARISH"
    return "NEUTRAL"


def _compute_futures_vwap(
    db: Session, index: IndexConfig, option_finder: OptionFinder, smartapi: SmartAPIClient, now_ist
) -> float | None:
    """Today's session VWAP from the near-month futures contract's real
    volume. Index-instrument candles report volume=0 (see CLAUDE.md), so
    they cannot support a real VWAP at all -- this is why the original
    build of this module, and every previously-declined-VWAP mechanism in
    this project, could not compute one. Stored under a synthetic
    index_symbol key (see _FUTURES_CANDLE_SUFFIX) so this never mixes into
    the real index candle history every other consumer relies on.

    Fails closed -- returns None, never fabricates a value -- when the
    futures contract can't be resolved, the candle fetch fails with nothing
    already stored, or there's no real volume in today's session yet (e.g.
    called right at market open)."""
    try:
        contract = option_finder.find_current_futures_contract(index)
    except Exception as exc:
        logger.info("[AUTONOMOUS_AI] %s: futures contract lookup failed (%s), VWAP unavailable", index.symbol, exc)
        return None
    if contract is None:
        logger.info("[AUTONOMOUS_AI] %s: no futures contract found, VWAP unavailable", index.symbol)
        return None

    futures_key = f"{index.symbol}{_FUTURES_CANDLE_SUFFIX}"
    try:
        rows = smartapi.get_candles(
            exchange=contract["exchange"],
            symboltoken=contract["symboltoken"],
            interval=ONE_MINUTE,
            from_dt=now_ist.strftime("%Y-%m-%d 09:15"),
            to_dt=now_ist.strftime("%Y-%m-%d %H:%M"),
        )
        if rows:
            store_bars(db, futures_key, ONE_MINUTE, [parse_smartapi_row(row) for row in rows])
    except Exception as exc:
        logger.info("[AUTONOMOUS_AI] %s: futures candle refresh failed (%s), using stored history", index.symbol, exc)

    bars = load_bars(db, futures_key, ONE_MINUTE)
    today_bars = [b for b in bars if b.ts_ist.date() == now_ist.date()]
    total_volume = sum(b.volume for b in today_bars)
    if not today_bars or total_volume <= 0:
        return None
    cumulative = sum(((b.high + b.low + b.close) / 3) * b.volume for b in today_bars)
    return round(cumulative / total_volume, 2)


def _compute_features(
    db: Session,
    index: IndexConfig,
    spot: float,
    now_ist,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[_Features]:
    """The design document's deterministic feature engine. Returns None --
    fail closed, same convention build_market_context itself uses -- when
    there isn't enough INDEX candle history to compute ADX/EMA honestly.
    VWAP failing on its own does not fail the whole feature set (see
    _compute_futures_vwap) -- a missing futures contract is a data-
    availability question distinct from whether the index itself has
    enough history for ADX/EMA/PDH-PDL."""
    if not index.spot_token:
        return None
    try:
        rows = smartapi.get_candles(
            exchange=index.spot_exchange,
            symboltoken=index.spot_token,
            interval=ONE_MINUTE,
            from_dt=(now_ist - timedelta(days=_CANDLE_WARMUP_DAYS)).strftime("%Y-%m-%d %H:%M"),
            to_dt=now_ist.strftime("%Y-%m-%d %H:%M"),
        )
        if rows:
            store_bars(db, index.symbol, ONE_MINUTE, [parse_smartapi_row(row) for row in rows])
    except Exception as exc:
        logger.info("[AUTONOMOUS_AI] %s: candle refresh failed (%s), using stored history", index.symbol, exc)

    bars_1m = load_bars(db, index.symbol, ONE_MINUTE, limit=_CANDLE_LOAD_LIMIT)
    if not bars_1m:
        return None
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
    if context is None:
        return None

    vwap = _compute_futures_vwap(db, index, option_finder, smartapi, now_ist)
    session_phase = _session_phase(now_ist)
    end_minutes = _TRADING_END[0] * 60 + _TRADING_END[1]
    now_minutes = now_ist.hour * 60 + now_ist.minute
    minutes_to_close = max(end_minutes - now_minutes, 0)
    pdh = context.levels.previous_day_high
    pdl = context.levels.previous_day_low

    return _Features(
        spot=spot,
        vwap=vwap,
        vwap_relation=_vwap_relation(spot, vwap),
        fast_ema=context.ema9,
        slow_ema=context.ema21,
        trend_regime=_trend_regime(context.ema9, context.ema21),
        adx=context.adx,
        pdh=pdh,
        pdl=pdl,
        dist_to_pdh=round(pdh - spot, 2) if pdh is not None else None,
        dist_to_pdl=round(spot - pdl, 2) if pdl is not None else None,
        session_phase=session_phase,
        minutes_to_close=minutes_to_close,
    )


def _build_entry_prompt(features: _Features, index_display_name: str) -> str:
    adx_label = "Trending" if (features.adx or 0.0) >= _ADX_LLM_FLOOR else "Range-bound/Chop"
    adx_text = f"{features.adx:.1f}" if features.adx is not None else "unavailable"
    vwap_text = f"{features.vwap:.2f}" if features.vwap is not None else "unavailable"
    fast_ema_text = f"{features.fast_ema:.2f}" if features.fast_ema is not None else "unavailable"
    slow_ema_text = f"{features.slow_ema:.2f}" if features.slow_ema is not None else "unavailable"
    dist_to_pdh = f"{features.dist_to_pdh} pts" if features.dist_to_pdh is not None else "unknown"
    dist_to_pdl = f"{features.dist_to_pdl} pts" if features.dist_to_pdl is not None else "unknown"
    return (
        "Current Market State:\n"
        f"- Index: {index_display_name}\n"
        f"- Spot Price: {features.spot:.2f}\n"
        f"- Intraday VWAP: {vwap_text} (Relation: {features.vwap_relation})\n"
        f"- Trend Regime: {features.trend_regime} (9 EMA: {fast_ema_text}, 21 EMA: {slow_ema_text})\n"
        f"- ADX (14): {adx_text} ({adx_label})\n"
        f"- Proximity to Key Levels: PDH: {dist_to_pdh} | PDL: {dist_to_pdl}\n"
        f"- Session Phase: {features.session_phase} (Time to square-off: {features.minutes_to_close} mins)\n\n"
        "Apply the evaluation rules. Output decision JSON:"
    )


def _peak_pnl_percent(trade: StrategyTrade) -> float:
    if not trade.entry_price or trade.highest_price is None:
        return trade.pnl_percent or 0.0
    return round((trade.highest_price - trade.entry_price) / trade.entry_price * 100, 2)


def _structural_invalidation(trade: StrategyTrade, features: _Features) -> bool:
    if features.vwap_relation == "UNKNOWN":
        return False
    if trade.option_type == "CE":
        return features.vwap_relation == "BELOW_VWAP"
    return features.vwap_relation == "ABOVE_VWAP"


def _build_exit_prompt(trade: StrategyTrade, features: Optional[_Features], now_ist) -> str:
    entry_ist = to_ist(trade.entry_time)
    holding_minutes = max(int((now_ist - entry_ist).total_seconds() // 60), 0) if entry_ist is not None else 0
    current_pnl_pct = trade.pnl_percent or 0.0
    peak_pnl_pct = _peak_pnl_percent(trade)
    stop_loss_pct = round((1 - trade.stoploss / trade.entry_price) * 100, 2) if trade.entry_price else 0.0
    target_pnl_pct = round((trade.target / trade.entry_price - 1) * 100, 2) if trade.entry_price else 0.0
    if features is not None:
        minutes_to_close = features.minutes_to_close
        vwap_status = features.vwap_relation
        momentum = features.trend_regime
    else:
        end_minutes = _TRADING_END[0] * 60 + _TRADING_END[1]
        now_minutes = now_ist.hour * 60 + now_ist.minute
        minutes_to_close = max(end_minutes - now_minutes, 0)
        vwap_status = "UNKNOWN"
        momentum = "UNKNOWN"
    return (
        "Position Status:\n"
        f"- Index: {trade.index_symbol} ({trade.option_type})\n"
        f"- Entry Premium: {trade.entry_price:.2f} | Current: {(trade.current_premium or trade.entry_price):.2f}\n"
        f"- Current P&L: {current_pnl_pct:.1f}%\n"
        f"- Peak P&L Reached: {peak_pnl_pct:.1f}%\n"
        f"- Holding Duration: {holding_minutes} minutes\n"
        f"- Minutes to Square-Off: {minutes_to_close}\n"
        f"- Underlying Spot vs VWAP: {vwap_status}\n"
        f"- Underlying Momentum: {momentum}\n"
        f"- Defined Risk Boundaries: Hard SL at -{stop_loss_pct}%, Target at +{target_pnl_pct}%\n\n"
        "Apply the evaluation rules. Output decision JSON:"
    )


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
        # only -- see module docstring's EXIT MATRIX section.
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
    features: Optional[_Features],
    now_ist,
    settings: AISettings,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[StrategyTrade]:
    if _has_open_autonomous_trade(db, index.symbol):
        return None
    if features is None:
        logger.info("[AUTONOMOUS_AI] %s: Skipped, insufficient data for the feature engine", index.symbol)
        return None

    # Deterministic hard gates -- checked before spending any LLM call, per
    # the design document's own "Hard Gating Rules in Python" stage and its
    # AutonomousOptionsAgent.evaluate_entry sample code specifically. ADX>=20
    # / VWAP / PDH-PDL proximity remain the model's own job, stated in
    # SYSTEM_PROMPT_ENTRY -- the document does not hard-gate those in Python
    # either, and this rebuild preserves that split.
    if features.session_phase in _ENTRY_BLOCKED_SESSION_PHASES:
        logger.info(
            "[AUTONOMOUS_AI] %s: Deterministic block -- session phase %s", index.symbol, features.session_phase
        )
        return None
    if features.adx is None or features.adx < _ADX_HARD_FLOOR:
        logger.info(
            "[AUTONOMOUS_AI] %s: Deterministic block -- ADX %s below %.0f floor",
            index.symbol, features.adx, _ADX_HARD_FLOOR,
        )
        return None

    user_prompt = _build_entry_prompt(features, index.display_name or index.symbol)
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


def check_autonomous_exits(
    db: Session,
    trade_manager,
    settings: AISettings,
    end_hm: tuple[int, int],
    features_by_index: Optional[dict[str, Optional[_Features]]] = None,
) -> None:
    """Re-asks the model, for every currently open AUTONOMOUS_AI trade whose
    deterministic exit matrix did not already fire, whether to HOLD or EXIT
    -- and actually closes the trade on EXIT via trade_manager.close_trade
    (the same helper monitor_open_trades' own backstop uses), so every exit
    path records a close identically. See module docstring's EXIT MATRIX
    section for the full deterministic sequence and why each rule is a
    distinct, separately-measurable ExitReason.

    features_by_index carries this cycle's already-computed feature set per
    index symbol (see run_autonomous_checks), reused here rather than
    recomputed, so the feature engine runs once per index per cycle
    regardless of how many of its consumers (entry evaluation, exit
    structural-invalidation check) need it. A missing or None entry means
    the structural-invalidation check is skipped for that trade this cycle
    (falls through to the model) rather than guessing."""
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
    end_minutes = end_hm[0] * 60 + end_hm[1]
    now_minutes = now_ist.hour * 60 + now_ist.minute
    minutes_to_close = max(end_minutes - now_minutes, 0)
    features_by_index = features_by_index or {}

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

            if minutes_to_close <= _SESSION_CLOSE_WARNING_MINUTES:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.AUTONOMOUS_SESSION_CLOSE)
                log_event(
                    db, "AUTONOMOUS_AI",
                    f"[{trade.strategy_name}] session-close warning -- {minutes_to_close} min to square-off",
                    payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent, "minutes_to_close": minutes_to_close},
                )
                logger.info(
                    "[AUTONOMOUS_AI] %s closed AUTONOMOUS_SESSION_CLOSE at %d min to square-off (%.2f%%)",
                    trade.trade_id, minutes_to_close, trade.pnl_percent or 0.0,
                )
                continue

            current_pnl_pct = trade.pnl_percent or 0.0
            peak_pnl_pct = _peak_pnl_percent(trade)

            if peak_pnl_pct >= _PEAK_GIVEBACK_ACTIVATE_PERCENT and (peak_pnl_pct - current_pnl_pct) >= _PEAK_GIVEBACK_WIDTH_PERCENT:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.AUTONOMOUS_TRAIL_EXIT)
                log_event(
                    db, "AUTONOMOUS_AI",
                    f"[{trade.strategy_name}] peak-giveback -- peak {peak_pnl_pct:.2f}%, now {current_pnl_pct:.2f}%",
                    payload={"trade_id": trade.trade_id, "peak_pnl_percent": peak_pnl_pct, "pnl_percent": current_pnl_pct},
                )
                logger.info(
                    "[AUTONOMOUS_AI] %s closed AUTONOMOUS_TRAIL_EXIT (peak %.2f%%, now %.2f%%)",
                    trade.trade_id, peak_pnl_pct, current_pnl_pct,
                )
                continue

            if peak_pnl_pct >= _BREAKEVEN_VIOLATION_ACTIVATE_PERCENT and current_pnl_pct <= _BREAKEVEN_VIOLATION_FLOOR_PERCENT:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.AUTONOMOUS_BREAKEVEN_EXIT)
                log_event(
                    db, "AUTONOMOUS_AI",
                    f"[{trade.strategy_name}] break-even violation -- peak {peak_pnl_pct:.2f}%, now {current_pnl_pct:.2f}%",
                    payload={"trade_id": trade.trade_id, "peak_pnl_percent": peak_pnl_pct, "pnl_percent": current_pnl_pct},
                )
                logger.info(
                    "[AUTONOMOUS_AI] %s closed AUTONOMOUS_BREAKEVEN_EXIT (peak %.2f%%, now %.2f%%)",
                    trade.trade_id, peak_pnl_pct, current_pnl_pct,
                )
                continue

            entry_time_ist = to_ist(trade.entry_time) if trade.entry_time is not None else None
            elapsed_minutes = (now_ist - entry_time_ist).total_seconds() / 60 if entry_time_ist else 0
            if elapsed_minutes >= _STALL_WINDOW_MINUTES and abs(current_pnl_pct) <= _STALL_BAND_PERCENT:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.AUTONOMOUS_STALL_EXIT)
                log_event(
                    db, "AUTONOMOUS_AI",
                    f"[{trade.strategy_name}] stalled -- {elapsed_minutes:.0f} min, {current_pnl_pct:.2f}%",
                    payload={"trade_id": trade.trade_id, "pnl_percent": current_pnl_pct, "elapsed_minutes": elapsed_minutes},
                )
                logger.info(
                    "[AUTONOMOUS_AI] %s closed AUTONOMOUS_STALL_EXIT after %.0f min at %.2f%%",
                    trade.trade_id, elapsed_minutes, current_pnl_pct,
                )
                continue

            features = features_by_index.get(trade.index_symbol)
            if features is not None and _structural_invalidation(trade, features):
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.AUTONOMOUS_STRUCTURAL_EXIT)
                log_event(
                    db, "AUTONOMOUS_AI",
                    f"[{trade.strategy_name}] structural invalidation -- spot {features.vwap_relation} contradicts {trade.option_type}",
                    payload={"trade_id": trade.trade_id, "pnl_percent": current_pnl_pct, "vwap_relation": features.vwap_relation},
                )
                logger.info(
                    "[AUTONOMOUS_AI] %s closed AUTONOMOUS_STRUCTURAL_EXIT (%s vs %s)",
                    trade.trade_id, features.vwap_relation, trade.option_type,
                )
                continue

            user_prompt = _build_exit_prompt(trade, features, now_ist)
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
                f"[{trade.strategy_name}] exit check: {decision.action}"
                + (f" ({decision.exit_rule})" if decision.exit_rule else ""),
                payload={
                    "trade_id": trade.trade_id, "reasoning": decision.reasoning,
                    "exit_rule": decision.exit_rule, "pnl_percent": trade.pnl_percent,
                },
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

        now_ist = to_ist(utc_now())
        indexes = [index for index in list_index_configs(session) if index.enabled]
        figures = {row["symbol"]: row for row in get_index_live_figures(session, smartapi, feed_store)}

        # Feature engine runs once per enabled index per cycle -- shared
        # between exit management (structural-invalidation check) and entry
        # evaluation below, rather than computed twice. See module
        # docstring's FEATURE ENGINE section.
        features_by_index: dict[str, Optional[_Features]] = {}
        for index in indexes:
            spot = (figures.get(index.symbol) or {}).get("price")
            if spot is None:
                features_by_index[index.symbol] = None
                continue
            try:
                features_by_index[index.symbol] = _compute_features(
                    session, index, spot, now_ist, smartapi, option_finder
                )
            except Exception:
                logger.exception("[AUTONOMOUS_AI] feature computation failed for %s", index.symbol)
                features_by_index[index.symbol] = None

        # Exits first: closing a stale position before considering a fresh
        # entry means a freed-up index slot can be re-entered the same cycle
        # rather than waiting a full 5 minutes.
        check_autonomous_exits(session, trade_manager, settings, _TRADING_END, features_by_index)

        platform_settings = get_or_create_settings(session)
        start_hm = parse_hhmm(platform_settings.trading_start_time, _DEFAULT_TRADING_START)
        if (now_ist.hour, now_ist.minute) < start_hm or (now_ist.hour, now_ist.minute) >= _TRADING_END:
            return

        for index in indexes:
            try:
                check_autonomous_entry(
                    session, index, features_by_index.get(index.symbol), now_ist, settings, smartapi, option_finder,
                )
            except Exception:
                logger.exception("[AUTONOMOUS_AI] entry check failed for %s", index.symbol)
    finally:
        if owns_session:
            session.close()
