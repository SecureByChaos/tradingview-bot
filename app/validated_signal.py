"""Validated Signal -- 5 Sep 2026 full rebuild to an external specification,
"Systematic Intraday Options Trading Engine: Morning & Afternoon Breakout
Specification," on an explicit instruction: "Change validated signal as
follow, you must follow everything said as it is." This REPLACES the
previous EMA_STACK/ST_ALIGNED/ORB_BREAK/PDH_PDL_BREAK 11:00-14:00 signal
(31 Aug 2026) entirely -- same posture as the Autonomous AI "without
judgement" rebuild and the Quick Scalp VWAP 2-sigma rebuild the same week:
implement the spec in full, and where this codebase's real architecture
cannot support something LITERALLY as worded, build the closest faithful
equivalent and name the substitution here rather than silently dropping it.

STRATEGY, IN ONE PARAGRAPH
----------------------------
Two independent breakout windows, both evaluated strictly on 5-minute SPOT
INDEX candles (never option premium): a Morning Trend Impulse (09:25-10:45
IST) breaking out of max(PDH, 10-min opening range) / min(PDL, opening
range), and an Afternoon Box Expansion (13:15-14:30 IST) breaking out of an
11:00-13:15 consolidation box, each gated on a volume surge over its own
20-bar average and a hard per-trade risk cap. Both size their target at a
strict 1:2 risk:reward off the SPOT stop distance. At most one position is
open across BOTH indices combined; the ENTIRE stop/target/stagnation/hard-
exit exit engine runs every 5 seconds against live SPOT LTP, not the option's
own premium -- the option is purely the execution vehicle.

NAMED DEVIATIONS -- WHAT'S BUILT DIFFERENTLY FROM THE LITERAL SPEC, AND WHY
-------------------------------------------------------------------------------
1. **Spot index candles report volume=0 in this pipeline** (the same wall
   BNV5.1/BNV6's own VWAP gate hits, and the same one app.quick_scalp and
   app.ai.autonomous already worked around) -- so every literal volume-surge
   check (`c_vol >= 1.5 * vol_sma20`) would be comparing 0 against 0, a
   trivially-satisfied no-op rather than a real filter. Volume is substituted
   from the near-month FUTIDX futures contract's own 5-minute candles
   (OptionFinder.find_current_futures_contract, aligned by timestamp onto the
   spot bars), the identical pattern those two modules already established,
   just at this module's own 5-minute resolution instead of their 1-minute
   one. A missing/unresolvable futures contract degrades to a volume of 0.0
   for that bar (never fabricated), which fails the surge gate closed rather
   than open.
2. **The volume SMA's own literal denominator is a bug, not a deliberate
   choice, and is not reproduced.** The spec's reference code computes
   `sum(c['volume'] for c in spot_candles[-21:-1]) / 20.0` -- a FIXED
   denominator of 20, regardless of how many prior bars actually exist. Early
   in Session 1 (before ~21 bars have accumulated since 09:15, which doesn't
   happen until session 1's own window is nearly over) this divides a
   partial sum by a denominator larger than the real sample, systematically
   UNDER-stating the average and making the surge gate spuriously easy to
   pass for most of the morning -- the opposite of what a volume filter is
   for. This build averages over however many prior bars actually exist
   (still capped at 20), and requires a minimum of _MIN_VOLUME_SMA_BARS
   before evaluating the gate at all, so it is a real average rather than an
   artificially deflated one.
3. **Section 3.2's stated "Conflict Resolution" rule (discard both signals if
   both fire on the same candle) is honoured over the reference code's own
   literal behaviour, which doesn't actually implement it** -- the reference
   `evaluate_intraday_signal` checks the bullish branch with a bare `if` that
   returns immediately, so if both were somehow true on the same candle it
   would silently return the bullish one, never reaching the bearish check to
   notice the conflict. This build checks both conditions before deciding,
   matching the prose rule directly. In fact UpperBoundary >= LowerBoundary
   is provably guaranteed by construction here (UpperBoundary = max(PDH,
   ORB_High) >= ORB_High >= ORB_Low >= min(PDL, ORB_Low) = LowerBoundary,
   since ORB_High/ORB_Low are themselves a max-of-highs/min-of-lows over the
   same two bars) -- so both conditions being true at once is structurally
   unreachable with these two boundaries specifically, not merely rare. The
   check is kept anyway as a direct, literal implementation of the spec's
   own stated rule, in case a future change to how the boundaries are
   computed ever makes it reachable. Session 2 has the identical property:
   Box_High >= Box_Low always, so close > Box_High and close < Box_Low can
   never both be true either.
4. **The stop/target/stagnation exit engine's data is genuinely SPOT, not a
   percent-of-premium rescale like every other strategy in this codebase.**
   `StrategyTrade.structural_stop_level` / `structural_target_level` hold the
   real spot_sl / spot_target index price levels; `stoploss` / `target`
   (option premium fields) are set to deliberately unreachable sentinels so
   app.multi_strategy's shared 30-second monitor_open_trades never preempts
   this module's own 5-second poll with an unrelated premium-based check --
   same sentinel technique app.quick_scalp's Runner leg already established
   (`entry_price * 5`), just needed on both sides here since NEITHER field
   carries a real number in this build. The shared monitor still updates
   current_premium/highest_price/lowest_price/ticks every 30s for reporting
   -- only its STOPLOSS/TARGET decision is defused.
5. **VALIDATED_SIGNAL was removed from app.multi_strategy's
   _GIVEBACK_STOP_ORIGINS.** That 2-week live trial (3 Sep 2026) was scoped
   to origins with "zero trailing/discretionary protection today" -- true of
   the superseded fixed-12%/20% build, no longer true of this one, which now
   has its own complete, spec-driven exit engine. Leaving it in scope would
   let a premium-based mechanism the new spec never mentions silently
   override the new engine's spot-level exits on a subset of trades.
6. **"Immediate market order to exit" resolves to a fetched LTP fill**, same
   as every other paper strategy in this codebase -- no live order-placement
   path exists here at all (see STRUCTURALLY PAPER-ONLY below).
7. **Sensex is out of scope.** The spec's own title and every numeric
   constant in it (buffers, risk caps, box widths) are stated only for
   "Nifty 50 and Bank Nifty" -- this build evaluates exactly those two index
   symbols and no others, regardless of what else is enabled in Settings.

STRUCTURALLY PAPER-ONLY
-------------------------
Same construction as every other experimental strategy in this project: mode
is hardcoded to TradingMode.PAPER, smartapi.place_market_order is never
called anywhere in this module.

ISOLATION
----------
origin="VALIDATED_SIGNAL" (unchanged from the superseded build -- this
replaces the STRATEGY's logic, not its identity, so existing trade history
and the /validated-signal page keep working against the same population).
Single Active Position Rule (spec Section 1): at most ONE open trade across
BOTH indices combined, not per-index like the superseded build -- checked
across the whole ORIGIN population, not scoped to one index_symbol.

WHERE THIS RUNS
-----------------
Two independent scheduler jobs, not one -- the spec's own cadence needs
genuinely differ:
- "validated-signal-entry-check" -- 5-minute cron (app.scheduler), matching
  the spec's own 5-minute spot-candle cadence for setup/trigger evaluation.
  No longer hooked into AI Origination's cycle (the superseded build's own
  design) -- the Single Active Position Rule needs to see BOTH indices at
  once per cycle to resolve a concurrent-signal tie-break, which a per-index
  hook inside another module's own per-index loop cannot do cleanly.
- "validated-signal-exit-check" -- a genuinely new 5-SECOND IntervalTrigger,
  the fastest job in this codebase (faster even than the existing 30-second
  shared monitor), matching the spec's own explicit "5-second polling loop"
  requirement for the stop/target/stagnation/hard-exit checks. Gated only on
  trading_day_reason() (weekday/holiday), not hour-of-day -- same reasoning
  as the shared monitor's own trade-monitor job: it must keep running through
  the entire trading day, including right up to and past either hard-exit
  time, to catch a position rather than going silent on it. Returns
  immediately with zero SmartAPI calls whenever no VALIDATED_SIGNAL trade is
  open (the common case), so its 5-second cadence costs nothing when idle.

WHAT IS NOT VALIDATED HERE, STATED PLAINLY
---------------------------------------------
Every threshold in this module (buffers, risk caps, box widths, volume
multipliers, the 20-minute stagnation window) is the external spec's own
number, not something this project's own backtesting has produced or
confirmed. This module's own real results are the only way to find out
whether this construction works -- read every number it produces with the
same "not yet enough evidence" standard this project applies to every other
new, unbacktested mechanism.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time as dtime, timedelta
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.db_models import IndexConfig, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_context import compute_levels
from app.market_data import Bar, FIVE_MINUTE, load_bars, parse_smartapi_row, store_bars
from app.models import ExitReason, Signal
from app.option_finder import OptionFinder
from app.platform import list_index_configs, log_event
from app.signal_validation import check_market_hours, trading_day_reason
from app.smartapi_client import SmartAPIClient
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)

ORIGIN = "VALIDATED_SIGNAL"

# ---------------------------------------------------------------------------
# Section 2 -- Trading Windows & Market Regimes
# ---------------------------------------------------------------------------

_ORB_END = dtime(9, 25)              # 09:15-09:25: Range Formation, ORB bars
_SESSION1_START = dtime(9, 25)
_SESSION1_END = dtime(10, 45)
_MORNING_HARD_EXIT_TIME = dtime(11, 15)
_MORNING_HARD_EXIT_HM = (11, 15)
_BOX_START = dtime(11, 0)
_BOX_END = dtime(13, 15)
_SESSION2_START = dtime(13, 15)
_SESSION2_END = dtime(14, 30)
_AFTERNOON_HARD_EXIT_HM = (15, 10)

# ---------------------------------------------------------------------------
# Sections 3/4 -- per-index constants (Nifty 50 / Bank Nifty only -- see
# module docstring's NAMED DEVIATIONS #7)
# ---------------------------------------------------------------------------

_BUFFER_POINTS = {"NIFTY": 2.0, "BANKNIFTY": 10.0}
_SESSION1_MAX_RISK = {"NIFTY": 16.0, "BANKNIFTY": 55.0}
_SESSION2_MAX_RISK = {"NIFTY": 18.0, "BANKNIFTY": 65.0}
_BOX_MAX_WIDTH = {"NIFTY": 45.0, "BANKNIFTY": 160.0}
_SESSION1_VOLUME_MULTIPLIER = 1.5
_SESSION2_VOLUME_MULTIPLIER = 1.3
_RR_MULTIPLE = 2.0

# See module docstring's NAMED DEVIATIONS #2 -- a real average over however
# many prior bars exist (capped at 20), not the reference code's own
# fixed-denominator-of-20 division.
_MIN_VOLUME_SMA_BARS = 5

# ---------------------------------------------------------------------------
# Section 5 -- Trade Management & Exit Engine
# ---------------------------------------------------------------------------

_STAGNATION_MINUTES = 20.0
_STAGNATION_RISK_MULTIPLE = 1.0

# ---------------------------------------------------------------------------
# Sentinel premium levels -- see module docstring's NAMED DEVIATIONS #4.
# ---------------------------------------------------------------------------

_SENTINEL_STOP_FACTOR = 0.01
_SENTINEL_TARGET_FACTOR = 100.0

_SUPPORTED_INDEXES = frozenset({"NIFTY", "BANKNIFTY"})
_CANDLE_LOOKBACK_DAYS = 7
_FUTURES_CANDLE_SUFFIX = "_FUT"


@dataclass(frozen=True)
class _SignalCandidate:
    """One fully-qualified entry candidate from evaluate_intraday_signal --
    a direct structural analogue of the spec's own reference dict, kept as a
    typed dataclass since this module (unlike the spec's standalone
    snippet) has real downstream consumers that need typed fields."""

    action: str            # "BUY_CE" / "BUY_PE"
    session: str            # "MORNING_IMPULSE" / "AFTERNOON_EXPANSION"
    spot_entry: float
    spot_sl: float
    spot_target: float
    hard_exit_hm: tuple[int, int]
    volume_ratio: float     # trigger-candle volume / its own 20-bar SMA --
                             # the spec's own Section 1 cross-index tie-break


def select_itm_strike(spot_price: float, action: str, index_name: str) -> int:
    """Verbatim from the external spec (Section 6), including its own
    asymmetric CE/PE rounding -- preserved exactly per the "follow
    everything said as it is" instruction rather than simplified or
    "corrected". index_name is this app's IndexConfig.symbol ("NIFTY" /
    "BANKNIFTY"), matching the spec's own literal string check."""
    strike_step = 50 if index_name == "NIFTY" else 100

    if action == "BUY_CE":
        atm_strike = round(spot_price / strike_step) * strike_step
        return int(atm_strike - strike_step if spot_price >= atm_strike else atm_strike - (2 * strike_step))
    if action == "BUY_PE":
        atm_strike = round(spot_price / strike_step) * strike_step
        return int(atm_strike + strike_step if spot_price <= atm_strike else atm_strike + (2 * strike_step))
    raise ValueError(f"Unknown action: {action}")


