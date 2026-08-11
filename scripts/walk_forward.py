"""Walk-forward stability: is the edge consistent across time, or one regime?

WHAT THIS ADDS THAT NOTHING ELSE DOES
-------------------------------------
Every other test here pools the whole two years and asks "is the average edge
distinguishable from the base rate." That cannot tell a genuinely stable effect
apart from one concentrated in a single favourable period -- and the pooled
average looks identical either way.

That distinction matters now specifically because the holdout FAILED.
`EMA_STACK@1100_1400` measured +4.16pp on the fit window, cleared Bonferroni at
p = 0.000051, and then came back +0.80pp with a CI straddling zero on the two
untouched months. Two explanations fit:

  1. The fit-window result was noise that a large sample dressed up.
  2. The effect was real in some periods and absent in others, and the holdout
     happened to land in an absent one.

Those imply different things. (1) means stop. (2) means the effect is
regime-dependent and the interesting question becomes *which* regime -- though
it would still not be tradeable without knowing that in advance.

This script splits the history into consecutive windows and reports the edge in
each, so the two are distinguishable.

WHAT IT CANNOT DO
-----------------
Rescue anything. A stable-looking walk-forward sits *against* a failed holdout,
and out-of-sample failure outranks in-sample stability -- the holdout is the
only test whose result was fixed before the data was seen. Treat a positive
result here as "worth a fresh holdout once one exists", never as confirmation.

Usage:
    python -m scripts.walk_forward --db data/trading.db
    python -m scripts.walk_forward --db data/trading.db --setups ST_ALIGNED,EMA_STACK
    python -m scripts.walk_forward --db data/trading.db --windows 8 --regime 1100_1400
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, time

import numpy as np

from scripts.backtest.data import build_arrays, forward_window_bounds, load_bars_sqlite
from scripts.backtest.setups import Setup, assert_causal, build_signals, default_setups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("walk_forward")

TRADING_START = time(9, 45)
TRADING_END = time(15, 15)
HORIZON_BARS = 12          # 60 minutes
MIN_SIGNALS_PER_WINDOW = 50
BOOTSTRAP_ITERATIONS = 1000

REGIME_WINDOWS = {
    "all": (0, 10**6),
    "0945_1100": (0, 105),
    "1100_1400": (105, 285),
    "1400_1515": (285, 10**6),
}

# Futures series duplicate their spot index -- see setup_significance's note on
# why that must never count as a second partition.
FUTURES_SUFFIX = "_FUT"


@dataclass
class WindowResult:
    index_symbol: str
    setup: str
    window: int
    start: date
    end: date
    n: int
    edge: float
    ci_low: float
    ci_high: float

    @property
    def verdict(self) -> str:
        if self.ci_low > 0:
            return "+"
        if self.ci_high < 0:
            return "-"
        return "."


def _eligible(arrays, regime: str) -> np.ndarray:
    hours = arrays.ts.astype("datetime64[m]").astype(object)
    in_session = np.array([TRADING_START <= t.time() <= TRADING_END for t in hours], dtype=bool)
    warm = ~np.isnan(arrays.atr14) & ~np.isnan(arrays.ema21)
    low, high = REGIME_WINDOWS[regime]
    minutes = arrays.minutes_since_open
    return in_session & warm & (minutes >= low) & (minutes < high)


def _edge(wins: float, ups: float, longs: float, n: float) -> float:
    if n == 0:
        return 0.0
    up_rate = ups / n
    base = (longs * up_rate + (n - longs) * (1.0 - up_rate)) / n
    return (wins / n - base) * 100.0


def _evaluate_window(arrays, mask, direction, rng, horizon_bars: int = HORIZON_BARS) -> tuple[int, float, float, float]:
    n_bars = len(arrays)
    close = arrays.close.astype(np.float64)
    positions = np.arange(n_bars)
    bounds = forward_window_bounds(arrays, horizon_bars)
    target = np.minimum(positions + horizon_bars, bounds)

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--interval", default="FIVE_MINUTE")
    parser.add_argument("--windows", type=int, default=6, help="Number of consecutive periods")
    parser.add_argument("--regime", default="1100_1400", choices=sorted(REGIME_WINDOWS))
    parser.add_argument("--setups", default="ST_ALIGNED,EMA_STACK,ORB_BREAK,PDH_PDL_BREAK")
    parser.add_argument(
        "--horizon-bars", type=int, default=HORIZON_BARS,
        help=(
            f"Forward bars per window, at whatever --interval is loaded (default {HORIZON_BARS} "
            "bars = 60 min at the default FIVE_MINUTE interval). Scalping-horizon roadmap item 4: "
            "override for a shorter holding period, e.g. --interval ONE_MINUTE --horizon-bars 5 "
            "for a 5-minute scalp window."
        ),
    )
    parser.add_argument("--seed", type=int, default=20260731)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    wanted = {n.strip().upper() for n in args.setups.split(",") if n.strip()}
    setups = [s for s in default_setups() if s.name.upper() in wanted]
    if not setups:
        logger.error("No declared setup matches --setups %s", args.setups)
        return 1

    connection = sqlite3.connect(args.db)
    try:
        symbols = [
            r[0] for r in connection.execute(
                f"SELECT DISTINCT index_symbol FROM {args.table} WHERE interval = ?", (args.interval,)
            )
        ]
    finally:
        connection.close()
    symbols = [s for s in symbols if not s.upper().endswith(FUTURES_SUFFIX)]

    results: list[WindowResult] = []
    for symbol in sorted(symbols):
        bars = load_bars_sqlite(args.db, args.table, symbol, args.interval)
        if len(bars) < 2000:
            continue
        arrays = build_arrays(symbol, bars)
        eligible = _eligible(arrays, args.regime)

        # Split by SESSION, not by bar, so a window boundary never lands mid-day.
        sessions = np.unique(arrays.session_id)
        chunks = np.array_split(sessions, args.windows)

        logger.info("=" * 100)
        logger.info("%s | regime=%s | %s windows over %s sessions",
                    symbol, args.regime, args.windows, sessions.size)

        for setup in setups:
            signals = build_signals(arrays, setup)
            assert_causal(arrays, setup, signals)
            for w, chunk in enumerate(chunks, start=1):
                in_window = np.isin(arrays.session_id, chunk)
                n, edge, lo, hi = _evaluate_window(arrays, eligible & in_window, signals, rng, args.horizon_bars)
                if n < MIN_SIGNALS_PER_WINDOW:
                    continue
                dates = arrays.ts[in_window].astype("datetime64[D]").astype(object)
                results.append(WindowResult(
                    symbol, setup.label, w, min(dates), max(dates), n, edge, lo, hi,
                ))

    if not results:
        logger.error("No window produced at least %s signals.", MIN_SIGNALS_PER_WINDOW)
        return 1

    by_series: dict[tuple[str, str], list[WindowResult]] = defaultdict(list)
    for r in results:
        by_series[(r.index_symbol, r.setup)].append(r)

    logger.info("=" * 100)
    logger.info("PER-WINDOW EDGE (%s-bar horizon at %s, regime=%s)", args.horizon_bars, args.interval, args.regime)
    logger.info("  '+' CI above zero, '-' CI below, '.' straddles")
    for (symbol, setup), rows in sorted(by_series.items()):
        rows.sort(key=lambda r: r.window)
        logger.info("")
        logger.info("  %s  %s", symbol, setup)
        for r in rows:
            logger.info(
                "    W%-2d %s..%s  n=%5d  edge %+6.2fpp  [%+6.2f, %+6.2f]  %s",
                r.window, r.start, r.end, r.n, r.edge, r.ci_low, r.ci_high, r.verdict,
            )
        edges = [r.edge for r in rows]
        positive = sum(1 for e in edges if e > 0)
        significant = sum(1 for r in rows if r.ci_low > 0)
        logger.info(
            "    -> %s/%s windows positive, %s significant, mean %+.2fpp, spread %.2fpp",
            positive, len(rows), significant, float(np.mean(edges)),
            float(max(edges) - min(edges)),
        )

    # --- the actual question -------------------------------------------------
    logger.info("=" * 100)
    logger.info("STABILITY VERDICT")
    for (symbol, setup), rows in sorted(by_series.items()):
        edges = [r.edge for r in rows]
        positive = sum(1 for e in edges if e > 0)
        significant = sum(1 for r in rows if r.ci_low > 0)
        mean_edge = float(np.mean(edges))
        spread = float(max(edges) - min(edges))

        if positive == len(rows) and significant >= len(rows) // 2:
            verdict = "STABLE -- positive in every window"
        elif positive >= len(rows) * 0.75:
            verdict = "MOSTLY POSITIVE -- sign consistent, magnitude varies"
        elif significant >= 1 and positive <= len(rows) * 0.6:
            verdict = "CONCENTRATED -- driven by a subset of periods"
        else:
            verdict = "UNSTABLE -- no consistent sign"
        logger.info(
            "  %-10s %-28s %s (mean %+.2fpp, spread %.2fpp across %s windows)",
            symbol, setup, verdict, mean_edge, spread, len(rows),
        )

    logger.info("=" * 100)
    logger.info(
        "Read this against the spent holdout, not instead of it. EMA_STACK@1100_1400 "
        "measured +4.16pp in-sample and +0.80pp with a CI straddling zero out-of-sample. "
        "A CONCENTRATED verdict explains that. A STABLE verdict does NOT overturn it -- "
        "the holdout is the only test whose outcome was fixed before the data was seen, "
        "and it still says no."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
