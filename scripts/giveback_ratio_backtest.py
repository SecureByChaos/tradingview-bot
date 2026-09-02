"""Would a proportional (ratio-of-peak-gain) trailing stop help, independent of target distance?

WHY THIS IS A DIFFERENT MECHANISM FROM near_target_lock_backtest.py
------------------------------------------------------------------------
near_target_lock_backtest.py only protects a trade once its peak clears a
fraction of the *entry-to-target distance* -- real, but narrow: it found
support only at >=90% of target (n=26 of 189, ~14% of the population), and
does nothing for a trade that had a real, meaningful move but never got
close to its own (possibly very wide) target -- e.g. Autonomous AI's
39-55% nominal backstop targets, which a real trade rarely approaches, or
any trade whose target the AI simply set wide. The real 60-day review (2
Sep 2026) found 84% of ALL losses across every strategy had a positive MFE
first -- a population far bigger than the ~14% the near-target lock can
ever touch.

This script tests a mechanism scoped to the size of the MOVE ITSELF, not
distance to target: once a trade's running peak clears an absolute MFE
floor (swept 5/8/12%, well above what stop_survivability.py already found
is ordinary noise at a 10-12% stop distance), the stop trails continuously
at `peak - giveback_ratio * (peak - entry)` -- i.e. never let more than
`giveback_ratio` (swept 30/50/70%) of the CURRENT peak gain be given back,
recomputed as new peaks form. Works on any trade regardless of how far its
target sits.

WHY THIS IS NOT JUST THE ALREADY-FALSIFIED TRAIL AGAIN
-----------------------------------------------------------
The 31 Jul holdout's trail was 8%-activate / 5%-WIDTH -- a FIXED number of
percentage points of room, regardless of how far past 8% price had run. At
8.1% MFE that leaves only ~3.1% of room before triggering (clips almost
immediately); at 30% MFE it leaves the same fixed 5 points, a much smaller
share of the move. That mismatch -- constant room on a growing move -- is
exactly why winners were clipped to ~6% while losers still ran the full
stop. This script's trail width is a RATIO of the peak gain instead of a
fixed point count, so room grows proportionally with the size of the move.
Whether that actually avoids the early-clip failure right at the
activation floor -- rather than just moving where it happens -- is what
PART 2 measures, not assumed going in.

PART 1 is purely descriptive: buckets EVERY closed trade (wins included)
by its own real MFE (from strategy_trade_ticks, same reason
near_target_lock_backtest.py uses ticks over highest_price/lowest_price --
see that script's own docstring) and reports win rate, mean final P&L, and
-- for the losses in each band -- mean giveback ratio. This is what should
inform whether a candidate floor is even in the right neighbourhood before
PART 2 tests it, rather than picking floor/ratio values with no picture of
the real distribution behind them.

Usage:
    python -m scripts.giveback_ratio_backtest --db data/trading.db
    python -m scripts.giveback_ratio_backtest --db data/trading.db --origin-like VALIDATED_SIGNAL
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

from app.trade_costs import estimate_round_trip_cost

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("giveback_ratio_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20
MIN_TICKS_TO_REPLAY = 3

DEFAULT_FLOORS = (5.0, 8.0, 12.0)
DEFAULT_GIVEBACK_RATIOS = (0.3, 0.5, 0.7)

MFE_BANDS = ((0.0, 2.0, "0-2%"), (2.0, 5.0, "2-5%"), (5.0, 10.0, "5-10%"),
             (10.0, 20.0, "10-20%"), (20.0, float("inf"), "20%+"))


@dataclass
class SourceTrade:
    trade_id: str
    entry_price: float
    target_price: float
    stop_price: float
    quantity: int
    result: str
    pnl_percent: float


def _load_trades(db_path: str, origin_like: str = "AI_ORIGIN_%") -> list[SourceTrade]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, entry_price, target, stoploss, quantity, result, pnl_percent
            FROM strategy_trades
            WHERE origin LIKE ?
              AND exit_price IS NOT NULL
              AND sl_mode = 'FIXED'
              AND entry_price IS NOT NULL AND entry_price > 0
              AND target IS NOT NULL AND stoploss IS NOT NULL
              AND target > entry_price
            ORDER BY entry_time
            """,
            (origin_like,),
        ).fetchall()
    finally:
        connection.close()
    return [
        SourceTrade(
            trade_id=row["trade_id"], entry_price=float(row["entry_price"]),
            target_price=float(row["target"]), stop_price=float(row["stoploss"]),
            quantity=int(row["quantity"]), result=str(row["result"]),
            pnl_percent=float(row["pnl_percent"] or 0.0),
        )
        for row in rows
    ]


