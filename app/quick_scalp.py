"""Quick Scalp -- 4 Sep 2026 rebuild. Replaces the original EMA9/EMA21-
crossover build entirely, on an explicit instruction: "Replace scalping to
following logic. Build exactly as said. Do not skip anything," pasting a
full spec titled "NIFTY 50 VWAP 2-sigma Mean-Reversion Scalp -- SmartAPI
Implementation Spec." Every rule in that spec is implemented below. Where
this codebase's real architecture cannot support something LITERALLY as
worded, the closest faithful equivalent is built and the substitution is
named here, not silently dropped -- the same discipline this project applied
to the Autonomous AI rebuild the same week.

STRATEGY, IN ONE PARAGRAPH
----------------------------
On each completed 1-minute bar of the underlying index, track a session
VWAP and its 2-sigma bands. A bar (`C0`) that pierces a band, closes back
inside it, leaves a rejection wick covering >=30% of its range, and has
RSI(7) confirming exhaustion (<30 for a lower-band piercing, >70 for an
upper-band one) is a candidate reversal. If the VERY NEXT completed bar
(`C1`) then trades back through C0's opposite extreme (C0's high for a
bullish setup, C0's low for a bearish one), that is the entry trigger. A
Deep ITM contract is bought, split 50/50 into a "Target 1" leg (a flat
option-point target) and a "Runner" leg (rides until spot reaches back to
VWAP, or a hard 3-minute time stop, or a structural/premium stop fires
first).

NAMED DEVIATIONS -- WHAT'S BUILT DIFFERENTLY FROM THE LITERAL SPEC, AND WHY
-------------------------------------------------------------------------------
1. **Data ingestion is REST 1-minute candle polling, not WebSocket tick
   synthesis.** The spec's own bar-level logic (VWAP, sigma, RSI7, wick
   geometry, the C0/C1 setup-and-trigger check) is entirely BAR-based, not
   tick-based -- only the spec's "aggregate ticks into 1-minute bars,
   finalize on the first tick of the next second" instruction is genuinely
   tick-level. This codebase's entire live-data architecture (every other
   strategy here, AI Origination included) already runs on
   smartapi.get_candles() polled on a scheduler tick, the same pattern this
   module reuses -- building a new raw WebSocket tick-aggregation pipeline
   is a materially larger, separate infrastructure project this codebase
   doesn't have (app.live_feed.py's own persistent WS connection is index
   LTP only, no depth, no bar synthesis). The strategy LOGIC is unchanged;
   only how a completed bar reaches it differs -- REST polling on the
   existing 1-minute scheduler job instead of live tick aggregation.
2. **Deep ITM delta (~0.65-0.75) is approximated by a fixed point offset,
   not computed.** This codebase has no live per-contract Greeks feed (see
   CLAUDE.md's own note on impliedVolatility/Greeks being unverified where
   they do exist). The spec's own wording treats the point offset as a
   proxy for that delta band ("Delta ~0.65-0.75" listed as a parenthetical
   next to the point rule, not a separately computed gate) -- implemented
   exactly as the point rule, nothing invented for delta.
3. **"Fire market order" / "limit order with a 2-point marketable buffer"
   both resolve to a fetched LTP fill, same as every other paper strategy in
   this codebase.** No synthetic slippage is added in either direction --
   this module has no live order-placement path at all (see "STRUCTURALLY
   PAPER-ONLY" below), so an order-TYPE distinction has no real execution
   consequence to model here.
4. **"Hard broker-level SL-M order" is NOT a real order sent to Angel One.**
   This project's standing, repeatedly-reinforced rule is that every
   experimental/paper strategy has NO order-placement code path at all, not
   just one gated off by default (see CLAUDE.md's "Live-trading safety"
   section and every other experimental module's own docstring). The spec's
   REQUIREMENT -- a fast, deterministic, unconditional premium stop -- is
   still built in full: the option-point stop is stored directly on
   trade.stoploss and enforced by the EXISTING shared monitor_open_trades
   30-second tick (app/multi_strategy.py), the fastest, most deterministic
   check this codebase has, running on ITS OWN independent SmartAPI call
   path (see deviation 5). What's not built is a literal exchange-side
   SL-M order -- the safety property (a hard, fast, un-overridable stop) is
   real; the transport (a genuine broker order) is not, because this module
   is structurally forbidden from placing one.
5. **The WebSocket-disconnect fallback is mapped onto this module's actual,
   non-WS architecture.** "If ticks cease >5s, place a fallback SL-M and
   halt new signals": there is no continuous tick stream here to go silent
   for 5 seconds. What this module DOES do: if its own 1-minute index-candle
   refresh fails, new entries are halted for that cycle (see
   `_compute_scalp_features`'s `refresh_failed` return and
   `run_quick_scalp_checks`'s handling of it) -- "halt new signals",
   faithfully. The "fallback SL-M" half is already structurally satisfied
   without needing to do anything extra: the option-premium stop
   (trade.stoploss) is checked by monitor_open_trades' own 30-second tick,
   which calls SmartAPI independently of THIS module's candle refresh -- a
   failure in this module's own data pull does not silently remove that
   protection, because it was never this module's job to enforce it in the
   first place.
6. **"Armed state" needs no persisted flag.** The spec's own state machine
   (IDLE -> ARM_BUY_CE/PE -> ENTER_CE/PE, valid strictly for the duration of
   the next bar, disarmed if that bar closes without a cross) is fully
   reproduced, but stateless: `vwap_scalp_action` re-evaluates the C0/C1
   pair fresh every cycle from the stored bar history. This is equivalent
   by construction -- an "armed" state that is checked exactly once, on
   exactly the next bar, and discarded either way, needs no separate
   database flag to expire correctly; the two-bar window it's valid for IS
   the check itself. This also directly satisfies the "one active trade /
   never evaluate a new position while an armed state is pending" rule --
   there is nothing pending to concurrently manage.

STRUCTURALLY PAPER-ONLY
-------------------------
Same construction as every other experimental strategy in this project:
mode is hardcoded to TradingMode.PAPER, smartapi.place_market_order is never
called anywhere in this module.

ISOLATION
----------
origin="QUICK_SCALP" (unchanged from the superseded build -- this replaces
the STRATEGY's logic, not its identity, so existing trade history and the
/quick-scalp page keep working against the same population). One open
position per index blocks a new signal on that index -- both legs of a
still-open trade count.

WHERE THIS RUNS
-----------------
Unchanged: app.scheduler's "quick-scalp-check" job, 1-minute resolution --
matches the spec's own 1-minute bar cadence exactly, more precisely than the
superseded build's own reasoning even needed to argue for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.db_models import IndexConfig, SLMode, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.indicators import rsi
from app.market_data import ONE_MINUTE, Bar, load_bars, parse_smartapi_row, store_bars
from app.models import ExitReason, Signal
from app.option_finder import OptionFinder
from app.platform import list_index_configs, log_event
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIClient
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)

ORIGIN = "QUICK_SCALP"

# ---------------------------------------------------------------------------
# Section 3 -- Mathematical Calculations
# ---------------------------------------------------------------------------

_VWAP_SIGMA_MULTIPLIER = 2.0
_RSI_PERIOD = 7
_RSI_OVERSOLD = 30.0
_RSI_OVERBOUGHT = 70.0
_WICK_REJECTION_RATIO = 0.30

# Minimum session bars before a setup is evaluated at all -- "allows minimum
# 15 bars for VWAP variance stability," independent of the 09:30 clock gate
# below (belt and suspenders: a data gap after 09:30 must not let a
# half-warmed variance estimate drive a signal).
_MIN_WARMUP_BARS = 15

# Generous enough to reliably cover the whole session from 09:15 regardless
# of what time this cycle runs, since VWAP/sigma reset at session open and
# need every bar since then, not a short rolling window.
_CANDLE_LOOKBACK_MINUTES = 420

# Synthetic Candle.index_symbol suffix for futures volume, matching
# app.ai.autonomous's own convention exactly (same underlying concept, same
# key shape -- the two modules' futures candle fetches are naturally
# idempotent against each other, store_bars is an upsert either way).
_FUTURES_CANDLE_SUFFIX = "_FUT"

# ---------------------------------------------------------------------------
# Section 2 -- Instrument Setup
# ---------------------------------------------------------------------------

_STRIKE_OFFSET_POINTS = 100.0

# ---------------------------------------------------------------------------
# Section 5 -- Strict Risk & Position Management
# ---------------------------------------------------------------------------

_MAX_INDEX_STOP_POINTS = 14.0          # "Max Index Stop Loss: 12-14 pts"
_STRUCTURAL_BUFFER_POINTS = 1.0        # "C0.Low - 1pt" / "C0.High + 1pt"
_OPTION_SL_POINTS = 9.0                # "~8-10 Option Premium Points" -- midpoint
_TARGET1_OPTION_POINTS = 13.0          # "+12-14 Option Points" -- midpoint
_TARGET1_LOT_FRACTION = 0.5
_BREAKEVEN_BUFFER_POINTS = 1.0         # "Entry Price + 1 pt"
_HARD_TIME_STOP_MINUTES = 3            # "3 Completed Candles (180 seconds)"

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

    which is algebraically identical to the spec's own
    sum(V_i*(TP_i-VWAP_t)^2)/sum(V_i) form (expand the square and substitute
    VWAP_t = sum(V_i*TP_i)/sum(V_i) -- the cross term cancels), avoiding an
    O(n) rescan of every prior bar on every new one.

    `volumes[i] <= 0` falls back to a weight of 1.0 for that bar -- the
    spec's own explicit contingency ("if subscribing to spot index where
    volume is flat/zero, compute equal-weighted rolling stdev"), applied
    per-bar so a single missing-volume minute degrades gracefully rather
    than invalidating the whole session's bands.
    """
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
    """Section 4's full setup-and-trigger check, evaluated fresh each cycle
    against the two most recent completed session bars. Pure function, no
    DB, no network -- directly testable.

    BUY_CE: C0.low pierces the lower band, C0 closes back above it, the
    lower rejection wick covers >=30% of C0's range, RSI7 < 30 on C0, and
    C1 (the very next bar) trades back above C0's high.
    BUY_PE: the mirror image against the upper band / RSI7 > 70 / C1 below
    C0's low.

    Returns None on anything not fully set up -- including simply "C0 was a
    valid setup but C1 didn't cross" (the spec's own disarm-on-no-cross
    rule; see module docstring's NAMED DEVIATIONS #6 for why no separate
    "armed" state needs to persist for this to be correct)."""
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
    """Section 5's "Structural SL", capped by "Max Index Stop Loss": CE gets
    C0.low - 1pt, never more than 14pts below the trigger price; PE mirrors
    against C0.high + 1pt / 14pts above."""
    if signal.action == "BUY_CE":
        raw = signal.setup_low - _STRUCTURAL_BUFFER_POINTS
        capped = signal.trigger_level - _MAX_INDEX_STOP_POINTS
        return max(raw, capped)
    raw = signal.setup_high + _STRUCTURAL_BUFFER_POINTS
    capped = signal.trigger_level + _MAX_INDEX_STOP_POINTS
    return min(raw, capped)


