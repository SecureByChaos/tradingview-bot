"""Would a tighter stop-loss (proposal: 5%) help or hurt AI Origination?

WHY 5% IS SUSPECT BEFORE THIS EVEN RUNS
-----------------------------------------
Two pieces of existing evidence already point against it:

  * scripts/stop_survivability.py: at a 10% premium stop, ordinary index noise
    (not a wrong directional call) breaches it in roughly 55-62% of forward
    windows across both indices; at 12%, 46-55%. 5% is materially tighter than
    either.
  * scripts/scalp_stop_sweep.py: stops in the 1-4% range, tested directly
    against real archived premium at scalping horizons, were net-negative
    after costs in every combination -- tight stops catch noise more often
    than genuine adverse moves.

Neither tested exactly 5% at AI Origination's OWN holding style (no fixed
horizon -- runs until stop/target/time-exit, whatever those happen to be for
each real trade). That is what this script is for. Per this project's
standing rule, a plausible-sounding parameter change is tested before it
ships, not after -- "5% is too tight, don't ship it" is as valid and useful
an outcome as finding a level that works.

REAL PREMIUM, NOT THE ELASTICITY MODEL -- SAME REASONING AS STALL_EXIT
------------------------------------------------------------------------
scripts/stall_exit_backtest.py made this choice explicitly: the elasticity
model's own error margin (3-20% Epps attenuation depending on DTE) is
comparable to the effect being measured at a 5-12% stop distance, so a model
cannot be trusted to answer "does this specific distance survive noise
better." This script reuses that file's load_premium_series() (the actual
1-minute high/low/close of the actual contract, from data/option_candles/)
rather than scripts/backtest's index-array + multiplier simulator that
scalp_stop_sweep.py and stop_survivability.py both use. Trades whose contract
is not archived are reported as unreconstructible, never silently dropped.

WHAT GETS REPLAYED
-------------------
Every closed AI Origination trade with sl_mode=FIXED (the trailing-mode
trades have no fixed stop to test against) is replayed from its OWN real
entry_price and entry_time, using its OWN real target (held fixed, per the
task: this tests the stop, not a new target) against each swept stop
distance -- 5/7/8/10/12%, plus the trade's own actual recorded stop as the
"actual" baseline row. Same pessimistic intrabar ordering as
stall_exit_backtest.py: a bar that touches both stop and target scores as a
loss. Trailing and STALL_EXIT are deliberately NOT simulated here -- layering
either on top would conflate the stop-distance question with the
trailing-width question already investigated separately (see CLAUDE.md), and
make this result harder to read.

Cost is computed from the REAL entry price and REAL quantity via
app/trade_costs.py against each replay's own simulated exit price -- no
elasticity coefficient needed, because premium here is never modeled, only
read from the archive.

Usage:
    python -m scripts.stop_distance_backtest --db data/trading.db
    python -m scripts.stop_distance_backtest --db data/trading.db --stops 5,7,8,10,12
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import time

from app.trade_costs import estimate_round_trip_cost
from scripts.stall_exit_backtest import db_timestamp_to_ist, load_premium_series

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stop_distance_backtest")

SQUARE_OFF = time(15, 15)
DEFAULT_STOPS = (5.0, 7.0, 8.0, 10.0, 12.0)
ACTUAL_LABEL = "actual"
# Fraction of the stop distance MFE must clear before a stop-out counts as
# having caught a real adverse move rather than pure noise. Same value and
# meaning as scripts/scalp_stop_sweep.py's NOISE_MFE_FRACTION -- a documented
# judgment call, not a measured threshold, kept consistent across both
# scripts rather than re-derived.
NOISE_MFE_FRACTION = 0.20
# Below this a (option_type, stop, split) cell is reported but not trusted --
# named rather than implied, matching stall_exit_backtest.py's
# MIN_SAMPLE_FOR_SPLIT convention.
MIN_SAMPLE = 10


@dataclass
class SourceTrade:
    trade_id: str
    index_symbol: str
    option_type: str
    entry_price: float
    entry_time: str
    target_price: float
    actual_stoploss_price: float
    quantity: int
    tradingsymbol: str
    symboltoken: str


@dataclass
class ReplayResult:
    trade_id: str
    index_symbol: str
    option_type: str
    entry_day: str
    stop_label: str
    reason: str
    pnl_percent: float
    net_pnl_percent: float
    is_win: bool
    is_noise_hit: bool


def _load_trades(db_path: str) -> list[SourceTrade]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, index_symbol, option_type, entry_price, entry_time,
                   target, stoploss, quantity, tradingsymbol, symboltoken
            FROM strategy_trades
            WHERE origin LIKE 'AI_ORIGIN_%'
              AND exit_price IS NOT NULL
              AND sl_mode = 'FIXED'
              AND entry_price IS NOT NULL AND entry_price > 0
              AND target IS NOT NULL AND stoploss IS NOT NULL
            ORDER BY entry_time
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        SourceTrade(
            trade_id=row["trade_id"], index_symbol=str(row["index_symbol"]),
            option_type=str(row["option_type"]), entry_price=float(row["entry_price"]),
            entry_time=str(row["entry_time"]), target_price=float(row["target"]),
            actual_stoploss_price=float(row["stoploss"]), quantity=int(row["quantity"]),
            tradingsymbol=str(row["tradingsymbol"]), symboltoken=str(row["symboltoken"]),
        )
        for row in rows
    ]


def _cost_pct(entry_price: float, exit_price: float, quantity: int) -> float:
    if entry_price <= 0 or quantity <= 0:
        return 0.0
    breakdown = estimate_round_trip_cost(entry_price, exit_price, quantity)
    return breakdown.total / (entry_price * quantity) * 100.0


def _replay(
    bars: list[tuple], entry_price: float, stop_price: float, target_price: float,
) -> tuple[str, float, float]:
    """(reason, exit_price, mfe_price). Pessimistic intrabar ordering: a bar
    that touches both stop and target in the same minute scores as a loss,
    same convention as stall_exit_backtest.py's _replay_forward, for the same
    reason -- it makes a "this distance is fine" finding harder to reach, not
    easier."""
    mfe_price = entry_price
    for ts, high, low, close in bars:
        if ts.time() >= SQUARE_OFF:
            return "TIME_EXIT", close, mfe_price
        mfe_price = max(mfe_price, high)
        if low <= stop_price:
            return "STOPLOSS", stop_price, mfe_price
        if high >= target_price:
            return "TARGET", target_price, mfe_price
    return "INCOMPLETE", bars[-1][3] if bars else entry_price, mfe_price


def _run_one(trade: SourceTrade, bars: list[tuple], stop_label: str, stop_price: float) -> ReplayResult:
    reason, exit_price, mfe_price = _replay(bars, trade.entry_price, stop_price, trade.target_price)
    pnl_percent = (exit_price - trade.entry_price) / trade.entry_price * 100.0
    cost_pct = _cost_pct(trade.entry_price, exit_price, trade.quantity)
    stop_distance = trade.entry_price - stop_price
    mfe_fraction = (mfe_price - trade.entry_price) / stop_distance if stop_distance > 0 else 0.0
    is_noise_hit = reason == "STOPLOSS" and mfe_fraction < NOISE_MFE_FRACTION
    return ReplayResult(
        trade_id=trade.trade_id, index_symbol=trade.index_symbol, option_type=trade.option_type,
        entry_day=trade.entry_time[:10], stop_label=stop_label, reason=reason,
        pnl_percent=pnl_percent, net_pnl_percent=pnl_percent - cost_pct,
        is_win=pnl_percent > 0, is_noise_hit=is_noise_hit,
    )


@dataclass
class Cell:
    option_type: str
    stop_label: str
    split: str
    n: int
    win_rate: float
    noise_hit_rate: float
    mean_pnl_percent: float
    mean_net_expectancy_percent: float


def _aggregate(results: list[ReplayResult], option_type: str, stop_label: str, split: str) -> Cell | None:
    group = [r for r in results if r.option_type == option_type and r.stop_label == stop_label]
    if not group:
        return None
    n = len(group)
    stop_outs = [r for r in group if r.reason == "STOPLOSS"]
    noise_hit_rate = (sum(1 for r in stop_outs if r.is_noise_hit) / len(stop_outs)) if stop_outs else 0.0
    return Cell(
        option_type=option_type, stop_label=stop_label, split=split, n=n,
        win_rate=sum(1 for r in group if r.is_win) / n * 100.0,
        noise_hit_rate=noise_hit_rate * 100.0,
        mean_pnl_percent=sum(r.pnl_percent for r in group) / n,
        mean_net_expectancy_percent=sum(r.net_pnl_percent for r in group) / n,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--stops", default=",".join(str(s) for s in DEFAULT_STOPS))
    parser.add_argument("--split-fraction", type=float, default=0.7,
                         help="Fraction of trades (chronological) in the fit/in-sample slice.")
    args = parser.parse_args()
    stop_levels = tuple(float(s.strip()) for s in args.stops.split(",") if s.strip())

    trades = _load_trades(args.db)
    logger.info("=" * 100)
    logger.info("Stop-distance backtest: would a tighter stop have helped AI Origination?")
    logger.info("=" * 100)
    logger.info("  Closed AI Origination FIXED-mode trades: %s", len(trades))
    if not trades:
        logger.error("No closed AI Origination FIXED-mode trades found. Nothing to replay.")
        return 1

    reconstructed = 0
    no_archive: list[str] = []
    no_bars_from_entry: list[str] = []
    all_results: list[ReplayResult] = []
    for trade in trades:
        series = load_premium_series(trade.tradingsymbol, trade.symboltoken)
        if not series:
            no_archive.append(f"{trade.trade_id[:8]} {trade.index_symbol} {trade.tradingsymbol}")
            continue
        # entry_time as stored is UTC with no offset marker (see
        # db_timestamp_to_ist's docstring) -- must be shifted to IST before
        # comparing against the archive's own naive-IST timestamps, same as
        # stall_exit_backtest.py does for exit_time. Same-day match only: an
        # entry near midnight has no business picking up the next day's bars.
        entry_ist = db_timestamp_to_ist(trade.entry_time)
        bars = [b for b in series if b[0] >= entry_ist and b[0].date() == entry_ist.date()]
        if not bars:
            no_bars_from_entry.append(f"{trade.trade_id[:8]} {trade.index_symbol} {trade.tradingsymbol}")
            continue
        reconstructed += 1

        for stop_pct in stop_levels:
            stop_price = round(trade.entry_price * (1 - stop_pct / 100.0), 2)
            label = f"{stop_pct:.0f}%"
            all_results.append(_run_one(trade, bars, label, stop_price))
        all_results.append(_run_one(trade, bars, ACTUAL_LABEL, trade.actual_stoploss_price))

    logger.info("  Reconstructed from real option candles: %s of %s", reconstructed, len(trades))
    if no_archive:
        logger.warning(
            "  %s not reconstructible -- contract not in data/option_candles/. Result below "
            "describes only the %s that could be reconstructed.", len(no_archive), reconstructed,
        )
    if no_bars_from_entry:
        logger.warning(
            "  %s had an archived contract but no bars on/after the real entry time (same "
            "calendar day) -- excluded rather than replayed from the wrong starting point.",
            len(no_bars_from_entry),
        )
    if not all_results:
        logger.error(
            "Nothing could be reconstructed. Archive the contracts AI Origination is currently "
            "trading (scripts/pull_option_candles.py) and re-run."
        )
        return 1

    cutoff_idx = max(int(reconstructed * args.split_fraction), 1)
    cutoff_day = sorted({r.entry_day for r in all_results})[min(cutoff_idx, len(set(r.entry_day for r in all_results)) - 1)]
    in_sample = [r for r in all_results if r.entry_day < cutoff_day]
    out_of_sample = [r for r in all_results if r.entry_day >= cutoff_day]
    if not in_sample or not out_of_sample:
        # Too few distinct days to split meaningfully -- report everything as
        # a single "all" slice rather than manufacturing an empty one.
        splits = (("all", all_results),)
    else:
        splits = (("in_sample", in_sample), ("out_of_sample", out_of_sample))

    option_types = sorted({r.option_type for r in all_results})
    labels = [f"{s:.0f}%" for s in stop_levels] + [ACTUAL_LABEL]

    logger.info("=" * 100)
    logger.info(
        "  %-6s %-8s %-12s %6s %8s %10s %10s %12s",
        "type", "stop", "split", "n", "win%", "noise-hit%", "mean_pnl%", "net_exp%",
    )
    cells: list[Cell] = []
    for option_type in option_types:
        for label in labels:
            for split_name, split_results in splits:
                cell = _aggregate(split_results, option_type, label, split_name)
                if cell is None:
                    continue
                cells.append(cell)
                flag = "" if cell.n >= MIN_SAMPLE else "  [THIN]"
                logger.info(
                    "  %-6s %-8s %-12s %6d %7.1f%% %9.1f%% %9.2f%% %11.2f%%%s",
                    option_type, label, split_name, cell.n, cell.win_rate,
                    cell.noise_hit_rate, cell.mean_pnl_percent, cell.mean_net_expectancy_percent, flag,
                )

    logger.info("=" * 100)
    logger.info("RECOMMENDATION")
    for option_type in option_types:
        actual_cells = [c for c in cells if c.option_type == option_type and c.stop_label == ACTUAL_LABEL]
        actual_baseline = (
            sum(c.mean_net_expectancy_percent * c.n for c in actual_cells) / sum(c.n for c in actual_cells)
            if actual_cells else None
        )
        logger.info(
            "  %s -- current (actual) stop baseline net expectancy: %s",
            option_type, f"{actual_baseline:+.2f}%" if actual_baseline is not None else "n/a",
        )
        for stop_pct in stop_levels:
            label = f"{stop_pct:.0f}%"
            label_cells = [c for c in cells if c.option_type == option_type and c.stop_label == label]
            if not label_cells:
                continue
            consistent_positive = all(c.mean_net_expectancy_percent > 0 for c in label_cells)
            consistent_below_noise_bar = all(c.noise_hit_rate < 50.0 for c in label_cells)
            thin = any(c.n < MIN_SAMPLE for c in label_cells)
            verdict = (
                "CLEARS both bars" if (consistent_positive and consistent_below_noise_bar and not thin)
                else ("thin sample -- not a basis for a decision" if thin
                      else "does not clear both bars")
            )
            logger.info("    stop=%-5s -> %s", label, verdict)
    logger.info(
        "  A stop only 'clears both bars' if EVERY reported split for that option type shows "
        "net expectancy > 0 AND noise-hit rate < 50%%, with no split below the %s-trade minimum. "
        "Per the task spec: if nothing clears, that is the expected, reportable answer -- 5%% "
        "matching the prior evidence is not a failure of this backtest.",
        MIN_SAMPLE,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
