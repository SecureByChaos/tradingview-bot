"""Item 2: do any indicator-based setups have a directional edge?

Drift is dead (see scripts/band_significance.py). These are different inputs
and have never been tested. Method is identical to Part 0 so the results are
directly comparable:

  * edge measured against the UNCONDITIONAL base rate, not 50%
  * non-overlapping subsample AND day-block bootstrap
  * direction-aware verdict -- a CI below zero means the setup is BACKWARDS,
    which is a different finding from an exploitable edge

WHAT IS DIFFERENT FROM PART 0, AND WHY IT MATTERS MORE
------------------------------------------------------
Sample sizes are far smaller. ORB fires once or twice a day, so expect
hundreds to a few thousand signals over two years rather than 31,000 bars.
Overlapping-window inflation matters less; small-sample noise matters much
more. The bootstrap is load-bearing here, not a formality.

Multiple comparisons are now the dominant risk. This is the SECOND round of
hypothesis testing on the same two years, across setups x parameters x indices
x horizons x regimes -- several hundred comparisons. Something will look
excellent by chance. Defences, in order of how much weight they carry:

  1. Consistency across partitions (both indices, both horizons, several
     regimes) beats any single low p-value. That is what carried the drift
     result, and it is the only defence that does not depend on a threshold.
  2. The reported comparison count and corrected alpha.
  3. The holdout -- single-use, at most two candidates.

Usage:
    python -m scripts.setup_significance --db data/trading.db
    python -m scripts.setup_significance --db data/trading.db --regimes
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time

import numpy as np

from app.market_context import ADX_NO_TREND, CPR_NARROW_MAX_PERCENT
from scripts.backtest.data import build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.setups import Setup, assert_causal, build_signals, default_setups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("setup_significance")

TRADING_START = time(9, 45)
TRADING_END = time(15, 15)
HORIZONS = ((6, "30min"), (12, "60min"))
MIN_SIGNALS = 100          # below this the bootstrap is not informative
BOOTSTRAP_ITERATIONS = 2000

# Forward windows are CLIPPED at session end (see forward_window_bounds). A
# 60-minute horizon therefore measures ~45 minutes from a 14:30 signal and ~15
# from a 15:00 one, so inside the 14:00-15:15 bucket the "60min" label is wrong
# and the effective horizon shrinks systematically across the window. That is a
# distortion correlated with the exact variable under test, which is why
# --horizons exists: re-run that bucket at a horizon that FITS inside it
# (3 bars = 15 min) and see whether the reversal survives.
TRUNCATION_WARNING_REGIMES = {"1400_1515"}


@dataclass
class Result:
    index_symbol: str
    horizon: str
    setup: str
    regime: str
    n_signals: int
    n_indep: int
    edge: float
    ci_low: float
    ci_high: float
    p_indep: float

    @property
    def survives(self) -> bool:
        return self.ci_low > 0

    @property
    def reliably_negative(self) -> bool:
        return self.ci_high < 0


def _eligible(arrays) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_window = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    return in_window & warm


def _regime_masks(arrays, with_regimes: bool) -> dict[str, np.ndarray]:
    n = len(arrays)
    masks: dict[str, np.ndarray] = {"all": np.ones(n, dtype=bool)}
    if not with_regimes:
        return masks
    cpr = arrays.cpr_width_pct
    adx = arrays.adx14
    minutes = arrays.minutes_since_open
    masks["cpr_narrow"] = ~np.isnan(cpr) & (cpr <= CPR_NARROW_MAX_PERCENT)
    masks["cpr_wide"] = ~np.isnan(cpr) & (cpr > CPR_NARROW_MAX_PERCENT)
    masks["adx_lt20"] = ~np.isnan(adx) & (adx < ADX_NO_TREND)
    masks["adx_ge20"] = ~np.isnan(adx) & (adx >= ADX_NO_TREND)
    # Labelled by clock time rather than "first hour" -- eligibility starts at
    # 09:45, so the opening slice is 75 minutes, not 60, and a label implying
    # otherwise would be misread later.
    masks["0945_1100"] = minutes < 105
    masks["1100_1400"] = (minutes >= 105) & (minutes < 285)
    masks["1400_1515"] = minutes >= 285
    return masks


def _edge(wins: float, ups: float, longs: float, n: float) -> float:
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _evaluate(
    arrays, mask: np.ndarray, direction: np.ndarray, forward_bars: int, rng
) -> tuple[int, int, float, float, float, float]:
    n_bars = len(arrays)
    close = arrays.close.astype(np.float64)
    bounds = forward_window_bounds(arrays, forward_bars)
    positions = np.arange(n_bars)
    target = np.minimum(positions + forward_bars, bounds)

    valid = mask & (direction != 0) & (target > positions)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return 0, 0, 0.0, 0.0, 0.0, 1.0

    raw = (close[target[idx]] - close[idx]) / close[idx] * 100.0
    win = (raw * direction[idx]) > 0
    up = raw > 0
    is_long = direction[idx] == 1
    edge = _edge(float(win.sum()), float(up.sum()), float(is_long.sum()), float(idx.size))

    # Non-overlapping subsample: stride so no two retained signals share a
    # forward bar.
    keep = np.zeros(idx.size, dtype=bool)
    last = -10**9
    for k, bar in enumerate(idx):
        if bar - last >= forward_bars:
            keep[k] = True
            last = bar
    n_indep = int(keep.sum())
    if n_indep > 0:
        up_rate = float(up[keep].mean())
        longs = float(is_long[keep].sum())
        base = (longs * up_rate + (n_indep - longs) * (1 - up_rate)) / n_indep
        se = math.sqrt(base * (1 - base) / n_indep) if 0 < base < 1 else 0.0
        z = ((float(win[keep].mean()) - base) / se) if se > 0 else 0.0
        p = math.erfc(abs(z) / math.sqrt(2))
    else:
        p = 1.0

    # Day-block bootstrap on per-session scalar aggregates.
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
    return idx.size, n_indep, edge, float(ci_low), float(ci_high), p


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--regimes", action="store_true", help="Also split by CPR / ADX / session third")
    parser.add_argument(
        "--horizons", default="",
        help="Comma-separated forward-bar counts, e.g. '3' for 15 min. Overrides the "
             "default 30/60 min. Use a horizon that fits inside the regime under test.",
    )
    parser.add_argument(
        "--end", default="",
        help="Last session date to include (YYYY-MM-DD). Use this to EXCLUDE the holdout "
             "window from selection -- a candidate chosen using holdout data is not "
             "testable on it.",
    )
    parser.add_argument("--start", default="", help="First session date to include (YYYY-MM-DD)")
    parser.add_argument("--seed", type=int, default=20260730)
    parser.add_argument(
        "--setups", default="",
        help="Comma-separated setup names to run (matches Setup.name, e.g. 'BNV6'). "
             "Default: run the full declared sweep. Filtering the already-declared list "
             "down to a subset is not the same as adding a setup after seeing results -- "
             "the full sweep was still fixed up front; this just chooses what to report "
             "from a single run.",
    )
    args = parser.parse_args()

    horizons = HORIZONS
    if args.horizons:
        horizons = tuple(
            (int(bars.strip()), f"{int(bars.strip()) * 5}min")
            for bars in args.horizons.split(",")
            if bars.strip()
        )
        logger.info("Horizon override: %s", ", ".join(label for _, label in horizons))

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

    results: list[Result] = []
    too_few: dict[str, dict[str, int]] = {}
    for symbol in sorted(symbols):
        bars = load_bars_sqlite(args.db, args.table, symbol, args.interval)
        if args.start:
            cutoff = datetime.strptime(args.start, "%Y-%m-%d").date()
            bars = [b for b in bars if b.ts_ist.date() >= cutoff]
        if args.end:
            cutoff = datetime.strptime(args.end, "%Y-%m-%d").date()
            bars = [b for b in bars if b.ts_ist.date() <= cutoff]
        if len(bars) < 500:
            continue
        logger.info("=" * 108)
        logger.info("%s | %s bars | %s to %s", symbol, len(bars), bars[0].ts_ist.date(), bars[-1].ts_ist.date())
        arrays = build_arrays(symbol, bars)
        eligible = _eligible(arrays)
        regimes = _regime_masks(arrays, args.regimes)

        for setup in setups:
            signals = build_signals(arrays, setup)
            assert_causal(arrays, setup, signals)
            for forward_bars, horizon_label in horizons:
                for regime_name, regime_mask in regimes.items():
                    mask = eligible & regime_mask
                    n_sig, n_indep, edge, ci_low, ci_high, p = _evaluate(
                        arrays, mask, signals, forward_bars, rng
                    )
                    if n_sig < MIN_SIGNALS:
                        # Record rather than silently drop. A setup that fires
                        # too rarely to test is a finding -- it means the
                        # strategy cannot be validated in any reasonable
                        # timeframe -- and silence looks identical to "not run".
                        if regime_name == "all":
                            too_few.setdefault(setup.label, {})[f"{symbol}/{horizon_label}"] = n_sig
                        continue
                    results.append(
                        Result(symbol, horizon_label, setup.label, regime_name,
                               n_sig, n_indep, edge, ci_low, ci_high, p)
                    )

    if not results:
        logger.error("No setup produced at least %s signals. Nothing to report.", MIN_SIGNALS)
        return 1

    # --- headline table, "all" regime only ---------------------------------
    logger.info("=" * 108)
    logger.info("ALL-REGIME RESULTS")
    logger.info(
        "  %-10s %-7s %-28s %8s %8s %9s  %-18s %s",
        "index", "horizon", "setup", "n_sig", "n_indep", "edge", "bootstrap 90% CI", "verdict",
    )
    for r in sorted(results, key=lambda x: (x.index_symbol, x.horizon, x.setup)):
        if r.regime != "all":
            continue
        verdict = "POSITIVE" if r.survives else ("BACKWARDS" if r.reliably_negative else "-")
        logger.info(
            "  %-10s %-7s %-28s %8d %8d %+8.2fpp  [%+6.2f, %+6.2f]  %s",
            r.index_symbol, r.horizon, r.setup, r.n_signals, r.n_indep, r.edge,
            r.ci_low, r.ci_high, verdict,
        )

    if args.regimes:
        logger.info("=" * 108)
        logger.info("REGIME SPLITS (only cells whose CI excludes zero)")
        truncated_seen = False
        for r in sorted(results, key=lambda x: (x.setup, x.index_symbol, x.regime)):
            if r.regime == "all" or not (r.survives or r.reliably_negative):
                continue
            # Flag cells where the forward window cannot fit inside the regime,
            # so a truncation artefact is never silently read as a result.
            truncated = r.regime in TRUNCATION_WARNING_REGIMES and r.horizon != "15min"
            truncated_seen = truncated_seen or truncated
            logger.info(
                "  %-10s %-7s %-28s %-12s n=%6d edge %+6.2fpp  [%+6.2f, %+6.2f]  %s%s",
                r.index_symbol, r.horizon, r.setup, r.regime, r.n_signals, r.edge,
                r.ci_low, r.ci_high, "POSITIVE" if r.survives else "BACKWARDS",
                "  [TRUNCATED WINDOW]" if truncated else "",
            )
        if truncated_seen:
            logger.warning(
                "  Cells marked TRUNCATED WINDOW sit in 14:00-15:15, where the forward "
                "window is clipped at session end -- the stated horizon is not the "
                "horizon actually measured. Re-run with --horizons 3 before treating "
                "any of them as a finding."
            )

    if too_few:
        logger.info("=" * 108)
        logger.info("EXCLUDED -- fewer than %s signals, so not testable:", MIN_SIGNALS)
        for label, counts in sorted(too_few.items()):
            detail = ", ".join(f"{k} n={v}" for k, v in sorted(counts.items()))
            logger.info("  %-28s %s", label, detail)
        logger.info(
            "  A strategy firing this rarely cannot be validated in a reasonable "
            "timeframe. That is a property of the strategy, not a gap in the test."
        )

    # --- multiple-comparison accounting -------------------------------------
    total = len(results)
    corrected = 0.05 / total
    logger.info("=" * 108)
    logger.info("MULTIPLE-COMPARISON CONTEXT")
    logger.info("  comparisons run: %s", total)
    logger.info("  Bonferroni-corrected alpha: %.6f (uncorrected 0.05)", corrected)
    clearing = [r for r in results if r.p_indep < corrected]
    logger.info("  clearing corrected alpha on the independent subsample: %s", len(clearing))
    for r in clearing:
        logger.info("    %s %s %s %s edge %+.2fpp p=%.6f",
                    r.index_symbol, r.horizon, r.setup, r.regime, r.edge, r.p_indep)

    positives = [r for r in results if r.survives]
    negatives = [r for r in results if r.reliably_negative]

    # Consistency is the defence that does not depend on a threshold: a real
    # effect should appear on both indices and at both horizons.
    #
    # Keyed on (setup, regime), NOT setup alone. An earlier version only
    # considered regime == "all" and therefore reported "0 consistent" while a
    # setup was replicating across both indices and both horizons WITHIN a
    # regime. A conditional effect is still an effect; it just isn't visible
    # in the pooled cell, which is the entire reason for splitting by regime.
    # Require both horizons only when both were actually run. With a single
    # horizon (e.g. --horizons 3) the two-horizon test is unsatisfiable, so
    # leaving it in would report "0 consistent" for structural reasons and read
    # as a negative result when nothing was tested.
    horizons_run = len({r.horizon for r in results})
    by_key: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for r in positives:
        by_key[(r.setup, r.regime)].append(r)
    consistent = {
        key: rows for key, rows in by_key.items()
        if len({r.index_symbol for r in rows}) == 2
        and (horizons_run < 2 or len({r.horizon for r in rows}) == 2)
    }
    if horizons_run < 2:
        logger.info(
            "Single horizon run: replication is assessed across indices only, not "
            "across horizons."
        )

    # The mirror check: the same setup/regime reliably BACKWARDS on both
    # indices is equally informative and equally hard to get by chance.
    by_key_neg: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for r in negatives:
        by_key_neg[(r.setup, r.regime)].append(r)
    consistent_negative = {
        key: rows for key, rows in by_key_neg.items()
        if len({r.index_symbol for r in rows}) == 2
    }

    logger.info("=" * 108)
    if not positives:
        logger.info(
            "VERDICT: no setup shows a positive edge with a CI excluding zero, in any "
            "regime, on either index."
        )
        logger.info(
            "Combined with the drift result, two years of index data have now failed to "
            "detect a tradeable directional edge from either momentum or indicator "
            "setups. The honest reading is that the edge is not in intraday index "
            "direction, and continuing to search here has poor expected value."
        )
    else:
        logger.info(
            "VERDICT: %s positive cell(s); %s (setup, regime) combination(s) replicate "
            "across BOTH indices and BOTH horizons.", len(positives), len(consistent),
        )
        for (name, regime), rows in sorted(consistent.items()):
            edges = ", ".join(
                f"{r.index_symbol}/{r.horizon} {r.edge:+.2f}pp"
                for r in sorted(rows, key=lambda x: (x.index_symbol, x.horizon))
            )
            logger.info("  CONSISTENT  %-28s %-12s %s", name, regime, edges)
        if not consistent:
            logger.info(
                "  None replicates across partitions. With %s comparisons run, isolated "
                "positive cells are the expected appearance of noise -- treat them as such "
                "unless they replicate.", total,
            )
        logger.info(
            "Take at most TWO candidates to the holdout, preferring replication over the "
            "lowest p-value. The holdout is single-use."
        )

    if consistent_negative:
        logger.info("-" * 108)
        logger.info(
            "%s (setup, regime) combination(s) reliably BACKWARDS on BOTH indices -- "
            "as hard to get by chance as a positive replication, and equally informative:",
            len(consistent_negative),
        )
        for (name, regime), rows in sorted(consistent_negative.items()):
            edges = ", ".join(
                f"{r.index_symbol}/{r.horizon} {r.edge:+.2f}pp"
                for r in sorted(rows, key=lambda x: (x.index_symbol, x.horizon))
            )
            logger.info("  CONSISTENT-NEGATIVE  %-28s %-12s %s", name, regime, edges)

    if negatives:
        logger.info("-" * 108)
        logger.info("%s cell(s) reliably BACKWARDS (CI entirely below zero):", len(negatives))
        for r in sorted(negatives, key=lambda x: x.edge)[:15]:
            logger.info(
                "  %-10s %-7s %-28s %-12s n=%6d edge %+6.2fpp  [%+6.2f, %+6.2f]",
                r.index_symbol, r.horizon, r.setup, r.regime, r.n_signals, r.edge, r.ci_low, r.ci_high,
            )
        logger.info(
            "Caution: FAILED_BREAKOUT and EXTENDED_FADE already trade AGAINST recent "
            "direction. A positive result from those is partly a restatement of the "
            "negative drift finding, not independent evidence. Conversely a negative "
            "result from a momentum setup restates the same thing."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