def _sibling_trade_id(trade_id: str) -> str:
    """Leg pairing is encoded in trade_id's own last character (`A`/`B`)
    rather than a new linking column -- both legs share one group uuid."""
    return trade_id[:-1] + ("A" if trade_id.endswith("B") else "B")


def _futures_volume_by_minute(
    db: Session, index: IndexConfig, option_finder: OptionFinder, smartapi: SmartAPIClient, now_ist
) -> dict:
    """Real per-minute volume from the near-month futures contract, keyed by
    naive-IST minute timestamp -- the spec's own explicit route to a real
    volume-weighted VWAP against an index instrument that itself reports
    zero volume. Returns {} (triggering _compute_vwap_bands' own per-bar
    equal-weighted fallback) when no futures contract can be resolved."""
    try:
        contract = option_finder.find_current_futures_contract(index)
    except Exception as exc:
        logger.info("[QUICK_SCALP] %s: futures contract lookup failed (%s), falling back to equal-weighted bands", index.symbol, exc)
        return {}
    if contract is None:
        return {}
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
        logger.info("[QUICK_SCALP] %s: futures candle refresh failed (%s)", index.symbol, exc)
    bars = load_bars(db, futures_key, ONE_MINUTE)
    return {b.ts_ist: b.volume for b in bars if b.ts_ist.date() == now_ist.date() and b.volume}


