"""Would ratcheting the stop up once a trade nears its own target have helped?

WHY THIS IS NARROWER THAN A TRAILING STOP
-------------------------------------------
"Add trailing to lock in MFE" was already tried, and it backfired: the 31 Jul
holdout replayed an 8%-activate/5%-width trail against this exact entry
signal and found average wins clipped to ~6% while average losses still ran
the full 9-11% stop -- win/loss ratio 0.53-0.68, net negative even at a
52-59% win rate. That is precisely why Validated Signal and Quick Scalp were
both built with sl_mode=FIXED and no trailing mechanism at all.

Two real trades (1 Sep and 2 Sep 2026, both Validated Signal) showed a
different, narrower shape than "trail arms too early": both ran to within a
few points of their OWN target (MFE 19.02% against a 20% target; MFE 21.06%
against a 28.98% target) and then fully reversed past entry to the stop.
That is not "clip winners early" -- it is "give back a move that was already
almost a full win." This script tests a mechanism scoped to exactly that:
do nothing until a trade gets close to its own target, then ratchet the stop
up to protect some fraction of the gain already made, leaving everything
before that point (including every small-MFE giveback, which this project's
own data shows is most of them) completely untouched.

REAL RECORDED TICKS, NOT THE OPTION-CANDLE ARCHIVE
------------------------------------------------------
Unlike stall_exit_backtest.py/stop_distance_backtest.py, this does not need
data/option_candles/ at all. Every trade this tests was open long enough to
have real StrategyTradeTick rows (30s premium samples recorded by the live
30s monitor, for the trade's own actual lifetime) -- the two trigger trades'
whole round trip (peak near target, then reversal to the stop) already
happened inside that recorded window. Reusing the ticks the app already
collected is exactly what "we have 3 months of MFE data" (AI Origination has
been running since early June) points at -- no archive dependency, no
elasticity model, no DTE-coverage gap.

Trade-off, stated plainly: a tick is a point sample every ~30s, not an OHLC
bar, so this replay can miss a touch that happened and reversed between two
samples -- it is a conservative estimate of hit rates, not exact tick-by-tick
reconstruction. Both the baseline (no lock) and the locked replay are
computed from the SAME tick series, so this blind spot is shared and the
DELTA between them stays a fair comparison even though the absolute
hit-rates should be read as conservative.

WHAT GETS REPLAYED
-------------------
Every closed trade with sl_mode=FIXED matching --origin-like (default
AI_ORIGIN_%, the only population with real statistical power today --
Validated Signal and Quick Scalp are days old, but this script takes an
origin pattern so it can be re-run against them once they have enough
closed history) is replayed twice from its own real entry_price/target/
stoploss: once with no lock (baseline) and once per (threshold,
lock_fraction) combination. `threshold` is the fraction of the entry-to-
target distance the running peak must reach before the lock arms;
`lock_fraction` is how much of the gain-at-activation gets protected (0.0 =
ratchet to breakeven, 0.5 = protect half the move made so far). Only trades
whose peak actually reached a given threshold are counted in that
threshold's comparison -- a trade that never got close to target behaves
identically with or without the lock and would only dilute the result.

Cost is computed from the real entry price, real quantity, and each
replay's own simulated exit price via app/trade_costs.py, same as every
other backtest in this family.

Usage:
    python -m scripts.near_target_lock_backtest --db data/trading.db
    python -m scripts.near_target_lock_backtest --db data/trading.db --origin-like VALIDATED_SIGNAL
    python -m scripts.near_target_lock_backtest --db data/trading.db --thresholds 0.7,0.8,0.9 --lock-fractions 0,0.25,0.5
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
logger = logging.getLogger("near_target_lock_backtest")

BOOTSTRAP_ROUNDS = 10000
# Below this, a cell is reported but flagged as untrustworthy -- same
# convention and value as confidence_sizing_backtest.py/break_confirmation_
# backtest.py for a real (not archive-reconstructed) live-history population.
MIN_BUCKET_LIVE = 20
# A trade with fewer recorded ticks than this has too little of its own
# lifetime observed to replay meaningfully (very short-lived trade, or one
# that predates strategy_trade_ticks) -- excluded and reported, not silently
# dropped.
MIN_TICKS_TO_REPLAY = 3

DEFAULT_THRESHOLDS = (0.70, 0.80, 0.90)
DEFAULT_LOCK_FRACTIONS = (0.0, 0.25, 0.5)
BASELINE_LABEL = "baseline"


@dataclass
class SourceTrade:
    trade_id: str
    index_symbol: str
    option_type: str
    entry_price: float
    target_price: float
    stop_price: float
    quantity: int


@dataclass
class Outcome:
    trade_id: str
    index_symbol: str
    option_type: str
    label: str
    reason: str
    pnl_percent: float
    net_pnl_percent: float
    is_win: bool
    activated: bool


def _load_trades(db_path: str, origin_like: str = "AI_ORIGIN_%") -> list[SourceTrade]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, index_symbol, option_type, entry_price, target, stoploss, quantity
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
            trade_id=row["trade_id"], index_symbol=str(row["index_symbol"]),
            option_type=str(row["option_type"]), entry_price=float(row["entry_price"]),
            target_price=float(row["target"]), stop_price=float(row["stoploss"]),
            quantity=int(row["quantity"]),
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
        # Chunked to stay well under SQLite's default ~999 bound-parameter
        # limit for a large trade population.
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


def _replay_ticks(
    premiums: list[float], entry_price: float, stop_price: float, target_price: float,
    lock: tuple[float, float] | None = None,
) -> tuple[str, float, float, bool]:
    """(reason, exit_price, mfe_price, activated). Walks the trade's own real
    recorded premium ticks in chronological order. lock, when given, is
    (threshold, lock_fraction): once the running peak clears `threshold` of
    the entry-to-target distance, the operative stop ratchets up to
    entry + lock_fraction * (peak_at_activation - entry) -- always above the
    original stop, since peak > entry is required to activate at all."""
    peak = entry_price
    operative_stop = stop_price
    activated = False
    target_distance = target_price - entry_price
    for premium in premiums:
        peak = max(peak, premium)
        if lock is not None and not activated and target_distance > 0:
            threshold, lock_fraction = lock
            if (peak - entry_price) / target_distance >= threshold:
                activated = True
                operative_stop = entry_price + lock_fraction * (peak - entry_price)
        if premium <= operative_stop:
            reason = "LOCK_STOP" if activated else "STOPLOSS"
            return reason, operative_stop, peak, activated
        if premium >= target_price:
            return "TARGET", target_price, peak, activated
    exit_price = premiums[-1] if premiums else entry_price
    return "INCOMPLETE", exit_price, peak, activated


def _run(trade: SourceTrade, premiums: list[float], label: str, lock: tuple[float, float] | None) -> Outcome:
    reason, exit_price, _peak, activated = _replay_ticks(
        premiums, trade.entry_price, trade.stop_price, trade.target_price, lock,
    )
    pnl_percent = (exit_price - trade.entry_price) / trade.entry_price * 100.0
    cost_pct = _cost_pct(trade.entry_price, exit_price, trade.quantity)
    return Outcome(
        trade_id=trade.trade_id, index_symbol=trade.index_symbol, option_type=trade.option_type,
        label=label, reason=reason, pnl_percent=pnl_percent,
        net_pnl_percent=pnl_percent - cost_pct, is_win=pnl_percent > 0, activated=activated,
    )


def _bootstrap_mean(values: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on the mean of `values` (paired per-trade deltas) via resampling."""
    rng = random.Random(20260902)
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
    parser.add_argument(
        "--origin-like", default="AI_ORIGIN_%",
        help="SQL LIKE pattern for origin, e.g. 'AI_ORIGIN_%%' (default) or 'VALIDATED_SIGNAL'.",
    )
    parser.add_argument("--thresholds", default=",".join(str(t) for t in DEFAULT_THRESHOLDS))
    parser.add_argument("--lock-fractions", default=",".join(str(l) for l in DEFAULT_LOCK_FRACTIONS))
    args = parser.parse_args()
    thresholds = tuple(float(t.strip()) for t in args.thresholds.split(",") if t.strip())
    lock_fractions = tuple(float(l.strip()) for l in args.lock_fractions.split(",") if l.strip())

    trades = _load_trades(args.db, args.origin_like)
    logger.info("=" * 100)
    logger.info("Near-target profit-lock backtest: would ratcheting the stop near target have helped?")
    logger.info("=" * 100)
    logger.info("  Closed FIXED-mode trades matching origin LIKE %r: %s", args.origin_like, len(trades))
    if not trades:
        logger.error("No matching closed trades found. Nothing to replay.")
        return 1

    ticks_by_trade = _load_ticks_by_trade(args.db, [t.trade_id for t in trades])

    no_ticks: list[str] = []
    usable_trades: list[SourceTrade] = []
    baseline_by_trade: dict[str, Outcome] = {}
    for trade in trades:
        premiums = ticks_by_trade.get(trade.trade_id, [])
        if len(premiums) < MIN_TICKS_TO_REPLAY:
            no_ticks.append(f"{trade.trade_id[:8]} {trade.index_symbol} {trade.option_type}")
            continue
        usable_trades.append(trade)
        baseline_by_trade[trade.trade_id] = _run(trade, premiums, BASELINE_LABEL, None)

    logger.info("  Usable (>= %s recorded ticks): %s of %s", MIN_TICKS_TO_REPLAY, len(usable_trades), len(trades))
    if no_ticks:
        logger.warning(
            "  %s excluded -- fewer than %s recorded premium ticks (very short-lived trade, or "
            "predates strategy_trade_ticks). Result below describes only the %s usable trades.",
            len(no_ticks), MIN_TICKS_TO_REPLAY, len(usable_trades),
        )
    if not usable_trades:
        logger.error("Nothing usable. Nothing to replay.")
        return 1

    logger.info("=" * 100)
    logger.info(
        "  %-10s %-8s %6s %10s %10s %10s %18s %12s",
        "threshold", "lock", "n", "base_net%", "lock_net%", "delta%", "90% CI on delta", "win% b->l",
    )
    cleared: list[tuple[float, float]] = []
    for threshold in thresholds:
        for lock_fraction in lock_fractions:
            label = f"T{threshold:.2f}_L{lock_fraction:.2f}"
            locked_results = [
                _run(t, ticks_by_trade[t.trade_id], label, (threshold, lock_fraction))
                for t in usable_trades
            ]
            affected = [r for r in locked_results if r.activated]
            n = len(affected)
            threshold_label = f">={threshold:.0%}"
            lock_label = f"{lock_fraction:.0%}"
            if n == 0:
                logger.info(
                    "  %-10s %-8s %6d  (no trade in this population ever reached this threshold)",
                    threshold_label, lock_label, 0,
                )
                continue
            paired = [(baseline_by_trade[r.trade_id], r) for r in affected]
            deltas = [locked.net_pnl_percent - base.net_pnl_percent for base, locked in paired]
            mean_delta = sum(deltas) / n
            base_mean = sum(b.net_pnl_percent for b, _ in paired) / n
            locked_mean = sum(l.net_pnl_percent for _, l in paired) / n
            base_win = sum(1 for b, _ in paired if b.is_win) / n * 100.0
            locked_win = sum(1 for _, l in paired if l.is_win) / n * 100.0
            lo, hi = _bootstrap_mean(deltas)
            thin = n < MIN_BUCKET_LIVE
            flag = "  [THIN]" if thin else ""
            logger.info(
                "  %-10s %-8s %6d %9.2f%% %9.2f%% %9.2f%% [%+.2f%%,%+.2f%%]%s  %.0f%%->%.0f%%",
                threshold_label, lock_label, n, base_mean, locked_mean, mean_delta, lo, hi, flag,
                base_win, locked_win,
            )
            if lo > 0 and not thin:
                cleared.append((threshold, lock_fraction))

    logger.info("=" * 100)
    logger.info("RECOMMENDATION")
    if cleared:
        for threshold, lock_fraction in cleared:
            logger.info(
                "  threshold>=%.0f%%, lock=%.0f%% of gain-at-activation -- 90%% CI on mean delta "
                "excludes zero on the positive side, n>=%s.", threshold * 100, lock_fraction * 100,
                MIN_BUCKET_LIVE,
            )
    else:
        logger.info(
            "  Nothing clears the bar (90%% CI on mean delta must exclude zero, n>=%s). Per this "
            "project's standing discipline, that is the expected, reportable answer -- do not ship "
            "any of these (threshold, lock) combinations from this run alone.", MIN_BUCKET_LIVE,
        )
    logger.info(
        "  Reminder: this replays 30s point samples, not OHLC bars -- both the baseline and locked "
        "replays share that blind spot, so the DELTA is fair even though absolute hit-rates read "
        "conservative. Re-run against --origin-like VALIDATED_SIGNAL/QUICK_SCALP once either has "
        "enough closed history to clear the trust minimum on its own."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