def _load_ticks_by_trade(db_path: str, trade_ids: list[str]) -> dict[str, list[float]]:
    if not trade_ids:
        return {}
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    ticks: dict[str, list[float]] = {}
    try:
        chunk_size = 500
        for i in range(0, len(trade_ids), chunk_size):
            chunk = trade_ids[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT trade_id, premium FROM strategy_trade_ticks "
                f"WHERE trade_id IN ({placeholders}) ORDER BY trade_id, recorded_at ASC",
                chunk,
            ).fetchall()
            for row in rows:
                ticks.setdefault(str(row["trade_id"]), []).append(float(row["premium"]))
    finally:
        connection.close()
    return ticks


def _cost_pct(entry_price: float, exit_price: float, quantity: int) -> float:
    if entry_price <= 0 or quantity <= 0:
        return 0.0
    breakdown = estimate_round_trip_cost(entry_price, exit_price, quantity)
    return breakdown.total / (entry_price * quantity) * 100.0


def _mfe_percent(premiums: list[float], entry_price: float) -> float:
    if not premiums or entry_price <= 0:
        return 0.0
    return max(0.0, (max(premiums) - entry_price) / entry_price * 100.0)


def _mfe_band(mfe_percent: float) -> str:
    for lo, hi, label in MFE_BANDS:
        if lo <= mfe_percent < hi:
            return label
    return MFE_BANDS[-1][2]


def _replay_giveback(
    premiums: list[float], entry_price: float, stop_price: float, target_price: float,
    floor_pct: float, giveback_ratio: float,
) -> tuple[str, float, float, bool]:
    """(reason, exit_price, mfe_price, armed). Continuously-updating trail:
    once the running peak clears `floor_pct` of entry, the operative stop
    ratchets to peak - giveback_ratio * (peak - entry) on every subsequent
    tick as new peaks form (never below the original stop). This scales the
    protected room with the size of the move, unlike a fixed-point trail."""
    peak = entry_price
    armed = False
    for premium in premiums:
        peak = max(peak, premium)
        if not armed and entry_price > 0 and (peak - entry_price) / entry_price * 100.0 >= floor_pct:
            armed = True
        operative_stop = stop_price
        if armed:
            trail_stop = peak - giveback_ratio * (peak - entry_price)
            operative_stop = max(stop_price, trail_stop)
        if premium <= operative_stop:
            reason = "GIVEBACK_STOP" if armed else "STOPLOSS"
            return reason, operative_stop, peak, armed
        if premium >= target_price:
            return "TARGET", target_price, peak, armed
    exit_price = premiums[-1] if premiums else entry_price
    return "INCOMPLETE", exit_price, peak, armed


@dataclass
class Outcome:
    trade_id: str
    net_pnl_percent: float
    is_win: bool
    armed: bool


def _run(trade: SourceTrade, premiums: list[float], floor_pct: float, giveback_ratio: float) -> Outcome:
    reason, exit_price, _peak, armed = _replay_giveback(
        premiums, trade.entry_price, trade.stop_price, trade.target_price, floor_pct, giveback_ratio,
    )
    pnl_percent = (exit_price - trade.entry_price) / trade.entry_price * 100.0
    cost_pct = _cost_pct(trade.entry_price, exit_price, trade.quantity)
    return Outcome(
        trade_id=trade.trade_id, net_pnl_percent=pnl_percent - cost_pct,
        is_win=pnl_percent > 0, armed=armed,
    )


def _baseline_net_pnl_percent(trade: SourceTrade, premiums: list[float]) -> float:
    """No-mechanism baseline: replay with an unreachable floor so the trail
    never arms -- falls straight through to the trade's own real stop/target,
    same shape near_target_lock_backtest.py's baseline uses."""
    outcome = _run(trade, premiums, floor_pct=10_000.0, giveback_ratio=0.0)
    return outcome.net_pnl_percent


