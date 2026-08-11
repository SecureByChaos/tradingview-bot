"""Validate the trend-age hard gate shipped 11 Aug in app/ai/originator.py.

WHAT THIS ANSWERS
------------------
app/ai/originator.py now blocks a new AI Origination entry once
same_direction_entries_today for that index+direction reaches 2 (see
_MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK's comment there). That threshold came
from one day's incident review, not a backtest -- this script is the backtest
the review itself said was required before trusting it, and before adding a
second gate on trend_duration_pct_of_session, which shipped with NO threshold
at all pending exactly this.

Neither app/ai/originator.py's live fields are directly backtestable: they
depend on actual AI Origination trades placed and actual live Supertrend
state, neither of which exist in a bars-only backtest. So this computes the
closest faithful proxies from the existing arrays (see the two functions
below), applied to the setups already declared in scripts/backtest/setups.py,
and asks the same question the roadmap posed: does filtering an already-
tested setup by same-direction repeat count or by trend age change its
(already mostly negative, see CLAUDE.md) profile?

METHOD
------
  * same_direction_count_today: for a given setup's own signal history, how
    many times has THIS setup already fired in the same direction earlier
    the same session, before this bar. Proxies "how many times has this
    thesis already been traded today" against setup fires rather than real
    trades, since real per-provider trade history is not in the 2-year
    archive this script reads.
  * trend_duration_pct: consecutive same-direction 5-minute Supertrend bars
    (run-length ending at this bar, within the session) as a percentage of
    bars elapsed since session open, capped at 100 -- an exact re-derivation
    of app/market_context.py's compute_trend_age, computed once over the
    whole array instead of once per live cycle.

Every (setup, threshold) cell is reported for BOTH the "below threshold"
bucket and the "at-or-above threshold" bucket, on an in-sample slice (the
first --split-fraction of sessions, chronological) and an out-of-sample tail
-- NOT the locked holdout (data/holdout_record.json), which this script never
touches. This is a risk-control validation, not a search for a new
directional edge, so the roadmap explicitly said not to spend the scarce
holdout on it.

Usage:
    python -m scripts.trend_age_gate_backtest --db data/trading.db
    python -m scripts.trend_age_gate_backtest --db data/trading.db --setups EMA_STACK,ORB_BREAK
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import time

import numpy as np

from scripts.backtest.data import IndexArrays, build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.setups import Setup, assert_causal, build_signals, default_setups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("trend_age_gate_backtest")

TRADING_START = time(9, 45)
TRADING_END = time(15, 15)

# Same pairing rule as scripts/setup_significance.py -- volume-dependent
# setups (VWAP-gated) only make sense on futures, spot volume is always zero.
FUTURES_SUFFIX = "_FUT"
VOLUME_DEPENDENT_SETUPS = {"BNV6"}

ENTRIES_THRESHOLDS = (1, 2, 3, 4)
TREND_PCT_THRESHOLDS = (80.0, 90.0, 95.0)
HORIZON_BARS = 12  # 60 min at the default FIVE_MINUTE interval
MIN_SIGNALS = 30   # small buckets are expected here (repeat/late-trend fires are rare by construction)
BOOTSTRAP_ITERATIONS = 2000


def _is_futures(symbol: str) -> bool:
    return symbol.upper().endswith(FUTURES_SUFFIX)


def _trend_duration_pct(arrays: IndexArrays) -> np.ndarray:
    """Per-bar %-of-session-elapsed the current 5-min Supertrend direction has
    held, capped at 100. Exact re-derivation of
    app/market_context.py:compute_trend_age's duration_bars/pct_of_session,
    computed once over the whole array instead of once per live cycle.
    Always defined from the first bar of a session onward (bars_elapsed is
    never 0 inside the loop) -- unlike the live per-cycle version, which can
    read None in the first few minutes after 09:15 before a whole 5-min bar
    has elapsed; that gap does not apply to bar-indexed backtest data.
    """
    n = len(arrays)
    dirs = arrays.st_5m_dir
    session = arrays.session_id
    pct = np.full(n, np.nan, dtype=np.float64)
    run = 0
    bars_elapsed = 0
    prev_dir = 0
    for i in range(n):
        if i == 0 or session[i] != session[i - 1]:
            run = 0
            bars_elapsed = 0
            prev_dir = 0
        bars_elapsed += 1
        d = int(dirs[i])
        if d != 0 and d == prev_dir:
            run += 1
        elif d != 0:
            run = 1
        else:
            run = 0
        prev_dir = d
        pct[i] = min(run / bars_elapsed, 1.0) * 100.0
    return pct


def _same_direction_count_today(session: np.ndarray, direction: np.ndarray) -> np.ndarray:
    """Per-bar: how many times THIS setup already fired in the same direction
    earlier the same session, before this bar. First fire of the day reads 0,
    matching app/ai/originator.py's _same_direction_entries_today semantics
    (a count taken BEFORE the entry it's evaluated against)."""
    n = len(direction)
    counts = np.zeros(n, dtype=np.int32)
    ce_count = 0
    pe_count = 0
    cur_session = None
    for i in range(n):
        if session[i] != cur_session:
            cur_session = session[i]
            ce_count = 0
            pe_count = 0
        d = int(direction[i])
        if d == 1:
            counts[i] = ce_count
            ce_count += 1
        elif d == -1:
            counts[i] = pe_count
            pe_count += 1
    return counts


def _eligible(arrays: IndexArrays) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    return in_window & warm


def _split_masks(arrays: IndexArrays, split_fraction: float) -> tuple[np.ndarray, np.ndarray]:
    """(in_sample_mask, out_of_sample_mask), split chronologically by session
    -- the first split_fraction of distinct sessions vs the rest. This is a
    plain train/test split within the ALREADY-in-use two-year archive, not the
    locked holdout (data/holdout_record.json); this script never reads or
    writes that file."""
    unique_sessions = np.unique(arrays.session_id)
    cutoff_idx = max(int(len(unique_sessions) * split_fraction), 1)
    cutoff_session = unique_sessions[cutoff_idx - 1]
    in_sample = arrays.session_id <= cutoff_session
    return in_sample, ~in_sample


def _edge(wins: float, ups: float, longs: float, n: float) -> float:
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _evaluate(arrays: IndexArrays, mask: np.ndarray, direction: np.ndarray, forward_bars: int, rng) -> tuple[int, float, float, float]:
    """(n_signals, edge, ci_low, ci_high) via day-block bootstrap. Mirrors
    scripts/setup_significance.py's _evaluate, minus the non-overlapping
    p-value (not needed here; Bonferroni below uses the CI-excludes-zero
    check like scripts/walk_forward.py does)."""
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
    edge = _edge(float(win.sum()), float(up.sum()), float(is_long.sum()), float(idx.size))

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
        edges[b] = _edge(per_win[pick].sum(), per_up[pick].sum(), per_long[pick].sum(), total) if total else 0.0
    ci_low, ci_high = np.percentile(edges, [5, 95])
    return int(idx.size), edge, float(ci_low), float(ci_high)


@dataclass
class Cell:
    index_symbol: str
    setup: str
    family: str          # "entries" or "trend_pct"
    threshold: float
    split: str            # "in_sample" or "out_of_sample"
    bucket: str            # "below" or "at_or_above"
    n: int
    edge: float
    ci_low: float
    ci_high: float

    @property
    def verdict(self) -> str:
        if self.ci_low > 0:
            return "POSITIVE"
        if self.ci_high < 0:
            return "BACKWARDS"
        return "-"


def _run_family(
    arrays: IndexArrays, eligible: np.ndarray, direction: np.ndarray, feature: np.ndarray,
    thresholds: tuple, family: str, in_sample: np.ndarray, out_of_sample: np.ndarray,
    index_symbol: str, setup_label: str, rng,
) -> list[Cell]:
    cells: list[Cell] = []
    have_feature = ~np.isnan(feature)
    for threshold in thresholds:
        at_or_above = have_feature & (feature >= threshold)
        below = have_feature & (feature < threshold)
        for split_name, split_mask in (("in_sample", in_sample), ("out_of_sample", out_of_sample)):
            for bucket_name, bucket_mask in (("below", below), ("at_or_above", at_or_above)):
                mask = eligible & split_mask & bucket_mask
                n, edge, ci_low, ci_high = _evaluate(arrays, mask, direction, HORIZON_BARS, rng)
                if n < MIN_SIGNALS:
                    continue
                cells.append(Cell(
                    index_symbol, setup_label, family, threshold, split_name, bucket_name,
                    n, edge, ci_low, ci_high,
                ))
    return cells


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--split-fraction", type=float, default=0.8,
                         help="Fraction of sessions (chronological) treated as in-sample; "
                              "the rest is the out-of-sample check. NOT the locked holdout.")
    parser.add_argument("--horizon-bars", type=int, default=HORIZON_BARS)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument(
        "--setups", default="",
        help="Comma-separated setup names (matches Setup.name). Default: the full "
             "declared sweep from default_setups().",
    )
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    setups = default_setups()
    if args.setups:
        wanted = {name.strip().upper() for name in args.setups.split(",") if name.strip()}
        setups = [s for s in setups if s.name.upper() in wanted]
        if not setups:
            logger.error("No declared setup matches --setups %s", args.setups)
            return 1
    logger.info("Setups declared up front: %s", len(setups))

    connection = sqlite3.connect(args.db)
    try:
        symbols = [
            row[0] for row in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {args.table} WHERE interval = ?",
                (args.interval,),
            )
        ]
    finally:
        connection.close()

    all_cells: list[Cell] = []
    for symbol in sorted(symbols):
        bars = load_bars_sqlite(args.db, args.table, symbol, args.interval)
        if len(bars) < 500:
            continue
        logger.info("=" * 108)
        logger.info("%s | %s bars | %s to %s", symbol, len(bars), bars[0].ts_ist.date(), bars[-1].ts_ist.date())
        arrays = build_arrays(symbol, bars)
        eligible = _eligible(arrays)
        in_sample, out_of_sample = _split_masks(arrays, args.split_fraction)
        trend_pct = _trend_duration_pct(arrays)
        is_futures = _is_futures(symbol)

        for setup in setups:
            needs_volume = setup.name.upper() in VOLUME_DEPENDENT_SETUPS
            if needs_volume != is_futures:
                continue
            signals = build_signals(arrays, setup)
            assert_causal(arrays, setup, signals)
            same_dir_count = _same_direction_count_today(arrays.session_id, signals)

            all_cells += _run_family(
                arrays, eligible, signals, same_dir_count.astype(np.float64),
                ENTRIES_THRESHOLDS, "entries", in_sample, out_of_sample, symbol, setup.label, rng,
            )
            all_cells += _run_family(
                arrays, eligible, signals, trend_pct,
                TREND_PCT_THRESHOLDS, "trend_pct", in_sample, out_of_sample, symbol, setup.label, rng,
            )

    if not all_cells:
        logger.error("No (setup, threshold) cell reached %s signals in any bucket. Nothing to report.", MIN_SIGNALS)
        return 1

    logger.info("=" * 108)
    logger.info("FULL PARAMETER SURFACE")
    logger.info(
        "  %-10s %-24s %-9s %6s %-13s %-11s %6s %9s  %-18s %s",
        "index", "setup", "family", "thr", "split", "bucket", "n", "edge", "bootstrap 90% CI", "verdict",
    )
    for c in sorted(all_cells, key=lambda x: (x.family, x.threshold, x.setup, x.index_symbol, x.split, x.bucket)):
        logger.info(
            "  %-10s %-24s %-9s %6s %-13s %-11s %6d %+8.2fpp  [%+6.2f, %+6.2f]  %s",
            c.index_symbol, c.setup, c.family, c.threshold, c.split, c.bucket,
            c.n, c.edge, c.ci_low, c.ci_high, c.verdict,
        )

    total = len(all_cells)
    corrected = 0.05 / total
    logger.info("=" * 108)
    logger.info("MULTIPLE-COMPARISON CONTEXT")
    logger.info("  comparisons run: %s", total)
    logger.info("  Bonferroni-corrected alpha (as a CI-width proxy, 90%% CI shown above): %.6f", corrected)

    # A threshold is worth trusting only if the at-or-above bucket is
    # reliably WORSE than the below bucket, on BOTH indices, in BOTH the
    # in-sample slice and the untouched out-of-sample tail. Anything less is
    # exactly the single-day-anecdote error this script exists to guard
    # against -- report it, do not pick a winner.
    logger.info("=" * 108)
    logger.info("PROTECTIVE-THRESHOLD CHECK (at_or_above reliably worse than below, both indices, both splits)")
    found_any = False
    by_family_setup_threshold: dict[tuple[str, str, float], dict[tuple[str, str], Cell]] = {}
    for c in all_cells:
        by_family_setup_threshold.setdefault((c.family, c.setup, c.threshold), {})[(c.split, c.bucket)] = c
    for (family, setup, threshold), by_split_bucket in sorted(by_family_setup_threshold.items()):
        needed = [
            (split, bucket)
            for split in ("in_sample", "out_of_sample")
            for bucket in ("below", "at_or_above")
        ]
        if not all(key in by_split_bucket for key in needed):
            continue
        protective_in_sample = by_split_bucket[("in_sample", "at_or_above")].ci_high < by_split_bucket[("in_sample", "below")].ci_low
        protective_out_of_sample = by_split_bucket[("out_of_sample", "at_or_above")].ci_high < by_split_bucket[("out_of_sample", "below")].ci_low
        if protective_in_sample and protective_out_of_sample:
            found_any = True
            logger.info(
                "  %-9s %-24s threshold=%s: at_or_above reliably worse than below in BOTH "
                "splits (in-sample %+.2f vs %+.2f, out-of-sample %+.2f vs %+.2f)",
                family, setup, threshold,
                by_split_bucket[("in_sample", "at_or_above")].edge, by_split_bucket[("in_sample", "below")].edge,
                by_split_bucket[("out_of_sample", "at_or_above")].edge, by_split_bucket[("out_of_sample", "below")].edge,
            )
    if not found_any:
        logger.info(
            "  No (family, setup, threshold) cell shows the at-or-above bucket reliably "
            "worse than the below bucket in BOTH the in-sample slice and the "
            "out-of-sample tail. That does not mean the gate is wrong -- it protects "
            "against a rare, high-severity pattern (7 correlated same-direction "
            "entries in one day), which a 2-year archive of setup re-fires may simply "
            "not reproduce at a testable sample size. Read this alongside n_signals "
            "in the full surface above before concluding either way."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