def _volume_sma(volumes: list[float], index: int) -> Optional[float]:
    """Average volume of up to 20 bars preceding `index` -- see module
    docstring's NAMED DEVIATIONS #2 for why this isn't the reference code's
    own fixed-denominator-of-20 division. None (fail closed) below
    _MIN_VOLUME_SMA_BARS of history."""
    preceding = volumes[max(0, index - 20):index]
    if len(preceding) < _MIN_VOLUME_SMA_BARS:
        return None
    return sum(preceding) / len(preceding)


def _orb_levels(today_bars: list[Bar]) -> Optional[tuple[float, float]]:
    """Section 3.1's 10-minute ORB -- the highest high / lowest low of
    exactly the 09:15 and 09:20 5-minute bars. None until both exist."""
    orb_bars = [b for b in today_bars if dtime(9, 15) <= b.ts_ist.time() < _ORB_END]
    if len(orb_bars) < 2:
        return None
    return max(b.high for b in orb_bars), min(b.low for b in orb_bars)


def _box_levels(today_bars: list[Bar]) -> Optional[tuple[float, float]]:
    """Section 4.1's 11:00-13:15 consolidation box. None if no bars have
    landed in that window yet (matches the reference code's own bare
    `if not midday_candles` check -- no stronger minimum-count floor, unlike
    the volume SMA, since this has no fixed-denominator bug to guard
    against)."""
    box_bars = [b for b in today_bars if _BOX_START <= b.ts_ist.time() < _BOX_END]
    if not box_bars:
        return None
    return max(b.high for b in box_bars), min(b.low for b in box_bars)