def _bootstrap_mean(values: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    rng = random.Random(20260903)
    means = []
    for _ in range(rounds):
        sample = [rng.choice(values) for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.05 * rounds)]
    hi = means[int(0.95 * rounds) - 1]
    return lo, hi


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--origin-like", default="AI_ORIGIN_%")
    parser.add_argument("--floors", default=",".join(str(f) for f in DEFAULT_FLOORS))
    parser.add_argument("--giveback-ratios", default=",".join(str(r) for r in DEFAULT_GIVEBACK_RATIOS))
    args = parser.parse_args()
    floors = tuple(float(f.strip()) for f in args.floors.split(",") if f.strip())
    ratios = tuple(float(r.strip()) for r in args.giveback_ratios.split(",") if r.strip())

    trades = _load_trades(args.db, args.origin_like)
    logger.info("=" * 100)
    logger.info("Giveback-ratio backtest: does a proportional trail, independent of target, help?")
    logger.info("=" * 100)
    logger.info("  Closed FIXED-mode trades matching origin LIKE %r: %s", args.origin_like, len(trades))
    if not trades:
        logger.error("No matching closed trades found. Nothing to replay.")
        return 1

    ticks_by_trade = _load_ticks_by_trade(args.db, [t.trade_id for t in trades])
    usable = [t for t in trades if len(ticks_by_trade.get(t.trade_id, [])) >= MIN_TICKS_TO_REPLAY]
    logger.info("  Usable (>= %s recorded ticks): %s of %s", MIN_TICKS_TO_REPLAY, len(usable), len(trades))
    if not usable:
        logger.error("Nothing usable. Nothing to replay.")
        return 1

    logger.info("=" * 100)
    logger.info("PART 1 -- descriptive: real MFE distribution across every closed trade (wins + losses)")
    logger.info("  %-8s %6s %7s %10s %16s", "MFE band", "n", "win%", "mean_pnl%", "loss giveback%")
    for _lo, _hi, label in MFE_BANDS:
        band_trades = [t for t in usable if _mfe_band(_mfe_percent(ticks_by_trade[t.trade_id], t.entry_price)) == label]
        if not band_trades:
            logger.info("  %-8s %6d  (no trades in this band)", label, 0)
            continue
        n = len(band_trades)
        win_rate = sum(1 for t in band_trades if t.result == "WIN") / n * 100.0
        mean_pnl = sum(t.pnl_percent for t in band_trades) / n
        loss_trades = [t for t in band_trades if t.result == "LOSS"]
        giveback_ratios_actual = []
        for t in loss_trades:
            mfe = _mfe_percent(ticks_by_trade[t.trade_id], t.entry_price)
            if mfe > 0:
                giveback_ratios_actual.append((mfe - t.pnl_percent) / mfe * 100.0)
        mean_giveback = sum(giveback_ratios_actual) / len(giveback_ratios_actual) if giveback_ratios_actual else 0.0
        logger.info("  %-8s %6d %6.1f%% %9.2f%% %15.1f%%", label, n, win_rate, mean_pnl, mean_giveback)

    logger.info("=" * 100)
    logger.info("PART 2 -- candidate: proportional giveback stop, activation floor x giveback ratio swept")
    logger.info(
        "  %-8s %-8s %6s %10s %10s %10s %18s %12s",
        "floor", "ratio", "n", "base_net%", "trail_net%", "delta%", "90% CI on delta", "win% b->t",
    )
    cleared: list[tuple[float, float]] = []
    for floor_pct in floors:
        for giveback_ratio in ratios:
            armed_trades = []
            deltas = []
            base_vals = []
            trail_vals = []
            base_wins = 0
            trail_wins = 0
            for t in usable:
                premiums = ticks_by_trade[t.trade_id]
                trailed = _run(t, premiums, floor_pct, giveback_ratio)
                if not trailed.armed:
                    continue
                baseline_net = _baseline_net_pnl_percent(t, premiums)
                armed_trades.append(t)
                deltas.append(trailed.net_pnl_percent - baseline_net)
                base_vals.append(baseline_net)
                trail_vals.append(trailed.net_pnl_percent)
                base_wins += 1 if baseline_net > 0 else 0
                trail_wins += 1 if trailed.is_win else 0

            n = len(armed_trades)
            if n == 0:
                logger.info("  %-8s %-8s %6d  (no trade ever armed this floor)", f"{floor_pct:.0f}%", f"{giveback_ratio:.0%}", 0)
                continue
            mean_delta = sum(deltas) / n
            base_mean = sum(base_vals) / n
            trail_mean = sum(trail_vals) / n
            lo, hi = _bootstrap_mean(deltas)
            thin = n < MIN_BUCKET_LIVE
            flag = "  [THIN]" if thin else ""
            logger.info(
                "  %-8s %-8s %6d %9.2f%% %9.2f%% %9.2f%% [%+.2f%%,%+.2f%%]%s  %.0f%%->%.0f%%",
                f"{floor_pct:.0f}%", f"{giveback_ratio:.0%}", n, base_mean, trail_mean, mean_delta,
                lo, hi, flag, base_wins / n * 100.0, trail_wins / n * 100.0,
            )
            if lo > 0 and not thin:
                cleared.append((floor_pct, giveback_ratio))

    logger.info("=" * 100)
    logger.info("RECOMMENDATION")
    if cleared:
        for floor_pct, giveback_ratio in cleared:
            logger.info(
                "  floor=%.0f%%, giveback_ratio=%.0f%% -- 90%% CI on mean delta excludes zero "
                "on the positive side, n>=%s.", floor_pct, giveback_ratio * 100, MIN_BUCKET_LIVE,
            )
    else:
        logger.info(
            "  Nothing clears the bar (90%% CI on mean delta must exclude zero, n>=%s). Per this "
            "project's standing discipline, that is the expected, reportable answer -- do not ship "
            "any of these (floor, ratio) combinations from this run alone.", MIN_BUCKET_LIVE,
        )
    logger.info(
        "  Read PART 1 alongside PART 2: a floor sitting in a band where losses already give back "
        "most of their MFE (high loss-giveback%% in PART 1) is the one worth trusting if PART 2 also "
        "clears the bar there -- the two sections should agree, not just PART 2 in isolation."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
