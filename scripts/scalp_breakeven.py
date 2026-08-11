"""Scalping-horizon cost breakeven -- roadmap item 3, report this FIRST.

WHAT THIS ANSWERS, BEFORE ANY DIRECTIONAL SIGNAL IS TESTED
-------------------------------------------------------------
At a 3/5/10/15-minute holding period, what forward premium move is needed
just to break even after round-trip costs? And how often does the archived
data actually show a move that large?

A real 55% directional accuracy is worthless if the typical premium move at
that holding period doesn't clear costs -- so this is computed and reported
BEFORE scripts/scalp_stop_sweep.py's predictive tests, not alongside them.

METHOD
------
Round-trip cost %% comes from the real cost model (app/trade_costs.py),
applied to the archive's own median premium -- not the flat ~0.56-0.6%%
figure quoted elsewhere (that number should fall out of this calculation
for a typical ATM contract, not be assumed into it).

The forward-move distribution comes from NON-OVERLAPPING windows in the
real archived option-candle data (data/option_candles/, built by
scripts/pull_option_candles.py) -- entry points are spaced at least one full
holding period apart specifically so 30,000 overlapping 3-minute windows
are not counted as 30,000 independent observations (see CLAUDE.md's walk-
forward work for the same discipline applied elsewhere). Each forward point
is matched to the first archived tick at or after entry_time + holding
period, within a tolerance window -- illiquid contracts do not print every
minute, so assuming exact 1-minute spacing would silently drop or
misattribute data.

REQUIRES DATA NOT PRESENT IN THIS DEVELOPMENT ENVIRONMENT: the real
1-minute option-candle archive. Not present in this sandbox -- built and
unit-tested against a synthetic archive (tests/test_scalp_breakeven.py); run
this against the real archive on the machine that has it.

Usage:
    python -m scripts.scalp_breakeven
    python -m scripts.scalp_breakeven --candles data/option_candles --holding-minutes 3,5,10,15
"""

from __future__ import annotations

import argparse
import logging
import sys
from bisect import bisect_left
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np

from app.trade_costs import estimate_round_trip_cost
from scripts.backtest.premium import OPTION_CANDLE_DIR, load_option_series, parse_symbol_filename

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scalp_breakeven")

DEFAULT_HOLDING_MINUTES = (3, 5, 10, 15)
# How close an archived tick must be to entry_time + holding_minutes to count
# as "the" forward observation, given contracts do not print every minute.
MATCH_TOLERANCE = timedelta(seconds=45)
DEFAULT_LOT_SIZE = 35


@dataclass
class BreakevenResult:
    index_symbol: str
    holding_minutes: int
    n_windows: int
    n_contracts: int
    breakeven_move_pct: float
    median_abs_move_pct: float
    mean_abs_move_pct: float
    fraction_clearing_breakeven: float


def _forward_moves(series: list[tuple], holding_minutes: int) -> list[float]:
    """Non-overlapping |% change| samples at the given holding period from
    one contract's own price series. Entry points step forward by a full
    holding period after each accepted sample (or after each skipped point,
    to avoid getting stuck on a single unmatched gap), which is what makes
    the samples non-overlapping rather than a rolling window."""
    if len(series) < 2:
        return []
    series = sorted(series, key=lambda row: row[0])
    times = [row[0] for row in series]
    prices = [row[1] for row in series]
    horizon = timedelta(minutes=holding_minutes)

    moves: list[float] = []
    i = 0
    n = len(series)
    while i < n:
        target_time = times[i] + horizon
        j = bisect_left(times, target_time, lo=i + 1)
        if j < n and abs(times[j] - target_time) <= MATCH_TOLERANCE and prices[i] > 0:
            moves.append(abs(prices[j] - prices[i]) / prices[i] * 100.0)
            i = j + 1
        else:
            i += 1
    return moves


def _cost_pct(avg_premium: float, lot_size: int) -> float:
    if avg_premium <= 0 or lot_size <= 0:
        return 0.0
    breakdown = estimate_round_trip_cost(avg_premium, avg_premium, lot_size)
    return breakdown.total / (avg_premium * lot_size) * 100.0


def compute_breakeven(
    candle_dir: Path, holding_minutes_list: list[int], lot_size: int = DEFAULT_LOT_SIZE,
) -> list[BreakevenResult]:
    if not candle_dir.exists():
        raise SystemExit(
            f"Option candle archive not found at {candle_dir}. Run scripts/pull_option_candles.py "
            "while the contracts are still live, then re-run this against that archive."
        )

    by_index: dict[str, list[list[tuple]]] = {}
    for path in sorted(candle_dir.glob("*.csv")):
        meta = parse_symbol_filename(path.stem)
        if not meta:
            continue
        series = load_option_series(path)
        if not series:
            continue
        by_index.setdefault(meta["name"].upper(), []).append(series)

    results: list[BreakevenResult] = []
    for index_symbol, contract_series in sorted(by_index.items()):
        all_prices = [price for series in contract_series for _, price in series]
        avg_premium = float(np.median(all_prices)) if all_prices else 0.0
        cost_pct = _cost_pct(avg_premium, lot_size)

        for holding_minutes in holding_minutes_list:
            moves: list[float] = []
            for series in contract_series:
                moves.extend(_forward_moves(series, holding_minutes))
            if not moves:
                results.append(BreakevenResult(
                    index_symbol, holding_minutes, 0, len(contract_series), cost_pct, 0.0, 0.0, 0.0,
                ))
                continue
            moves_arr = np.array(moves, dtype=np.float64)
            clears = float((moves_arr >= cost_pct).mean())
            results.append(BreakevenResult(
                index_symbol, holding_minutes, len(moves), len(contract_series), cost_pct,
                float(np.median(moves_arr)), float(moves_arr.mean()), clears,
            ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candles", default=str(OPTION_CANDLE_DIR))
    parser.add_argument("--holding-minutes", default=",".join(str(m) for m in DEFAULT_HOLDING_MINUTES))
    parser.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE)
    args = parser.parse_args()

    holding_minutes_list = [int(m.strip()) for m in args.holding_minutes.split(",") if m.strip()]
    results = compute_breakeven(Path(args.candles), holding_minutes_list, args.lot_size)
    if not results:
        logger.error("No archived contracts found under %s -- nothing to report.", args.candles)
        return 1

    logger.info("=" * 100)
    logger.info(
        "%-10s %5s %8s %6s %14s %14s %14s %10s",
        "index", "hold", "windows", "n_ctr", "breakeven%", "median|move|%", "mean|move|%", "clears%",
    )
    for r in results:
        logger.info(
            "%-10s %5s %8s %6s %14.3f %14.3f %14.3f %10.1f",
            r.index_symbol, r.holding_minutes, r.n_windows, r.n_contracts,
            r.breakeven_move_pct, r.median_abs_move_pct, r.mean_abs_move_pct,
            r.fraction_clearing_breakeven * 100,
        )
    logger.info("=" * 100)
    logger.info(
        "'clears%%' is the fraction of non-overlapping windows whose ABSOLUTE move alone would "
        "clear round-trip cost -- an upper bound on what any directional signal could achieve, "
        "since it does not require the direction to be called correctly. If this is low, no "
        "signal at that holding period can be profitable regardless of directional accuracy."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