def _evaluate_session1(
    today_bars: list[Bar], volumes: list[float], pdh: Optional[float], pdl: Optional[float], index_symbol: str,
) -> Optional[_SignalCandidate]:
    """Section 3's Morning Trend Impulse, evaluated against the most recent
    (last) bar in today_bars as the trigger candle."""
    if pdh is None or pdl is None:
        return None
    orb = _orb_levels(today_bars)
    if orb is None:
        return None
    orb_high, orb_low = orb
    upper = max(pdh, orb_high)
    lower = min(pdl, orb_low)

    idx = len(today_bars) - 1
    c = today_bars[idx]
    vol_sma = _volume_sma(volumes, idx)
    if vol_sma is None or vol_sma <= 0:
        return None
    c_vol = volumes[idx]
    if c_vol < _SESSION1_VOLUME_MULTIPLIER * vol_sma:
        return None

    bullish = c.close > upper and c.open < upper
    bearish = c.close < lower and c.open > lower
    if bullish and bearish:
        # Section 3.2's Conflict Resolution -- see NAMED DEVIATIONS #3.
        return None

    buffer = _BUFFER_POINTS[index_symbol]
    max_risk = _SESSION1_MAX_RISK[index_symbol]
    volume_ratio = c_vol / vol_sma

    if bullish:
        sl = c.low - buffer
        risk = c.close - sl
        if risk > max_risk:
            return None
        return _SignalCandidate(
            "BUY_CE", "MORNING_IMPULSE", c.close, sl, c.close + _RR_MULTIPLE * risk,
            _MORNING_HARD_EXIT_HM, volume_ratio,
        )
    if bearish:
        sl = c.high + buffer
        risk = sl - c.close
        if risk > max_risk:
            return None
        return _SignalCandidate(
            "BUY_PE", "MORNING_IMPULSE", c.close, sl, c.close - _RR_MULTIPLE * risk,
            _MORNING_HARD_EXIT_HM, volume_ratio,
        )
    return None