def _compute_scalp_features(
    db: Session, index: IndexConfig, smartapi: SmartAPIClient, option_finder: OptionFinder, now_ist
) -> tuple[Optional[_ScalpFeatures], bool]:
    """Returns (features, refresh_failed). features is None -- fail closed,
    same convention as app.market_context.build_market_context -- when there
    aren't yet _MIN_WARMUP_BARS session bars. refresh_failed is True when
    the live candle pull itself errored this cycle; see module docstring's
    NAMED DEVIATIONS #5 for how callers use it to halt new entries without
    touching the independent premium-based stop protection on any already-
    open leg."""
    refresh_failed = False
    if index.spot_token:
        try:
            rows = smartapi.get_candles(
                exchange=index.spot_exchange,
                symboltoken=index.spot_token,
                interval=ONE_MINUTE,
                from_dt=(now_ist - timedelta(minutes=_CANDLE_LOOKBACK_MINUTES)).strftime("%Y-%m-%d %H:%M"),
                to_dt=now_ist.strftime("%Y-%m-%d %H:%M"),
            )
            if rows:
                store_bars(db, index.symbol, ONE_MINUTE, [parse_smartapi_row(row) for row in rows])
        except Exception as exc:
            refresh_failed = True
            logger.info("[QUICK_SCALP] %s: candle refresh failed (%s), using stored history", index.symbol, exc)

    bars = load_bars(db, index.symbol, ONE_MINUTE, limit=_CANDLE_LOOKBACK_MINUTES + 30)
    session_bars = [b for b in bars if b.ts_ist.date() == now_ist.date()]
    if len(session_bars) < _MIN_WARMUP_BARS:
        return None, refresh_failed

    volume_by_minute = _futures_volume_by_minute(db, index, option_finder, smartapi, now_ist)
    volumes = [volume_by_minute.get(b.ts_ist, 0.0) for b in session_bars]
    vwap_series, sigma_series = _compute_vwap_bands(session_bars, volumes)
    rsi_series = rsi(session_bars, _RSI_PERIOD)
    return _ScalpFeatures(session_bars, vwap_series, sigma_series, rsi_series), refresh_failed


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
) -> list[StrategyTrade]:
    """Resolves a Deep ITM contract and opens one or two StrategyTrade rows
    (Target-1 leg + Runner leg, section 5's 50%/50% split) at flat OPTION-
    POINT stop/target levels -- not the percent-then-CE/PE-rescaled levels
    every other strategy in this codebase uses, since the spec's own
    numbers are already absolute option points, not a nominal percent."""
    option_type = "CE" if signal.action == "BUY_CE" else "PE"
    trade_signal = Signal.BUY_CE if option_type == "CE" else Signal.BUY_PE

    try:
        # No DTE floor here, deliberately -- the spec wants the CURRENT
        # weekly contract, with only the explicit 13:30-on-expiry-day roll
        # (see find_deep_itm_contract). This is a real, named departure from
        # every other strategy's _MIN_DTE_TO_TRADE=5 in this codebase, whose
        # reasoning (stop survivability at short DTE, see CLAUDE.md) is
        # about ATM contracts specifically -- a Deep ITM contract's premium
        # is mostly intrinsic value, a structurally different risk profile.
        contract = option_finder.find_deep_itm_contract(
            trade_signal, index, _STRIKE_OFFSET_POINTS, min_dte=0, now_ist=now_ist,
        )
    except Exception as exc:
        logger.info("[QUICK_SCALP] %s: Skipped, could not resolve deep-ITM contract (%s)", index.symbol, exc)
        return []

    try:
        entry_price = smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
    except Exception as exc:
        logger.info("[QUICK_SCALP] %s: Skipped, could not resolve price (%s)", index.symbol, exc)
        return []
    if not entry_price:
        logger.info("[QUICK_SCALP] %s: Skipped, LTP came back empty", index.symbol)
        return []

    option_stoploss = round(entry_price - _OPTION_SL_POINTS, 2)
    if option_stoploss <= 0:
        logger.info("[QUICK_SCALP] %s: Skipped, option SL would be non-positive at entry %.2f", index.symbol, entry_price)
        return []
    structural_level = round(_structural_stop_level(signal), 2)
    target1 = round(entry_price + _TARGET1_OPTION_POINTS, 2)
    # The Runner leg's real exit is a VWAP cross (checked live each cycle in
    # check_quick_scalp_exits), not a flat premium level -- this is an
    # intentionally distant sentinel so the shared monitor's own blunt
    # premium-target check never fires ahead of that.
    runner_target_sentinel = round(entry_price * 5, 2)

    qty_total = contract.lot_size
    qty_a = int(qty_total * _TARGET1_LOT_FRACTION)
    qty_b = qty_total - qty_a
    splittable = qty_a > 0 and qty_b > 0

    reasoning = (
        f"VWAP {_VWAP_SIGMA_MULTIPLIER:.0f}sigma mean-reversion: "
        f"{'lower' if option_type == 'CE' else 'upper'} band pierce + wick rejection "
        f"(>={_WICK_REJECTION_RATIO:.0%}) + RSI{_RSI_PERIOD} "
        f"{'<' if option_type == 'CE' else '>'} {(_RSI_OVERSOLD if option_type == 'CE' else _RSI_OVERBOUGHT):.0f}, "
        f"triggered on next-bar cross of {signal.trigger_level:.2f}."
    )
    group_id = uuid4().hex
    trades: list[StrategyTrade] = []

    def _make_leg(suffix: str, quantity: int, target: float, leg_label: str) -> StrategyTrade:
        trade = StrategyTrade(
            trade_id=f"{group_id}{suffix}",
            strategy_name=f"Quick Scalp - {index.display_name or index.symbol} ({leg_label})",
            signal=trade_signal.value,
            index_symbol=index.symbol,
            exchange=contract.exchange,
            tradingsymbol=contract.tradingsymbol,
            symboltoken=contract.symboltoken,
            strike=contract.strike,
            expiry=contract.expiry,
            option_type=contract.option_type,
            quantity=quantity,
            investment_amount=round(entry_price * quantity, 2),
            entry_price=round(entry_price, 2),
            current_premium=round(entry_price, 2),
            stoploss=option_stoploss,
            target=target,
            entry_time=utc_now(),
            # Structurally paper-only -- see module docstring. No live order
            # path exists anywhere in this module.
            mode=TradingMode.PAPER,
            status=TradeStatus.OPEN,
            result=TradeResult.OPEN,
            highest_price=round(entry_price, 2),
            lowest_price=round(entry_price, 2),
            trailing_active=False,
            # FIXED + this origin: monitor_open_trades' shared branch already
            # enforces stoploss/target (flat option points here, same field,
            # same check) with no code change needed there.
            sl_mode=SLMode.FIXED,
            origin=ORIGIN,
            ai_action=signal.action,
            ai_reasoning=f"{reasoning} {leg_label} leg.",
            spot_at_entry=round(signal.trigger_level, 2),
            structural_stop_level=structural_level,
        )
        db.add(trade)
        return trade

    if splittable:
        trades = [
            _make_leg("A", qty_a, target1, "Target 1, 50% lot"),
            _make_leg("B", qty_b, runner_target_sentinel, "Runner, VWAP-cross exit"),
        ]
    else:
        # Lot too small to split meaningfully -- a zero-quantity runner leg
        # is meaningless, so fall back to one full-size trade governed by
        # Target 1 / the option-point stop / the structural stop / the hard
        # time stop. No breakeven-move or VWAP-cross-runner mechanics apply,
        # since there is no "remaining lot" to protect or let run.
        trades = [_make_leg("A", qty_total, target1, "single lot, not splittable")]

    db.commit()
    for trade in trades:
        db.refresh(trade)
        log_event(
            db, "QUICK_SCALP",
            f"[{trade.strategy_name}] opened {trade_signal.value} @ strike {trade.strike}",
            payload={"trade_id": trade.trade_id, "structural_stop_level": structural_level},
        )
        logger.info("[QUICK_SCALP] %s opened %s leg %s for %s", ORIGIN, trade_signal.value, trade.trade_id[-1], index.symbol)
    return trades


