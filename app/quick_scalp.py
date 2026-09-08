"""Quick Scalp -- 8 Sep 2026 rebuild. Replaces the 4 Sep VWAP 2-sigma
mean-reversion build entirely, on an explicit instruction: pasting a full
spec titled "NIFTY 50 VWAP 2sigma Scalp Engine -- Comprehensive SmartAPI
Production Spec" and choosing, when asked, "Replace Quick Scalp with it
(full rebuild)" -- including the spec's own WebSocket-based engine, not just
its risk-mechanism changes. Every rule in that spec is implemented below.
Where this codebase's real architecture cannot support something LITERALLY
as worded, the closest faithful equivalent is built and the substitution is
named here, not silently dropped -- the same discipline every prior
"build exactly as spec'd" task in this project has followed.

STRATEGY, IN ONE PARAGRAPH
----------------------------
Unchanged from the 4 Sep build's own core signal: on each completed
1-minute bar of the underlying index, track a session VWAP and its 2-sigma
bands. A bar (`C0`) that pierces a band, closes back inside it, leaves a
rejection wick covering >=30% of its range, and has RSI(7) confirming
exhaustion (<30 for a lower-band piercing, >70 for an upper-band one) is a
candidate reversal. If the VERY NEXT completed bar (`C1`) then trades back
through C0's opposite extreme, that is the entry trigger. What changed is
everything downstream of that: how a completed bar reaches this check (real
WS tick aggregation instead of REST polling), and the risk construction once
a position opens (single-clip full exit at a percentage stop/target instead
of a 50/50 Target1/Runner split at flat option points).

WHAT'S GENUINELY NEW IN THIS REBUILD
----------------------------------------
1. **Real WebSocket tick aggregation, not REST polling.** app.quick_scalp_
   feed.QuickScalpFeed is a new, persistent background WS connection
   (mirroring app.live_feed.IndexFeed's own shape) that finalizes each
   1-minute bar the instant a tick's minute rolls over, not up to 60+
   seconds later on the next scheduler firing. This directly addresses the
   spec's own stated bottleneck #1 ("false-breakout chop" from lagging
   indicators/stale bars). See that module's own docstring for exactly what
   it does and does not do, and for the real, named resource cost of adding
   a second persistent WS connection to an already memory-constrained box.
2. **Single-clip full exit, no more Target1/Runner leg split.** One
   StrategyTrade row per signal now, not two. `_sibling_trade_id`, the 50%
   lot-fraction math, and the VWAP-cross Runner-leg exit are all gone --
   ExitReason.SCALP_VWAP_TARGET is no longer produced by any live code path
   (see app/models.py's own updated comment on that value).
3. **Percentage-based stop/target, not flat option points.** Stop is a flat
   -2.5% of entry premium; target is the midpoint of the spec's own stated
   "+3.5% to +4.0%" range, floored at the spec's own stated minimum +Rs12
   premium points -- `max(entry * 3.75%, entry + 12)`.
4. **The hard time-stop now branches on P&L, not force-closing
   unconditionally.** At the spec's own 3-completed-bar (180s) mark: if the
   position is already profitable enough to cover round-trip costs (current
   premium >= entry + Rs2, the spec's own cost-buffer number), the stop is
   moved up to that breakeven-plus-buffer level and the trade is left open
   to run toward its real target -- it is NOT force-closed. Only a flat/
   negative position is closed at this mark (a "scratch"). This is a real,
   material behavioural change from the 4 Sep build, whose SCALP_TIME_STOP
   force-closed the WHOLE position at 3 minutes regardless of P&L -- the
   exact mechanism that closed two of 8 Sep's four Quick Scalp legs at a net
   loss despite a small positive move, which is part of what prompted this
   rebuild. ExitReason.SCALP_TIME_STOP is KEPT (same value), but its meaning
   narrows to "closed at the 3-minute mark because it was NOT yet
   profitable" -- see app/models.py's updated comment on that value.

NAMED DEVIATIONS -- WHAT'S STILL BUILT DIFFERENTLY FROM THE LITERAL SPEC
-------------------------------------------------------------------------------
1. **Position-level monitoring (stop/target/breakeven-trail-or-scratch) is
   NOT a second, dynamically-resubscribing option-contract WS channel.** The
   spec's own `on_option_tick` design subscribes/unsubscribes per open
   position; this module keeps that decision on the existing fast-poll
   scheduler job instead (`quick-scalp-exit-check`, 5-second IntervalTrigger
   -- see app/scheduler.py), matching app.validated_signal's own already-
   established precedent for the identical tradeoff (continuous per-position
   WS monitoring scoped out there too, in favour of a fast poll this project
   already trusts). See app.quick_scalp_feed's own docstring for the full
   reasoning -- the option-premium stop/target themselves are additionally,
   independently enforced faster still by the existing shared 30-second
   monitor_open_trades tick via trade.stoploss/trade.target, unchanged from
   the 4 Sep build's own equivalent reasoning.
2. **Deep ITM delta (~0.65-0.75) is still approximated by a fixed point
   offset, not computed** -- unchanged from the 4 Sep build; this codebase
   has no live per-contract Greeks feed.
3. **"Fire market order" / "limit order with a 2-point marketable buffer"
   both still resolve to a fetched LTP fill.** No synthetic slippage, no
   live order-placement path exists anywhere in this module (see
   "STRUCTURALLY PAPER-ONLY" below).
4. **"Hard broker-level SL-M order" is still NOT a real order sent to Angel
   One.** The safety PROPERTY (a fast, deterministic, unconditional premium
   stop) is real -- trade.stoploss, enforced by the existing shared
   30-second monitor on its own independent SmartAPI path. The TRANSPORT (a
   genuine broker order) is not, because this module is structurally
   forbidden from placing one, unchanged from every prior build in this
   project.
5. **The structural (index-level) stop is kept even though the new spec's
   own sample code doesn't implement it**, despite the spec's risk TABLE
   stating "Stop Loss: -2.5% or structural bar invalidation" -- the table is
   the authoritative requirement here, the sample code is simply incomplete
   against its own stated rule (same class of gap Validated Signal's own
   rebuild found and resolved the same way: honour the written rule, not an
   incomplete reference implementation). The existing 4 Sep build's
   structural-stop construction (C0's rejection-bar extreme +-1pt, capped at
   14 points from the trigger price) is kept unchanged -- the new spec is
   silent on this cap, not opposed to it, and dropping an existing risk
   bound on silence alone would loosen risk, the opposite of this spec's own
   stated intent.
6. **"Armed state" still needs no persisted flag** -- same reasoning as the
   4 Sep build: `vwap_scalp_action` re-evaluates the C0/C1 pair fresh every
   time it's called, which is now itself real-time (triggered by a bar-close
   callback) rather than periodic, tightening rather than weakening this
   equivalence.
7. **A WS feed outage pauses NEW entries, not existing position
   management.** If app.quick_scalp_feed's connection drops, no new bars
   get persisted and no bar-close callbacks fire, so no new signals are
   detected until it reconnects -- same fail-soft philosophy as
   app.live_feed.IndexFeed ("a feed problem degrades... never blocks
   trading"). Any already-open Quick Scalp position is COMPLETELY
   unaffected: its stop/target/structural/time-stop checks all run on the
   independent scheduler-poll path above, which calls SmartAPI directly and
   has no dependency on this feed at all.

STRUCTURALLY PAPER-ONLY
-------------------------
Unchanged: mode is hardcoded to TradingMode.PAPER, smartapi.
place_market_order is never called anywhere in this module.

ISOLATION
----------
origin="QUICK_SCALP" (unchanged -- this replaces the STRATEGY's logic, not
its identity, so existing trade history and the /quick-scalp page keep
working against the same population). One open position per index blocks a
new signal on that index.

WHERE THIS RUNS
-----------------
Entries: app.quick_scalp_feed.QuickScalpFeed's own background thread,
started once at app startup (app/main.py's lifespan), calling back into
this module's _on_scalp_bar_closed whenever a new bar finalizes -- NOT the
scheduler. Exits + square-off: app.scheduler's "quick-scalp-exit-check" job,
a 5-second IntervalTrigger (replacing the 4 Sep build's 1-minute entry-and-
exit cron -- entries moved off the scheduler entirely, exits sped up to
match app.validated_signal's own precedent for continuous position
monitoring).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.db_models import IndexConfig, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.indicators import rsi
from app.market_data import ONE_MINUTE, Bar, load_bars
from app.models import ExitReason, Signal
from app.option_finder import OptionFinder
from app.platform import list_index_configs, log_event
from app.signal_validation import trading_day_reason
from app.smartapi_client import SmartAPIClient
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)

ORIGIN = "QUICK_SCALP"

# ---------------------------------------------------------------------------
# Section 3 -- Mathematical Calculations (unchanged from the 4 Sep build)
# ---------------------------------------------------------------------------

_VWAP_SIGMA_MULTIPLIER = 2.0
_RSI_PERIOD = 7
_RSI_OVERSOLD = 30.0
_RSI_OVERBOUGHT = 70.0
_WICK_REJECTION_RATIO = 0.30

_MIN_WARMUP_BARS = 15
_CANDLE_LOOKBACK_MINUTES = 420

# ---------------------------------------------------------------------------
# Section 2 -- Instrument Setup
# ---------------------------------------------------------------------------

_STRIKE_OFFSET_POINTS = 100.0

# 8 Sep 2026: requested as "trade with 2 lots by default," scoped to Quick
# Scalp only -- every other strategy in this codebase sizes its own position
# independently (AI Origination is hardcoded to exactly 1 lot for its own
# stated reasons, rule-based strategies read StrategyConfig.lots_per_trade),
# so this is a Quick-Scalp-specific constant, not a shared default. A plain
# multiplier on the resolved contract's own lot_size, not a hardcoded
# quantity -- keeps this correct across a strike/expiry with a different
# lot_size without needing a second number to stay in sync.
_LOT_MULTIPLIER = 2

# ---------------------------------------------------------------------------
# Section 5 -- Friction-Proof Risk & Position Management
# ---------------------------------------------------------------------------

_MAX_INDEX_STOP_POINTS = 14.0     # carried over from the 4 Sep build -- see NAMED DEVIATIONS #5
_STRUCTURAL_BUFFER_POINTS = 1.0   # "C0.Low - 1pt" / "C0.High + 1pt"
_STOP_PERCENT = 0.025             # "-2.5%"
_TARGET_PERCENT = 0.0375          # midpoint of "+3.5% to +4.0%"
_MIN_TARGET_POINTS = 12.0         # "Min +Rs12.00 premium"
_COST_BUFFER_POINTS = 2.0         # "Entry + Rs2.00 covers fees"
_HARD_TIME_STOP_MINUTES = 3       # "3 closed 1-minute bars (180 seconds)"

# ---------------------------------------------------------------------------
# Section 6 -- Edge Cases & Safety Constraints
# ---------------------------------------------------------------------------

_WARMUP_END = (9, 30)     # "Block all order generation prior to 09:30:00 IST"
_ENTRY_CUTOFF = (15, 10)  # "Block entries after 15:10:00 IST"
_SQUARE_OFF = (15, 15)    # "Hard square-off for any open position at 15:15:00 IST"


@dataclass(frozen=True)
class _ScalpSignal:
    """One arm-and-trigger detection: C0 satisfied the setup criteria, and
    C1 (the very next completed bar) crossed C0's opposite extreme."""

    action: str          # "BUY_CE" / "BUY_PE"
    trigger_level: float  # the level C1 crossed -- C0.high (CE) / C0.low (PE)
    setup_low: float      # C0.low
    setup_high: float     # C0.high