def _evaluate_session2(
    today_bars: list[Bar], volumes: list[float], index_symbol: str,
) -> Optional[_SignalCandidate]:
    """Section 4's Afternoon Box Expansion, evaluated against the most
    recent (last) bar in today_bars as the trigger candle."""
    box = _box_levels(today_bars)
    if box is None:
        return None
    box_high, box_low = box
    if (box_high - box_low) > _BOX_MAX_WIDTH[index_symbol]:
        # Compression pre-condition failed -- Session 2 disabled for the day.
        # Stateless: recomputed fresh every cycle from the same stored bars,
        # so no separate "disabled today" flag is needed to make this stick.
        return None

    idx = len(today_bars) - 1
    c = today_bars[idx]
    vol_sma = _volume_sma(volumes, idx)
    if vol_sma is None or vol_sma <= 0:
        return None
    c_vol = volumes[idx]
    if c_vol < _SESSION2_VOLUME_MULTIPLIER * vol_sma:
        return None

    buffer = _BUFFER_POINTS[index_symbol]
    max_risk = _SESSION2_MAX_RISK[index_symbol]
    volume_ratio = c_vol / vol_sma

    if c.close > box_high and c.open <= box_high:
        sl = c.low - buffer
        risk = c.close - sl
        if risk > max_risk:
            return None
        return _SignalCandidate(
            "BUY_CE", "AFTERNOON_EXPANSION", c.close, sl, c.close + _RR_MULTIPLE * risk,
            _AFTERNOON_HARD_EXIT_HM, volume_ratio,
        )
    if c.close < box_low and c.open >= box_low:
        sl = c.high + buffer
        risk = sl - c.close
        if risk > max_risk:
            return None
        return _SignalCandidate(
            "BUY_PE", "AFTERNOON_EXPANSION", c.close, sl, c.close - _RR_MULTIPLE * risk,
            _AFTERNOON_HARD_EXIT_HM, volume_ratio,
        )
    return None


