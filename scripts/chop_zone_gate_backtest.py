"""Does Autonomous AI's CHOP_ZONE session-phase gate block genuinely good
setups along with bad ones?

TRIGGER (17 Sep 2026, one day, not evidence on its own)
--------------------------------------------------------
Nifty ran a real ~50-point rally between roughly 11:00 AM and 1:00 PM IST.
Confirmed directly via journalctl (no live autonomous_ai_logs history exists
yet -- the table hasn't even been deployed to production as of this
writing): every single 5-minute Autonomous AI cycle from 11:15 AM through
1:27 PM, on BOTH indices, logged `Deterministic block -- session phase
CHOP_ZONE` -- the model was never once asked about the move. Before 11:15,
10:45-11:11 AM, Nifty was separately blocked by the ADX hard floor (ADX
15.36-17.28, below the 18.0 minimum) -- the move hadn't built up trend
strength yet. So Nifty never once cleared BOTH of Autonomous AI's
deterministic pre-model gates during the entire visible rally.

This is not a new question -- it is a previously-flagged risk playing out
live. When the CHOP_ZONE block shipped (3 Sep 2026, "without judgement" per
an external design document), CLAUDE.md's own notes at the time stated the
block "contradicts the ONE Bonferroni-significant finding this project's
entire two-year backtest history has produced" -- EMA_STACK, ST_ALIGNED,
ORB_BREAK and PDH_PDL_BREAK setups replicate a real forward edge specifically
in the 11:00-14:00 IST window (see the 31 Jul 2026 walk-forward), which the
CHOP_ZONE window (11:15-13:30) sits almost entirely inside of. It shipped
anyway, per explicit instruction, with that tension named rather than
resolved. Today is a live instance of exactly that tension.

WHAT THIS SCRIPT TESTS
-----------------------
Real Autonomous AI history cannot answer this yet -- autonomous_ai_logs (the
decision-logging table added 17 Sep 2026, PR #99/#100) had zero rows for
NIFTY as of the trigger day because the deploy carrying it had not reached
production yet. So this falls back on the same 2-year index-level candle
archive every other gate-validation script in this project uses when live
history is too short or nonexistent (see adx_gate_backtest.py's PART 4,
break_confirmation_backtest.py's PART 2, trend_age_gate_backtest.py).

PART A reconstructs Autonomous AI's own two deterministic entry ingredients
directly from app/ai/autonomous.py -- EMA9 vs EMA21 (the exact comparison
_trend_regime makes) for direction, ADX >= 18.0 (_ADX_HARD_FLOOR, exact
value) as the floor -- and asks: among bars that already clear the ADX
floor and have a real EMA9/21 direction, is forward index-direction edge
during CHOP_ZONE (11:15-13:30) reliably worse than during the two windows
the gate actually lets through (MORNING_MOMENTUM 09:45-11:15 portion,
AFTERNOON_TREND 13:30-15:00, both already inside Autonomous AI's own
09:45-15:00 trading window)? If CHOP_ZONE's edge is NOT reliably worse --
comparable, or better -- that is real evidence the block is over-broad
rather than correctly targeted.

PART B cross-checks with a completely independent, already-registered,
partially-validated signal construction: EMA_STACK and ST_ALIGNED from
scripts/backtest/setups.py, the exact two setups the 31 Jul 2026
walk-forward already found carry a real, replicated edge in a window
overlapping CHOP_ZONE almost entirely. Re-sliced here to CHOP_ZONE's own
precise boundary rather than the original wider 11:00-14:00 window, since
what matters for this question is the boundary the live gate actually uses.

Same limitation as every setup_significance-style script in this project:
index-direction-only, no real trades, no real premium P&L, no confidence
score, and no model in the loop. This measures whether the underlying
market during CHOP_ZONE looks structurally different from the rest of the
trading day -- it cannot measure whether Autonomous AI's own LLM exit
judgment would actually have captured that edge had the gate let it
through.

Per this project's standing rule: report what the data shows, including
"not enough evidence" as an acceptable outcome. No change is made to
app/ai/autonomous.py by this script -- that is a deliberate follow-up
decision, made only if the gate looks over-broad on evidence that
replicates across both indices, matching the same-standard this project has
applied to every other candidate gate change.

Usage:
    python -m scripts.chop_zone_gate_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import time

import numpy as np

from scripts.backtest.data import IndexArrays, build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.setups import Setup, assert_causal, build_signals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chop_zone_gate_backtest")

# Exact values from app/ai/autonomous.py -- not reinvented here.
ADX_HARD_FLOOR = 18.0
TRADING_START = time(9, 45)       # _DEFAULT_TRADING_START
TRADING_END = time(15, 0)         # _TRADING_END
CHOP_ZONE_START = time(11, 15)    # _CHOP_ZONE_START
AFTERNOON_TREND_START = time(13, 30)  # _AFTERNOON_TREND_START

HORIZON_BARS = 12  # 60 min at the default FIVE_MINUTE interval -- same choice every sibling script makes
MIN_SIGNALS = 30
BOOTSTRAP_ITERATIONS = 2000
SEED = 20260917

CROSS_CHECK_SETUPS = ("ST_ALIGNED", "EMA_STACK")


def _session_phase_buckets(arrays: IndexArrays) -> tuple[np.ndarray, np.ndarray]:
    """(is_chop_zone, is_open_window), both already restricted to Autonomous
    AI's own 09:45-15:00 trading window. OPENING_VOLATILITY (<09:30) and
    SQUARE_OFF_ZONE (>=15:00) never appear in either bucket -- they're
    excluded by the trading-window filter itself, not a separate check, so
    the two buckets partition exactly the time Autonomous AI would otherwise
    be free to trade."""
    times = arrays.ts.astype("datetime64[m]").astype(object)
    t = np.array([dt.time() for dt in times])
    in_trading_window = (t >= TRADING_START) & (t <= TRADING_END)
    is_chop_zone = in_trading_window & (t >= CHOP_ZONE_START) & (t < AFTERNOON_TREND_START)
    is_open_window = in_trading_window & ~is_chop_zone
    return is_chop_zone, is_open_window


def _trend_direction(arrays: IndexArrays) -> np.ndarray:
    """+1/-1 exactly mirroring app.ai.autonomous._trend_regime's own
    fast_ema (EMA9) vs slow_ema (EMA21) comparison. 0 (no signal) when equal
    or either is NaN."""
    direction = np.zeros(len(arrays), dtype=np.int8)
    with np.errstate(invalid="ignore"):
        direction[arrays.ema9 > arrays.ema21] = 1
        direction[arrays.ema9 < arrays.ema21] = -1
    return direction


def _adx_floor_eligible(arrays: IndexArrays) -> np.ndarray:
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21) & ~np.isnan(arrays.adx14)
    return warm & (arrays.adx14 >= ADX_HARD_FLOOR)


def _edge_index(wins: float, ups: float, longs: float, n: float) -> float:
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _evaluate(
    arrays: IndexArrays, mask: np.ndarray, direction: np.ndarray, forward_bars: int, rng,
) -> tuple[int, float, float, float]:
    """(n_signals, edge, ci_low, ci_high) via session-block bootstrap. Same
    shape as adx_gate_backtest.py's own _evaluate_index -- duplicated per
    this project's established per-script convention, not shared."""
    n_bars = len(arrays)
    close = arrays.close.astype(np.float64)
    bounds = forward_window_bounds(arrays, forward_bars)
    positions = np.arange(n_bars)
    target = np.minimum(positions + forward_bars, bounds)

    valid = mask & (direction != 0) & (target > positions)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return 0, 0.0, 0.0, 0.0

    raw = (close[target[idx]] - close[idx]) / close[idx] * 100.0
    win = (raw * direction[idx]) > 0
    up = raw > 0
    is_long = direction[idx] == 1
    edge = _edge_index(float(win.sum()), float(up.sum()), float(is_long.sum()), float(idx.size))

    sessions = arrays.session_id[idx]
    _, session_index = np.unique(sessions, return_inverse=True)
    size = session_index.max() + 1
    per_n = np.bincount(session_index, minlength=size).astype(np.float64)
    per_win = np.bincount(session_index, weights=win.astype(np.float64), minlength=size)
    per_up = np.bincount(session_index, weights=up.astype(np.float64), minlength=size)
    per_long = np.bincount(session_index, weights=is_long.astype(np.float64), minlength=size)

    edges = np.empty(BOOTSTRAP_ITERATIONS)
    for b in range(BOOTSTRAP_ITERATIONS):
        pick = rng.integers(0, size, size=size)
        total = per_n[pick].sum()
        edges[b] = (
            _edge_index(per_win[pick].sum(), per_up[pick].sum(), per_long[pick].sum(), total)
            if total else 0.0
        )
    ci_low, ci_high = np.percentile(edges, [5, 95])
    return int(idx.size), edge, float(ci_low), float(ci_high)