@dataclass(frozen=True)
class _ScalpFeatures:
    """This cycle's fully-computed feature set for one index, aligned 1:1
    with session_bars (today's completed 1-minute bars only -- VWAP/sigma
    reset at session open)."""

    session_bars: list[Bar]
    vwap_series: list[Optional[float]]
    sigma_series: list[Optional[float]]
    rsi_series: list[Optional[float]]


def _compute_vwap_bands(
    bars: list[Bar], volumes: list[float]
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """Session-cumulative VWAP and its volume-weighted standard deviation,
    per completed bar, via the spec's own formulas (section 3.1). Computed
    incrementally in O(1) per bar using the standard weighted mean-of-
    squares-minus-square-of-mean identity:

        Var_t = sum(V_i*TP_i^2)/sum(V_i) - VWAP_t^2

    `volumes[i] <= 0` falls back to a weight of 1.0 for that bar -- degrades
    gracefully when a bar's real futures-tick volume never arrived (e.g. a
    quiet minute, or the feed's futures leg couldn't resolve a contract)
    rather than invalidating the whole session's bands."""
    n = len(bars)
    vwap_series: list[Optional[float]] = [None] * n
    sigma_series: list[Optional[float]] = [None] * n
    s_v = s_vtp = s_vtp2 = 0.0
    for i, bar in enumerate(bars):
        typical_price = (bar.high + bar.low + bar.close) / 3.0
        weight = volumes[i] if i < len(volumes) and volumes[i] and volumes[i] > 0 else 1.0
        s_v += weight
        s_vtp += weight * typical_price
        s_vtp2 += weight * typical_price * typical_price
        if s_v <= 0:
            continue
        vwap_t = s_vtp / s_v
        variance = max(s_vtp2 / s_v - vwap_t * vwap_t, 0.0)
        vwap_series[i] = vwap_t
        sigma_series[i] = variance ** 0.5
    return vwap_series, sigma_series


def vwap_scalp_action(features: _ScalpFeatures) -> Optional[_ScalpSignal]:
    """Section 4's full setup-and-trigger check, evaluated against the two
    most recent completed session bars. Pure function, no DB, no network --
    directly testable. Unchanged from the 4 Sep build."""
    n = len(features.session_bars)
    if n < 2:
        return None
    c0_i, c1_i = n - 2, n - 1
    c0, c1 = features.session_bars[c0_i], features.session_bars[c1_i]
    vwap0, sigma0 = features.vwap_series[c0_i], features.sigma_series[c0_i]
    rsi0 = features.rsi_series[c0_i]
    if vwap0 is None or sigma0 is None or sigma0 <= 0 or rsi0 is None:
        return None

    upper0 = vwap0 + _VWAP_SIGMA_MULTIPLIER * sigma0
    lower0 = vwap0 - _VWAP_SIGMA_MULTIPLIER * sigma0
    candle_range = c0.high - c0.low
    if candle_range <= 0:
        return None
    lower_wick = min(c0.open, c0.close) - c0.low
    upper_wick = c0.high - max(c0.open, c0.close)

    if (
        c0.low < lower0
        and c0.close > lower0
        and (lower_wick / candle_range) >= _WICK_REJECTION_RATIO
        and rsi0 < _RSI_OVERSOLD
        and c1.high > c0.high
    ):
        return _ScalpSignal("BUY_CE", c0.high, c0.low, c0.high)

    if (
        c0.high > upper0
        and c0.close < upper0
        and (upper_wick / candle_range) >= _WICK_REJECTION_RATIO
        and rsi0 > _RSI_OVERBOUGHT
        and c1.low < c0.low
    ):
        return _ScalpSignal("BUY_PE", c0.low, c0.low, c0.high)

    return None


def _structural_stop_level(signal: _ScalpSignal) -> float:
    """Section 5's structural invalidation stop, capped at
    _MAX_INDEX_STOP_POINTS from the trigger price (see NAMED DEVIATIONS #5
    for why this cap is kept despite the new spec's own silence on it)."""
    if signal.action == "BUY_CE":
        raw = signal.setup_low - _STRUCTURAL_BUFFER_POINTS
        capped = signal.trigger_level - _MAX_INDEX_STOP_POINTS
        return max(raw, capped)
    raw = signal.setup_high + _STRUCTURAL_BUFFER_POINTS
    capped = signal.trigger_level + _MAX_INDEX_STOP_POINTS
    return min(raw, capped)


def _load_scalp_features(db: Session, index_symbol: str, now_ist) -> Optional[_ScalpFeatures]:
    """Loads today's session bars for this index and computes VWAP/sigma/
    RSI over them. Bar volume comes directly from Bar.volume -- app.
    quick_scalp_feed.QuickScalpFeed already merges real futures-contract
    volume into each bar it persists before this ever runs, so there is no
    separate futures-volume lookup here the way the 4 Sep REST-polling build
    needed. A bar written by some other, volume-blind path (e.g. a fallback
    index-candle REST pull, or a test seeding bars directly) simply carries
    volume=0, which _compute_vwap_bands' own per-bar equal-weight fallback
    already handles gracefully.

    Returns None -- fail closed, same convention as app.market_context.
    build_market_context -- when there aren't yet _MIN_WARMUP_BARS bars for
    today's session."""
    bars = load_bars(db, index_symbol, ONE_MINUTE, limit=_CANDLE_LOOKBACK_MINUTES + 30)
    session_bars = [b for b in bars if b.ts_ist.date() == now_ist.date()]
    if len(session_bars) < _MIN_WARMUP_BARS:
        return None
    volumes = [b.volume for b in session_bars]
    vwap_series, sigma_series = _compute_vwap_bands(session_bars, volumes)
    rsi_series = rsi(session_bars, _RSI_PERIOD)
    return _ScalpFeatures(session_bars, vwap_series, sigma_series, rsi_series)


def _has_open_quick_scalp_trade(db: Session, index_symbol: str) -> bool:
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


def open_scalp_trade(
    db: Session,
    index: IndexConfig,
    signal: _ScalpSignal,
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
    now_ist,
) -> Optional[StrategyTrade]:
    """Resolves a Deep ITM contract and opens exactly ONE StrategyTrade row
    -- single-clip full exit, no Target1/Runner split (see module docstring,
    "WHAT'S GENUINELY NEW IN THIS REBUILD" #2)."""
    option_type = "CE" if signal.action == "BUY_CE" else "PE"
    trade_signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE

    try:
        contract = option_finder.find_deep_itm_contract(
            trade_signal, index, _STRIKE_OFFSET_POINTS, min_dte=0, now_ist=now_ist,
        )
    except Exception as exc:
        logger.info("[QUICK_SCALP] %s: Skipped, could not resolve deep-ITM contract (%s)", index.symbol, exc)
        return None

    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[QUICK_SCALP] %s: Skipped, could not resolve price (%s)", index.symbol, exc)
        return None
    if not entry_price:
        logger.info("[QUICK_SCALP] %s: Skipped, LTP came back empty", index.symbol)
        return None

    stoploss = round(entry_price * (1 - _STOP_PERCENT), 2)
    if stoploss <= 0:
        logger.info("[QUICK_SCALP] %s: Skipped, stop would be non-positive at entry %.2f", index.symbol, entry_price)
        return None
    target = round(max(entry_price * (1 + _TARGET_PERCENT), entry_price + _MIN_TARGET_POINTS), 2)
    structural_level = round(_structural_stop_level(signal), 2)

    reasoning = (
        f"VWAP {_VWAP_SIGMA_MULTIPLIER:.0f}sigma mean-reversion: "
        f"{'lower' if option_type == 'CE' else 'upper'} band pierce + wick rejection "
        f"(>={_WICK_REJECTION_RATIO:.0%}) + RSI{_RSI_PERIOD} "
        f"{'<' if option_type == 'CE' else '>'} {(_RSI_OVERSOLD if option_type == 'CE' else _RSI_OVERBOUGHT):.0f}, "
        f"triggered on next-bar cross of {signal.trigger_level:.2f}. Single-clip, "
        f"stop {_STOP_PERCENT:.1%}, target {_TARGET_PERCENT:.2%} (floor +{_MIN_TARGET_POINTS:.0f}pts)."
    )

    trade = StrategyTrade(
        trade_id=uuid4().hex,
        strategy_name=f"Quick Scalp - {index.display_name or index.symbol}",
        signal=trade_signal.value,
        index_symbol=index.symbol,
        exchange=contract.exchange,
        tradingsymbol=contract.tradingsymbol,
        symboltoken=contract.symboltoken,
        strike=contract.strike,
        expiry=contract.expiry,
        option_type=contract.option_type,
        quantity=contract.lot_size * _LOT_MULTIPLIER,
        investment_amount=round(entry_price * contract.lot_size * _LOT_MULTIPLIER, 2),
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
        # FIXED + this origin: monitor_open_trades' shared branch already
        # enforces stoploss/target (now percentage-derived, same fields, same
        # check) with no code change needed there.
        sl_mode=SLMode.FIXED,
        origin=ORIGIN,
        ai_action=signal.action,
        ai_reasoning=reasoning,
        spot_at_entry=round(signal.trigger_level, 2),
        structural_stop_level=structural_level,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    log_event(
        db, "QUICK_SCALP",
        f"[{trade.strategy_name}] opened {trade_signal.value} @ strike {trade.strike}",
        payload={"trade_id": trade.trade_id, "structural_stop_level": structural_level},
    )
    logger.info("[QUICK_SCALP] %s opened %s for %s", ORIGIN, trade_signal.value, index.symbol)
    return trade


def check_quick_scalp_entry(
    db: Session,
    index: IndexConfig,
    features: Optional[_ScalpFeatures],
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
    now_ist,
) -> Optional[StrategyTrade]:
    if _has_open_quick_scalp_trade(db, index.symbol):
        return None
    if features is None:
        return None
    signal = vwap_scalp_action(features)
    if signal is None:
        return None
    logger.info("[QUICK_SCALP] %s -> %s", index.symbol, signal.action)
    return open_scalp_trade(db, index, signal, smartapi, option_finder, now_ist)


def _on_scalp_bar_closed(index_symbol: str, smartapi: SmartAPIClient, option_finder: OptionFinder) -> None:
    """Registered as app.quick_scalp_feed.QuickScalpFeed's on_bar_closed
    callback (see app/main.py's lifespan wiring) -- invoked synchronously on
    the feed's own background thread once a new bar has already been
    persisted. Opens and closes its own DB session, the same convention
    every other entry point in this codebase follows."""
    db = None
    try:
        if smartapi is None or option_finder is None:
            return
        now_ist = to_ist(utc_now())
        if (now_ist.hour, now_ist.minute) < _WARMUP_END or (now_ist.hour, now_ist.minute) >= _ENTRY_CUTOFF:
            return
        db = SessionLocal()
        index = db.scalar(
            select(IndexConfig).where(IndexConfig.symbol == index_symbol, IndexConfig.enabled.is_(True))
        )
        if index is None:
            return
        features = _load_scalp_features(db, index_symbol, now_ist)
        check_quick_scalp_entry(db, index, features, smartapi, option_finder, now_ist)
    except Exception:
        logger.exception("[QUICK_SCALP] bar-close entry check failed for %s", index_symbol)
    finally:
        if db is not None:
            db.close()


def make_bar_closed_callback(smartapi: SmartAPIClient, option_finder: OptionFinder):
    """Factory for QuickScalpFeed's on_bar_closed parameter -- keeps
    smartapi/option_finder as explicit closures rather than module globals."""
    return lambda index_symbol: _on_scalp_bar_closed(index_symbol, smartapi, option_finder)


def check_quick_scalp_exits(
    db: Session,
    trade_manager,
    now_ist,
    current_spot_by_index: Optional[dict] = None,
) -> None:
    """Per-cycle exit checks for every open QUICK_SCALP trade: the
    structural index-level stop, then the hard 3-minute mark's branch
    between a breakeven trail (profitable enough to cover costs -- leave it
    open, just raise the stop) and a scratch exit (not yet profitable --
    close now). The option-premium stop/target are NOT re-checked here --
    they're already enforced, faster, by the existing shared 30-second
    monitor via trade.stoploss/trade.target (see module docstring's NAMED
    DEVIATIONS #1).

    current_spot_by_index: {index_symbol: current_spot or None}, this
    cycle's already-fetched values (see run_quick_scalp_exit_checks) -- a
    missing or None entry means the structural check is skipped for that
    trade this cycle, never guessed."""
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == ORIGIN,
            )
        )
    )
    current_spot_by_index = current_spot_by_index or {}
    for trade in trades:
        try:
            if trade.current_premium is None:
                continue
            entry_ist = to_ist(trade.entry_time)
            if entry_ist is None:
                continue

            current_spot = current_spot_by_index.get(trade.index_symbol)
            if current_spot is not None and trade.structural_stop_level is not None:
                breached = (
                    current_spot <= trade.structural_stop_level if trade.option_type == "CE"
                    else current_spot >= trade.structural_stop_level
                )
                if breached:
                    trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.SCALP_STRUCTURAL_STOP)
                    log_event(
                        db, "QUICK_SCALP",
                        f"[{trade.strategy_name}] structural stop -- spot {current_spot} breached {trade.structural_stop_level}",
                        payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
                    )
                    logger.info(
                        "[QUICK_SCALP] %s closed SCALP_STRUCTURAL_STOP (spot %.2f vs level %.2f)",
                        trade.trade_id, current_spot, trade.structural_stop_level,
                    )
                    continue

            held_minutes = (now_ist - entry_ist).total_seconds() / 60
            if held_minutes < _HARD_TIME_STOP_MINUTES:
                continue

            breakeven_level = round(trade.entry_price + _COST_BUFFER_POINTS, 2)
            already_trailed = trade.stoploss >= breakeven_level
            if trade.current_premium >= breakeven_level:
                if not already_trailed:
                    trade.stoploss = breakeven_level
                    db.commit()
                    log_event(
                        db, "QUICK_SCALP",
                        f"[{trade.strategy_name}] breakeven trail at time-stop mark -- stop moved to {breakeven_level}",
                        payload={"trade_id": trade.trade_id},
                    )
                    logger.info("[QUICK_SCALP] %s breakeven trail -> stop %.2f", trade.trade_id, breakeven_level)
                # Profitable enough to cover costs -- left open to run
                # toward its real target, never force-closed here.
                continue

            trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.SCALP_TIME_STOP)
            log_event(
                db, "QUICK_SCALP",
                f"[{trade.strategy_name}] time-stop scratch -- {held_minutes:.1f} min, not yet profitable",
                payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
            )
            logger.info("[QUICK_SCALP] %s scratched (SCALP_TIME_STOP) after %.1f min", trade.trade_id, held_minutes)
        except Exception:
            logger.exception("[QUICK_SCALP] exit check failed for trade %s", trade.trade_id)