def evaluate_intraday_signal(
    today_bars: list[Bar],
    volumes: list[float],
    pdh: Optional[float],
    pdl: Optional[float],
    now_ist,
    index_symbol: str,
) -> Optional[_SignalCandidate]:
    """Section 7's reference evaluator, adapted to this codebase's real Bar
    history and a genuinely-volume-substituted `volumes` list (see module
    docstring's NAMED DEVIATIONS #1) instead of the spec's own literal
    spot-candle dicts carrying real volume it assumed would exist. `volumes`
    must be index-aligned 1:1 with today_bars. Fails closed (None) on empty
    or misaligned input, outside both entry windows, or an unsupported
    index_symbol."""
    if not today_bars or len(today_bars) != len(volumes) or index_symbol not in _SUPPORTED_INDEXES:
        return None
    now = now_ist.time()
    if _SESSION1_START <= now <= _SESSION1_END:
        return _evaluate_session1(today_bars, volumes, pdh, pdl, index_symbol)
    if _SESSION2_START <= now <= _SESSION2_END:
        return _evaluate_session2(today_bars, volumes, index_symbol)
    return None


def _futures_volume_by_5min(
    db: Session, index: IndexConfig, option_finder: OptionFinder, smartapi: SmartAPIClient, now_ist,
) -> dict:
    """Real per-5-minute volume from the near-month futures contract, keyed
    by naive-IST bar-open timestamp -- see module docstring's NAMED
    DEVIATIONS #1. Returns {} (every bar then falls back to a volume of 0.0,
    which fails every surge gate closed) when no futures contract can be
    resolved."""
    try:
        contract = option_finder.find_current_futures_contract(index)
    except Exception as exc:
        logger.info("[VALIDATED_SIGNAL] %s: futures contract lookup failed (%s)", index.symbol, exc)
        return {}
    if contract is None:
        return {}
    futures_key = f"{index.symbol}{_FUTURES_CANDLE_SUFFIX}"
    try:
        rows = smartapi.get_candles(
            exchange=contract["exchange"],
            symboltoken=contract["symboltoken"],
            interval=FIVE_MINUTE,
            from_dt=now_ist.strftime("%Y-%m-%d 09:15"),
            to_dt=now_ist.strftime("%Y-%m-%d %H:%M"),
        )
        if rows:
            store_bars(db, futures_key, FIVE_MINUTE, [parse_smartapi_row(row) for row in rows])
    except Exception as exc:
        logger.info("[VALIDATED_SIGNAL] %s: futures candle refresh failed (%s)", index.symbol, exc)
    bars = load_bars(db, futures_key, FIVE_MINUTE)
    return {b.ts_ist: b.volume for b in bars if b.ts_ist.date() == now_ist.date() and b.volume}