def _verdict(ci_low: float, ci_high: float) -> str:
    if ci_low > 0:
        return "POSITIVE"
    if ci_high < 0:
        return "BACKWARDS"
    return "-"


def _report_row(index_symbol: str, label: str, bucket: str, n: int, edge: float, ci_low: float, ci_high: float) -> None:
    if n < MIN_SIGNALS:
        logger.info("  %-10s %-14s %-14s n=%-4d  [below %d-signal trust minimum]", index_symbol, label, bucket, n, MIN_SIGNALS)
        return
    logger.info(
        "  %-10s %-14s %-14s n=%-4d  edge=%+7.2fpp  [%+6.2f, %+6.2f]  %s",
        index_symbol, label, bucket, n, edge, ci_low, ci_high, _verdict(ci_low, ci_high),
    )


def run_part_a(db_path: str, table: str, interval: str) -> None:
    logger.info("=" * 108)
    logger.info("PART A: AUTONOMOUS AI'S OWN CONSTRUCTION (EMA9/21 direction, ADX >= 18) -- CHOP_ZONE vs open window")
    logger.info("=" * 108)

    connection = sqlite3.connect(db_path)
    try:
        symbols = [
            row[0] for row in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {table} WHERE interval = ?", (interval,),
            )
        ]
    finally:
        connection.close()

    rng = np.random.default_rng(SEED)
    any_result = False
    for symbol in sorted(s for s in symbols if not s.upper().endswith("_FUT")):
        bars = load_bars_sqlite(db_path, table, symbol, interval)
        if len(bars) < 500:
            continue
        arrays = build_arrays(symbol, bars)
        direction = _trend_direction(arrays)
        eligible = _adx_floor_eligible(arrays)
        is_chop_zone, is_open_window = _session_phase_buckets(arrays)

        for bucket_name, bucket_mask in (("CHOP_ZONE", is_chop_zone), ("open_window", is_open_window)):
            n, edge, ci_low, ci_high = _evaluate(arrays, eligible & bucket_mask, direction, HORIZON_BARS, rng)
            any_result = any_result or n >= MIN_SIGNALS
            _report_row(symbol, "ema9/21+adx18", bucket_name, n, edge, ci_low, ci_high)

    if not any_result:
        logger.error(
            "No (index, bucket) cell reached %s signals. Nothing to report -- most likely no real "
            "candle data in this environment (expected in this sandbox).",
            MIN_SIGNALS,
        )
        return

    logger.info("-" * 108)
    logger.info(
        "Read CHOP_ZONE against open_window, not against zero: the gate is supported only if "
        "CHOP_ZONE reads reliably worse (BACKWARDS while open_window reads POSITIVE, or a materially "
        "lower edge), on BOTH indices. If CHOP_ZONE reads comparable to or better than open_window, "
        "that is real evidence the block is over-broad, not correctly targeted -- and per this "
        "project's own replication standard, a single-index result is suggestive, not confirmed."
    )