def check_quick_scalp_entry(
    db: Session,
    index: IndexConfig,
    features: Optional[_ScalpFeatures],
    smartapi: SmartAPIClient,
    option_finder: OptionFinder,
    now_ist,
) -> list[StrategyTrade]:
    if _has_open_quick_scalp_trade(db, index.symbol):
        return []
    if features is None:
        return []
    signal = vwap_scalp_action(features)
    if signal is None:
        return []
    logger.info("[QUICK_SCALP] %s -> %s", index.symbol, signal.action)
    return open_scalp_trade(db, index, signal, smartapi, option_finder, now_ist)


def check_quick_scalp_exits(
    db: Session,
    trade_manager,
    now_ist,
    current_by_index: Optional[dict] = None,
) -> None:
    """Section 5/6's per-cycle exit checks, in order, for every open
    QUICK_SCALP leg: a breakeven move on the Runner leg once its sibling's
    Target 1 has closed, the structural index-level stop, the Runner leg's
    VWAP-cross target, then the hard 3-minute time stop. The option-point
    stop and Target 1 are NOT re-checked here -- they're already enforced,
    faster, by the existing shared 30-second monitor via trade.stoploss/
    trade.target (see module docstring's NAMED DEVIATIONS #4).

    current_by_index: {index_symbol: (current_spot, current_vwap)}, this
    cycle's already-computed values (see run_quick_scalp_checks) -- a
    missing or (None, None) entry means the structural and VWAP-cross
    checks are skipped for that trade this cycle (falls through to the hard
    time stop), never guessed."""
    trades = list(
        db.scalars(
            select(StrategyTrade).where(
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == ORIGIN,
            )
        )
    )
    current_by_index = current_by_index or {}
    for trade in trades:
        try:
            if trade.current_premium is None:
                continue
            entry_ist = to_ist(trade.entry_time)
            if entry_ist is None:
                continue
            is_leg_b = trade.trade_id.endswith("B")

            if is_leg_b:
                sibling = db.scalar(select(StrategyTrade).where(StrategyTrade.trade_id == _sibling_trade_id(trade.trade_id)))
                if (
                    sibling is not None
                    and sibling.status == TradeStatus.CLOSED
                    and sibling.exit_reason == ExitReason.TARGET.value
                    and trade.stoploss < round(trade.entry_price + _BREAKEVEN_BUFFER_POINTS, 2)
                ):
                    trade.stoploss = round(trade.entry_price + _BREAKEVEN_BUFFER_POINTS, 2)
                    db.commit()
                    log_event(
                        db, "QUICK_SCALP",
                        f"[{trade.strategy_name}] breakeven move -- stop tightened to {trade.stoploss} after Target 1",
                        payload={"trade_id": trade.trade_id},
                    )
                    logger.info("[QUICK_SCALP] %s breakeven move -> stop %.2f", trade.trade_id, trade.stoploss)

            current_spot, current_vwap = current_by_index.get(trade.index_symbol, (None, None))

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

            if is_leg_b and current_spot is not None and current_vwap is not None:
                vwap_reached = current_spot >= current_vwap if trade.option_type == "CE" else current_spot <= current_vwap
                if vwap_reached:
                    trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.SCALP_VWAP_TARGET)
                    log_event(
                        db, "QUICK_SCALP",
                        f"[{trade.strategy_name}] runner target -- spot {current_spot} reached VWAP {current_vwap}",
                        payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
                    )
                    logger.info("[QUICK_SCALP] %s closed SCALP_VWAP_TARGET", trade.trade_id)
                    continue

            held_minutes = (now_ist - entry_ist).total_seconds() / 60
            if held_minutes >= _HARD_TIME_STOP_MINUTES:
                trade_manager.close_trade(db, trade, trade.current_premium, ExitReason.SCALP_TIME_STOP)
                log_event(
                    db, "QUICK_SCALP",
                    f"[{trade.strategy_name}] hard time stop -- {held_minutes:.1f} min, neither level hit",
                    payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent},
                )
                logger.info("[QUICK_SCALP] %s closed SCALP_TIME_STOP after %.1f min", trade.trade_id, held_minutes)
        except Exception:
            logger.exception("[QUICK_SCALP] exit check failed for trade %s", trade.trade_id)