def _square_off_all(db: Session, trade_manager) -> None:
    """"Hard square-off for any open position at 15:15:00 IST" -- fires
    unconditionally, replacing the nuanced exit checks above once past the
    cutoff."""
    trades = list(
        db.scalars(
            select(StrategyTrade).where(StrategyTrade.status == TradeStatus.OPEN, StrategyTrade.origin == ORIGIN)
        )
    )
    for trade in trades:
        if trade.current_premium is None:
            continue
        trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.TIME_EXIT)
        log_event(
            db, "QUICK_SCALP",
            f"[{trade.strategy_name}] squared off at {_SQUARE_OFF[0]:02d}:{_SQUARE_OFF[1]:02d} IST",
            payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
        )
        logger.info("[QUICK_SCALP] %s squared off at %02d:%02d cutoff", trade.trade_id, _SQUARE_OFF[0], _SQUARE_OFF[1])


def run_quick_scalp_exit_checks(
    smartapi: Optional[SmartAPIClient] = None,
    trade_manager=None,
    db=None,
) -> None:
    """Scheduler entry point (see app.scheduler's "quick-scalp-exit-check"
    job, a 5-second IntervalTrigger). Owns its own DB session when called
    from the scheduler; accepts an existing session in tests. Entries are
    NOT handled here -- see app.quick_scalp_feed.QuickScalpFeed and
    _on_scalp_bar_closed above; this function only manages already-open
    positions and end-of-day square-off. Returns immediately with zero
    SmartAPI calls whenever nothing is open, same precedent as app.
    validated_signal's own 5-second exit job -- the fast cadence costs
    nothing in the overwhelmingly common idle case."""
    if smartapi is None or trade_manager is None:
        logger.info("[QUICK_SCALP] Skipped: no smartapi/trade_manager available in this context")
        return
    if trading_day_reason(to_ist(utc_now())) is not None:
        return
    owns_session = db is None
    session = db or SessionLocal()
    try:
        open_trades = list(
            session.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.origin == ORIGIN,
                )
            )
        )
        if not open_trades:
            return
        now_ist = to_ist(utc_now())
        if (now_ist.hour, now_ist.minute) >= _SQUARE_OFF:
            _square_off_all(session, trade_manager)
            return

        open_index_symbols = {trade.index_symbol for trade in open_trades}
        current_spot_by_index: dict[str, float | None] = {}
        for index in list_index_configs(session):
            if index.symbol not in open_index_symbols:
                continue
            try:
                current_spot_by_index[index.symbol] = smartapi.get_index_spot(index)
            except Exception:
                current_spot_by_index[index.symbol] = None

        check_quick_scalp_exits(session, trade_manager, now_ist, current_spot_by_index)
    finally:
        if owns_session:
            session.close()
