"""Part 0: is the drift-band edge real, or an artefact of overlapping samples?

WHY THE FIRST RUN'S z-SCORES CANNOT BE TRUSTED
----------------------------------------------
The baseline samples every 5-minute bar with a 60-minute forward window, so
consecutive samples share 11 of their 12 forward bars. They are not independent
observations, but the binomial standard error assumes they are. That inflates z
by roughly sqrt(12) ~= 3.5x. A reported z of -4.52 is closer to -1.3 once
corrected, and none of the three "significant" bands survives that.

Stacked on top: those bands were SELECTED by scanning 4 buckets x 2 indices x
2 horizons = 16 comparisons on the same data. At alpha = 0.05 roughly one false
positive is expected by chance alone.

WHAT THIS SCRIPT DOES
---------------------
0a. Non-overlapping subsample -- every 12th eligible bar at the 60-minute
    horizon, every 6th at 30-minute, so no two samples share a forward bar.

0b. Block bootstrap -- resample contiguous ONE-DAY blocks with replacement.
    Days are the natural independence unit here: bars within a day are
    autocorrelated, days are much less so. This handles the dependence properly
    rather than by the crude n/12 rule, and the resulting interval is the
    number worth quoting.

0c. Multiple-comparison context -- how many bands were tested and what the
    corrected threshold is.

DECISION RULE (stated before seeing results, deliberately):
  * A band whose bootstrap CI excludes zero has a real edge.
  * If every CI straddles zero, the entry signal is indistinguishable from
    noise at this sample size. That is a finding, not a failure, and the
    correct response is to stop -- not to search harder for a filter.

Usage:
    python -m scripts.band_significance --db data/trading.db
    python -m scripts.band_significance --db data/trading.db --iterations 2000
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

from scripts.backtest.data import build_arrays, forward_window_bounds, load_bars_sqlite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("band_significance")

LOOKBACK_MINUTES = 45
BARS_PER_LOOKBACK = LOOKBACK_MINUTES // 5
TRADING_START = time(9, 45)
TRADING_END = time(15, 15)

# (label, low, high) in absolute drift percent. Same bands as the baseline so
# the results are directly comparable.
BANDS = (
    ("0.00-0.10", 0.0, 0.10),
    ("0.10-0.25", 0.10, 0.25),
    ("0.25-0.50", 0.25, 0.50),
    ("0.50+", 0.50, 99.0),
)
HORIZONS = ((6, "30min"), (12, "60min"))


@dataclass
class BandResult:
    index_symbol: str
    horizon: str
    band: str
    regime: str
    n_full: int
    edge_full: float
    n_indep: int
    edge_indep: float
    z_indep: float
    p_indep: float
    ci_low: float
    ci_high: float

    @property
    def survives(self) -> bool:
        """Direction matters. A CI excluding zero on the NEGATIVE side is not a
        tradeable edge for a rule that trades WITH the drift -- it is evidence
        the rule is backwards in that band. Only a positive CI supports
        proceeding to Part C as specified."""
        return self.ci_low > 0

    @property
    def reliably_negative(self) -> bool:
        return self.ci_high < 0


def _eligible_and_signal(arrays):
    """Eligible entry bars, drift direction and drift magnitude."""
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    same_session = np.zeros(len(arrays), dtype=bool)
    same_session[BARS_PER_LOOKBACK:] = (
        arrays.session_id[BARS_PER_LOOKBACK:] == arrays.session_id[:-BARS_PER_LOOKBACK]
    )
    eligible = in_window & warm & same_session

    close = arrays.close.astype(np.float64)
    past = np.full(len(arrays), np.nan)
    past[BARS_PER_LOOKBACK:] = close[:-BARS_PER_LOOKBACK]
    with np.errstate(invalid="ignore"):
        drift = (close - past) / past * 100.0
    direction = np.zeros(len(arrays), dtype=np.int8)
    direction[drift > 0] = 1
    direction[drift < 0] = -1
    return eligible, direction, drift


def _session_masks(arrays, split: bool) -> dict[str, np.ndarray]:
    """Time-of-day partitions.

    The original drift test POOLED all times of day. The indicator work
    subsequently found that momentum setups are positive 11:00-14:00 and
    reliably backwards after 14:00 -- and a pooled test across two windows of
    opposite sign will report the net, which is roughly what it did report.
    That does not overturn the drift result, but it means the drift rule was
    never tested conditionally, so this re-runs it split.
    """
    n = len(arrays)
    masks: dict[str, np.ndarray] = {"all": np.ones(n, dtype=bool)}
    if not split:
        return masks
    minutes = arrays.minutes_since_open
    masks["0945_1100"] = minutes < 105
    masks["1100_1400"] = (minutes >= 105) & (minutes < 285)
    masks["1400_1515"] = minutes >= 285
    return masks


def _edge(wins: int, ups: int, longs: int, n: int) -> float:
    """Hit rate minus base rate, in percentage points.

    The null is NOT 50%. Over a rising sample an always-long rule beats a coin
    flip on drift alone, so the base rate is the unconditional up-rate applied
    in whichever direction the rule chose.
    """
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _analyse_band(
    arrays, eligible, direction, drift, forward_bars: int, low: float, high: float, iterations: int, rng
) -> tuple[int, float, int, float, float, float, float, float]:
    n_bars = len(arrays)
    close = arrays.close.astype(np.float64)
    bounds = forward_window_bounds(arrays, forward_bars)
    target = np.minimum(np.arange(n_bars) + forward_bars, bounds)

    magnitude = np.abs(drift)
    with np.errstate(invalid="ignore"):
        in_band = (magnitude >= low) & (magnitude < high)
    valid = eligible & in_band & (direction != 0) & (target > np.arange(n_bars))
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return 0, 0.0, 0, 0.0, 0.0, 1.0, 0.0, 0.0

    raw = (close[target[idx]] - close[idx]) / close[idx] * 100.0
    win = (raw * direction[idx]) > 0
    up = raw > 0
    is_long = direction[idx] == 1

    edge_full = _edge(int(win.sum()), int(up.sum()), int(is_long.sum()), idx.size)

    # --- 0a. Non-overlapping subsample --------------------------------------
    # Stride by the forward window so no two retained samples share a bar.
    keep = np.zeros(idx.size, dtype=bool)
    last_kept = -10**9
    for k, bar_index in enumerate(idx):
        if bar_index - last_kept >= forward_bars:
            keep[k] = True
            last_kept = bar_index
    n_indep = int(keep.sum())
    edge_indep = _edge(int(win[keep].sum()), int(up[keep].sum()), int(is_long[keep].sum()), n_indep)
    if n_indep > 0:
        up_rate = up[keep].mean()
        longs = is_long[keep].sum()
        base = (longs * up_rate + (n_indep - longs) * (1 - up_rate)) / n_indep
        se = math.sqrt(base * (1 - base) / n_indep) if 0 < base < 1 else 0.0
        z = ((win[keep].mean() - base) / se) if se > 0 else 0.0
        p = math.erfc(abs(z) / math.sqrt(2))
    else:
        z, p = 0.0, 1.0

    # --- 0b. Block bootstrap over whole days --------------------------------
    # Per-session scalar aggregates, so an iteration is four array lookups and
    # a sum rather than a concatenation. 2,000 iterations then costs almost
    # nothing in memory, which matters on a 145 MB budget.
    sessions = arrays.session_id[idx]
    unique_sessions, session_index = np.unique(sessions, return_inverse=True)
    per_n = np.bincount(session_index, minlength=unique_sessions.size).astype(np.float64)
    per_win = np.bincount(session_index, weights=win.astype(np.float64), minlength=unique_sessions.size)
    per_up = np.bincount(session_index, weights=up.astype(np.float64), minlength=unique_sessions.size)
    per_long = np.bincount(session_index, weights=is_long.astype(np.float64), minlength=unique_sessions.size)

    n_sessions = unique_sessions.size
    edges = np.empty(iterations, dtype=np.float64)
    for b in range(iterations):
        pick = rng.integers(0, n_sessions, size=n_sessions)
        total_n = per_n[pick].sum()
        if total_n == 0:
            edges[b] = 0.0
            continue
        edges[b] = _edge(
            per_win[pick].sum(), per_up[pick].sum(), per_long[pick].sum(), total_n
        )
    ci_low, ci_high = np.percentile(edges, [5, 95])
    return idx.size, edge_full, n_indep, edge_indep, z, p, float(ci_low), float(ci_high)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument(
        "--by-session", action="store_true",
        help="Split by time of day. The original test pooled all sessions, and the "
             "indicator work found momentum is positive 11:00-14:00 and backwards after "
             "14:00 -- a pooled test across windows of opposite sign reports the net.",
    )
    parser.add_argument(
        "--horizons", default="",
        help="Comma-separated forward-bar counts, e.g. '3' for 15 min. Needed for the "
             "14:00-15:15 regime, where a 60-minute window is clipped at session end and "
             "the stated horizon is not the one measured.",
    )
    parser.add_argument("--seed", type=int, default=20260730)
    args = parser.parse_args()

    horizons = HORIZONS
    if args.horizons:
        horizons = tuple(
            (int(b.strip()), f"{int(b.strip()) * 5}min")
            for b in args.horizons.split(",") if b.strip()
        )
        logger.info("Horizon override: %s", ", ".join(label for _, label in horizons))

    rng = np.random.default_rng(args.seed)

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

    results: list[BandResult] = []
    for symbol in sorted(symbols):
        bars = load_bars_sqlite(args.db, args.table, symbol, args.interval)
        if len(bars) < 500:
            logger.warning("%s: only %s bars, skipping", symbol, len(bars))
            continue
        logger.info("=" * 100)
        logger.info("%s | %s bars | %s to %s", symbol, len(bars), bars[0].ts_ist.date(), bars[-1].ts_ist.date())
        arrays = build_arrays(symbol, bars)
        eligible, direction, drift = _eligible_and_signal(arrays)

        regimes = _session_masks(arrays, args.by_session)
        for regime_name, regime_mask in regimes.items():
            eligible_regime = eligible & regime_mask
            for forward_bars, horizon_label in horizons:
                logger.info("  %s horizon | %s", horizon_label, regime_name)
                logger.info(
                    "    %-12s %8s %9s %8s %9s %8s %9s  %s",
                    "band", "n_full", "edge_full", "n_indep", "edge_ind", "z_ind", "p_ind", "bootstrap 90% CI",
                )
                for band_label, low, high in BANDS:
                    (n_full, edge_full, n_indep, edge_indep, z, p, ci_low, ci_high) = _analyse_band(
                        arrays, eligible_regime, direction, drift, forward_bars, low, high, args.iterations, rng
                    )
                    if n_full == 0:
                        continue
                    verdict = "EXCLUDES ZERO" if (ci_low > 0 or ci_high < 0) else "straddles zero"
                    logger.info(
                        "    %-12s %8d %+8.2fpp %8d %+8.2fpp %+8.2f %9.4f  [%+.2f, %+.2f]  %s",
                        band_label, n_full, edge_full, n_indep, edge_indep, z, p, ci_low, ci_high, verdict,
                    )
                    results.append(
                        BandResult(symbol, horizon_label, band_label, regime_name, n_full, edge_full,
                                   n_indep, edge_indep, z, p, ci_low, ci_high)
                    )

    # --- 0c. Multiple-comparison context ------------------------------------
    logger.info("=" * 100)
    tested = len(results)
    bonferroni = 0.05 / tested if tested else float("nan")
    logger.info("Multiple-comparison context:")
    logger.info("  bands tested: %s (%s indices x %s bands x %s horizons x %s regimes)",
                tested, len(symbols), len(BANDS), len(horizons),
                len(_session_masks(arrays, args.by_session)))
    logger.info("  Bonferroni-corrected alpha: %.5f (uncorrected 0.05)", bonferroni)
    clearing = [r for r in results if r.p_indep < bonferroni]
    logger.info("  bands clearing corrected alpha on the independent subsample: %s", len(clearing))

    survivors = [r for r in results if r.survives]
    negatives = [r for r in results if r.reliably_negative]
    logger.info("=" * 100)
    if survivors:
        logger.info("VERDICT: %s band(s) show POSITIVE edge with a CI excluding zero:", len(survivors))
        for r in survivors:
            logger.info(
                "  %s %s %s %s -> edge %+.2fpp, CI [%+.2f, %+.2f], n_indep=%s",
                r.index_symbol, r.horizon, r.band, r.regime, r.edge_indep, r.ci_low, r.ci_high, r.n_indep,
            )
        logger.info(
            "These are candidates for Part C. They were selected from the same two "
            "years the sweep would run on, so the locked holdout carries more weight "
            "than usual -- any sweep conditional on them inherits that bias."
        )
    else:
        logger.info(
            "VERDICT: NO band shows a positive edge with a CI excluding zero. Trading "
            "WITH 45-minute drift has no detectable edge across two years."
        )
        logger.info(
            "Per the decision rule: STOP. Do not run the gate sweep on the momentum "
            "premise. The answer is to stop paying to rediscover this 15 trades at a "
            "time, not to search harder for a filter."
        )

    if negatives:
        logger.info("-" * 100)
        logger.info(
            "%s band(s) are reliably NEGATIVE (CI entirely below zero). The rule is not "
            "merely uninformative there, it is backwards:", len(negatives),
        )
        for r in negatives:
            logger.info(
                "  %s %s %s %s -> edge %+.2fpp, CI [%+.2f, %+.2f], n_full=%s",
                r.index_symbol, r.horizon, r.band, r.regime, r.edge_full, r.ci_low, r.ci_high, r.n_full,
            )
        logger.info(
            "Before treating this as a fade signal, note two things. (1) These bands "
            "were selected on the same data, so an inverted rule inherits the full "
            "selection bias and MUST be validated on the untouched holdout. (2) At "
            "symmetric +/-12%% payoffs a 2pp hit-rate edge is worth about 0.48%% per "
            "trade against ~0.56%% costs -- still net negative. Inversion only pays if "
            "the win/loss RATIO is asymmetric, which is the Part C question."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