def _load_index_features(
    db: Session, index: IndexConfig, smartapi: SmartAPIClient, option_finder: OptionFinder, now_ist,
) -> tuple[list[Bar], list[float], Optional[float], Optional[float], bool]:
    """Returns (session_bars, volumes, pdh, pdl, refresh_failed).
    session_bars/volumes are TODAY's 5-min spot bars only, index-aligned 1:1.
    refresh_failed True halts new-entry evaluation for this cycle (same
    convention as every other live-candle-driven strategy in this
    codebase)."""
    refresh_failed = False
    if index.spot_token:
        try:
            rows = smartapi.get_candles(
                exchange=index.spot_exchange,
                symboltoken=index.spot_token,
                interval=FIVE_MINUTE,
                from_dt=(now_ist - timedelta(days=_CANDLE_LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M"),
                to_dt=now_ist.strftime("%Y-%m-%d %H:%M"),
            )
            if rows:
                store_bars(db, index.symbol, FIVE_MINUTE, [parse_smartapi_row(row) for row in rows])
        except Exception as exc:
            refresh_failed = True
            logger.info("[VALIDATED_SIGNAL] %s: spot candle refresh failed (%s), using stored history", index.symbol, exc)

    all_bars = load_bars(db, index.symbol, FIVE_MINUTE, limit=2500)
    levels = compute_levels(all_bars, now_ist.date())
    session_bars = [b for b in all_bars if b.ts_ist.date() == now_ist.date()]

    volume_by_minute = _futures_volume_by_5min(db, index, option_finder, smartapi, now_ist)
    volumes = [volume_by_minute.get(b.ts_ist, 0.0) for b in session_bars]

    return session_bars, volumes, levels.previous_day_high, levels.previous_day_low, refresh_failed


def _has_open_trade_anywhere(db: Session) -> bool:
    """Section 1's Single Active Position Rule: at most 1 trade across BOTH
    indices combined -- not per-index like the superseded build."""
    return (
        db.scalar(
            select(StrategyTrade.id)
            .where(StrategyTrade.origin == ORIGIN, StrategyTrade.status == TradeStatus.OPEN)
            .limit(1)
        )
        is not None
    )


def open_validated_trade(
    db: Session,
    index: IndexConfig,
    candidate: _SignalCandidate,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
) -> Optional[StrategyTrade]:
    """Resolves a 1-strike-ITM contract (Section 6) at the trigger candle's
    close and opens one StrategyTrade with SPOT-level stop/target stored on
    structural_stop_level/structural_target_level -- see module docstring's
    NAMED DEVIATIONS #4 for why the premium stoploss/target fields are
    unreachable sentinels instead."""
    strike = select_itm_strike(candidate.spot_entry, candidate.action, index.symbol)
    trade_signal = Signal.BUY_CE if candidate.action == "BUY_CE" else Signal.BUY_PE

    try:
        contract = option_finder.find_contract_at_strike(trade_signal, index, strike, min_dte=0)
    except Exception as exc:
        logger.info(
            "[VALIDATED_SIGNAL] %s: Skipped, could not resolve contract at strike %s (%s)",
            index.symbol, strike, exc,
        )
        return None

    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[VALIDATED_SIGNAL] %s: Skipped, could not resolve price (%s)", index.symbol, exc)
        return None
    if not entry_price:
        logger.info("[VALIDATED_SIGNAL] %s: Skipped, LTP came back empty", index.symbol)
        return None

    session_label = candidate.session.replace("_", " ").title()
    strategy_name = f"Validated Signal - {index.display_name or index.symbol} ({session_label})"
    reasoning = (
        f"{session_label}: spot entry {candidate.spot_entry:.2f}, spot stop {candidate.spot_sl:.2f}, "
        f"spot target {candidate.spot_target:.2f} (strict 1:2 R:R), trigger-candle volume "
        f"{candidate.volume_ratio:.2f}x its own 20-bar SMA. Deterministic entry -- no model call "
        "was made for this trade."
    )

    trade = StrategyTrade(
        trade_id=uuid4().hex,
        strategy_name=strategy_name,
        signal=trade_signal.value,
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
        # Deliberately unreachable -- see module docstring's NAMED
        # DEVIATIONS #4. Every real stop/target decision here is a SPOT
        # level (structural_stop_level/structural_target_level below),
        # checked by this module's own 5-second exit poll.
        stoploss=round(entry_price * _SENTINEL_STOP_FACTOR, 2),
        target=round(entry_price * _SENTINEL_TARGET_FACTOR, 2),
        entry_time=utc_now(),
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        highest_price=round(entry_price, 2),
        lowest_price=round(entry_price, 2),
        trailing_active=False,
        sl_mode=SLMode.FIXED,
        origin=ORIGIN,
        ai_action=candidate.action,
        ai_reasoning=reasoning,
        spot_at_entry=round(candidate.spot_entry, 2),
        structural_stop_level=round(candidate.spot_sl, 2),
        structural_target_level=round(candidate.spot_target, 2),
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    log_event(
        db, "VALIDATED_SIGNAL",
        f"[{strategy_name}] opened {trade_signal.value} @ strike {trade.strike} (spot {candidate.spot_entry:.2f})",
        payload={
            "trade_id": trade.trade_id,
            "spot_sl": candidate.spot_sl,
            "spot_target": candidate.spot_target,
            "volume_ratio": candidate.volume_ratio,
        },
    )
    logger.info(
        "[VALIDATED_SIGNAL] %s opened %s for %s (session=%s, strike=%s)",
        ORIGIN, trade_signal.value, index.symbol, candidate.session, contract.strike,
    )
    return trade


def check_validated_signal_exits(
    db: Session,
    trade_manager,
    smartapi: SmartAPIClient,
    now_ist,
) -> None:
    """Section 5's 4-condition exit engine, checked in the spec's own order
    (spot stop, spot target, 20-minute stagnation, hard session stop) for
    every open VALIDATED_SIGNAL trade, against live spot LTP. Returns
    immediately with zero SmartAPI calls when there is nothing open."""
    trades = list(
        db.scalars(
            select(StrategyTrade).where(StrategyTrade.status == TradeStatus.OPEN, StrategyTrade.origin == ORIGIN)
        )
    )
    if not trades:
        return
    indexes_by_symbol = {index.symbol: index for index in list_index_configs(db)}

    for trade in trades:
        try:
            index = indexes_by_symbol.get(trade.index_symbol)
            if (
                index is None
                or trade.structural_stop_level is None
                or trade.structural_target_level is None
                or trade.spot_at_entry is None
            ):
                continue
            entry_ist = to_ist(trade.entry_time)
            if entry_ist is None:
                continue

            try:
                current_spot = smartapi.get_index_spot(index)
            except Exception as exc:
                logger.info("[VALIDATED_SIGNAL] %s: spot LTP fetch failed this poll (%s)", trade.trade_id, exc)
                continue
            if current_spot is None:
                continue

            is_ce = trade.option_type == "CE"
            reason: Optional[ExitReason] = None

            if (is_ce and current_spot <= trade.structural_stop_level) or (
                not is_ce and current_spot >= trade.structural_stop_level
            ):
                reason = ExitReason.VS_SPOT_STOP
            elif (is_ce and current_spot >= trade.structural_target_level) or (
                not is_ce and current_spot <= trade.structural_target_level
            ):
                reason = ExitReason.VS_SPOT_TARGET
            else:
                elapsed_minutes = (now_ist - entry_ist).total_seconds() / 60.0
                risk = abs(trade.spot_at_entry - trade.structural_stop_level)
                favorable_move = (
                    (current_spot - trade.spot_at_entry) if is_ce else (trade.spot_at_entry - current_spot)
                )
                if elapsed_minutes >= _STAGNATION_MINUTES and favorable_move < _STAGNATION_RISK_MULTIPLE * risk:
                    reason = ExitReason.VS_STAGNATION_EXIT
                else:
                    hard_exit_hm = (
                        _MORNING_HARD_EXIT_HM if entry_ist.time() < _MORNING_HARD_EXIT_TIME else _AFTERNOON_HARD_EXIT_HM
                    )
                    if (now_ist.hour, now_ist.minute) >= hard_exit_hm:
                        reason = ExitReason.TIME_EXIT

            if reason is None:
                continue

            try:
                exit_price = smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            except Exception:
                exit_price = trade.current_premium
            if not exit_price:
                exit_price = trade.current_premium
            if not exit_price:
                continue

            trade_manager.close_trade(db, trade, exit_price, reason)
            log_event(
                db, "VALIDATED_SIGNAL",
                f"[{trade.strategy_name}] closed {reason.value} -- spot {current_spot:.2f} "
                f"(stop {trade.structural_stop_level:.2f}, target {trade.structural_target_level:.2f})",
                payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
            )
            logger.info("[VALIDATED_SIGNAL] %s closed %s at spot %.2f", trade.trade_id, reason.value, current_spot)
        except Exception:
            logger.exception("[VALIDATED_SIGNAL] exit check failed for trade %s", trade.trade_id)


def run_validated_signal_entry_checks(
    smartapi: Optional[SmartAPIClient] = None,
    option_finder: Optional[OptionFinder] = None,
    db=None,
) -> None:
    """Scheduler entry point (see app.scheduler's "validated-signal-entry-
    check" job, 5-minute cron). Owns its own DB session when called from the
    scheduler; accepts an existing session in tests."""
    if smartapi is None or option_finder is None:
        logger.info("[VALIDATED_SIGNAL] Skipped: no smartapi/option_finder available in this context")
        return
    closed_reason = check_market_hours(utc_now())
    if closed_reason is not None:
        logger.info("[VALIDATED_SIGNAL] Cycle skipped -- %s", closed_reason.replace("Signal received ", "", 1))
        return

    owns_session = db is None
    session = db or SessionLocal()
    try:
        now_ist = to_ist(utc_now())
        now = now_ist.time()
        in_session1 = _SESSION1_START <= now <= _SESSION1_END
        in_session2 = _SESSION2_START <= now <= _SESSION2_END
        if not (in_session1 or in_session2):
            # Outside both entry windows (Range Formation, the Dead Zone, or
            # past 14:30) -- nothing to evaluate. Skipping the candle refresh
            # entirely here saves real SmartAPI budget across ~3 idle hours a
            # day; box/ORB levels are rebuilt fresh from stored history the
            # moment a window opens again, so nothing is lost by not
            # refreshing during the gap.
            return
        if _has_open_trade_anywhere(session):
            return  # Single Active Position Rule -- see module docstring

        indexes = [index for index in list_index_configs(session) if index.enabled and index.symbol in _SUPPORTED_INDEXES]
        candidates: list[tuple[IndexConfig, _SignalCandidate]] = []
        for index in indexes:
            try:
                session_bars, volumes, pdh, pdl, refresh_failed = _load_index_features(
                    session, index, smartapi, option_finder, now_ist
                )
                if refresh_failed:
                    logger.info("[VALIDATED_SIGNAL] %s: data refresh failed this cycle -- halting new signals", index.symbol)
                    continue
                candidate = evaluate_intraday_signal(session_bars, volumes, pdh, pdl, now_ist, index.symbol)
            except Exception:
                logger.exception("[VALIDATED_SIGNAL] entry check failed for %s", index.symbol)
                continue
            if candidate is not None:
                candidates.append((index, candidate))

        if not candidates:
            return

        # Section 1's cross-index tie-break: highest trigger-candle volume
        # relative to its own 20-period Volume SMA.
        index, candidate = max(candidates, key=lambda pair: pair[1].volume_ratio)
        if len(candidates) > 1:
            logger.info(
                "[VALIDATED_SIGNAL] Concurrent signals on %s -- selected %s (volume ratio %.2fx, highest of %d)",
                ",".join(i.symbol for i, _ in candidates), index.symbol, candidate.volume_ratio, len(candidates),
            )
        open_validated_trade(session, index, candidate, smartapi, option_finder)
    finally:
        if owns_session:
            session.close()


def run_validated_signal_exit_checks(
    smartapi: Optional[SmartAPIClient] = None,
    trade_manager=None,
    db=None,
) -> None:
    """Scheduler entry point (see app.scheduler's "validated-signal-exit-
    check" job, 5-second interval). Day-only gate (weekday/holiday, no
    hour-of-day component) -- same reasoning as the shared 30s monitor's own
    trade-monitor job: this must keep running through the whole trading day
    to catch a position right up to and past either hard-exit time."""
    if smartapi is None or trade_manager is None:
        return
    if trading_day_reason(to_ist(utc_now())) is not None:
        return

    owns_session = db is None
    session = db or SessionLocal()
    try:
        now_ist = to_ist(utc_now())
        check_validated_signal_exits(session, trade_manager, smartapi, now_ist)
    finally:
        if owns_session:
            session.close()