def run_part_b(db_path: str, table: str, interval: str) -> None:
    logger.info("=" * 108)
    logger.info("PART B: CROSS-CHECK WITH ALREADY-REGISTERED SETUPS (ST_ALIGNED, EMA_STACK) -- CHOP_ZONE vs open window")
    logger.info("=" * 108)

    connection = sqlite3.connect(db_path)
    try:
        symbols = [
            row[0] for row in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {table} WHERE interval = ?", (interval,),
            )
        ]
    finally:
        connection.close()

    rng = np.random.default_rng(SEED)
    any_result = False
    for symbol in sorted(s for s in symbols if not s.upper().endswith("_FUT")):
        bars = load_bars_sqlite(db_path, table, symbol, interval)
        if len(bars) < 500:
            continue
        arrays = build_arrays(symbol, bars)
        is_chop_zone, is_open_window = _session_phase_buckets(arrays)

        for setup_name in CROSS_CHECK_SETUPS:
            setup = Setup(setup_name)
            direction = build_signals(arrays, setup)
            assert_causal(arrays, setup, direction)

            for bucket_name, bucket_mask in (("CHOP_ZONE", is_chop_zone), ("open_window", is_open_window)):
                n, edge, ci_low, ci_high = _evaluate(arrays, bucket_mask, direction, HORIZON_BARS, rng)
                any_result = any_result or n >= MIN_SIGNALS
                _report_row(symbol, setup.label, bucket_name, n, edge, ci_low, ci_high)

    if not any_result:
        logger.error(
            "No (index, setup, bucket) cell reached %s signals. Nothing to report -- most likely no "
            "real candle data in this environment (expected in this sandbox).",
            MIN_SIGNALS,
        )
        return

    logger.info("-" * 108)
    logger.info(
        "These two setups are the ones the 31 Jul 2026 walk-forward already found carry a real, "
        "replicated edge in the 11:00-14:00 window -- this re-slices that same signal strictly to "
        "CHOP_ZONE's own 11:15-13:30 boundary. If CHOP_ZONE still reads POSITIVE here, that is a "
        "second, independent line of evidence (different signal construction, same underlying "
        "market) that the window itself is not structurally choppy -- consistent with, not proof "
        "on its own of, PART A's own finding."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    args = parser.parse_args()

    run_part_a(args.db, args.table, args.interval)
    run_part_b(args.db, args.table, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