def _square_off_all(db: Session, trade_manager) -> None:
    """"Hard square-off for any open position at 15:15:00 IST" -- fires
    unconditionally, replacing the nuanced exit checks above rather than
    running alongside them once past the cutoff."""
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


def run_quick_scalp_checks(
    smartapi: Optional[SmartAPIClient] = None,
    option_finder: Optional[OptionFinder] = None,
    trade_manager=None,
    db=None,
) -> None:
    """Scheduler entry point (see app.scheduler's "quick-scalp-check" job).
    Owns its own DB session when called from the scheduler; accepts an
    existing session in tests."""
    if smartapi is None or option_finder is None or trade_manager is None:
        logger.info("[QUICK_SCALP] Skipped: no smartapi/option_finder/trade_manager available in this context")
        return
    closed_reason = check_market_hours(utc_now())
    if closed_reason is not None:
        logger.info("[QUICK_SCALP] Cycle skipped -- %s", closed_reason.replace("Signal received ", "", 1))
        return
    owns_session = db is None
    session = db or SessionLocal()
    try:
        now_ist = to_ist(utc_now())
        past_square_off = (now_ist.hour, now_ist.minute) >= _SQUARE_OFF
        indexes = [index for index in list_index_configs(session) if index.enabled]

        # Feature engine runs once per enabled index per cycle -- shared
        # between exit management (structural/VWAP-cross checks) and entry
        # evaluation below.
        features_by_index: dict[str, Optional[_ScalpFeatures]] = {}
        refresh_failed_by_index: dict[str, bool] = {}
        current_by_index: dict[str, tuple] = {}
        for index in indexes:
            features, refresh_failed = _compute_scalp_features(session, index, smartapi, option_finder, now_ist)
            features_by_index[index.symbol] = features
            refresh_failed_by_index[index.symbol] = refresh_failed
            current_vwap = features.vwap_series[-1] if features is not None and features.vwap_series else None
            current_spot = None
            if current_vwap is not None:
                try:
                    current_spot = smartapi.get_index_spot(index)
                except Exception:
                    current_spot = None
            current_by_index[index.symbol] = (current_spot, current_vwap) if current_spot is not None else (None, None)

        if past_square_off:
            _square_off_all(session, trade_manager)
        else:
            check_quick_scalp_exits(session, trade_manager, now_ist, current_by_index)

        if (now_ist.hour, now_ist.minute) < _WARMUP_END or (now_ist.hour, now_ist.minute) >= _ENTRY_CUTOFF:
            return

        for index in indexes:
            if refresh_failed_by_index.get(index.symbol):
                logger.info(
                    "[QUICK_SCALP] %s: data refresh failed this cycle -- halting new signals", index.symbol,
                )
                continue
            try:
                check_quick_scalp_entry(
                    session, index, features_by_index.get(index.symbol), smartapi, option_finder, now_ist,
                )
            except Exception:
                logger.exception("[QUICK_SCALP] entry check failed for %s", index.symbol)
    finally:
        if owns_session:
            session.close()
