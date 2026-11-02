"""Does a closed trade's own win/loss predict the NEXT same-direction trade's outcome?

BACKGROUND
----------
Prompted by a real observation (2 Nov 2026): watching Agentic AI's trade history, a
BUY_CE loss was immediately followed by another BUY_CE that won, five minutes apart but
on the other index -- a "it lost, then corrected" shape that's easy to read as a real
signal. Checked the whole day's sequence by hand first: of the day's 4 CE transitions and
3 PE transitions, only about half were loss-then-win; the rest were loss-then-loss or
win-then-loss. One day, eyeballed, is not evidence either way -- this script is the real
check, against the full closed-trade history, not one day's anecdote.

QUESTION
--------
Within one origin, walk each DIRECTION's (BUY_CE, BUY_PE) own closed trades in
chronological order (entry_time) and classify each trade (other than the first in its
direction) by whether the trade immediately BEFORE it, in the same direction, was a WIN
or a LOSS -- regardless of what happened on the other direction or the other index in
between, matching how the pattern was actually noticed. Report win rate and mean P&L for
"following a loss" vs "following a win", plus a bootstrap 90% CI on the difference, same
shape as every other sequential/conditional backtest in this project
(same_direction_entries_backtest.py, confidence_sizing_backtest.py).

A trade immediately preceded by a BREAKEVEN result is excluded from the loss/win
comparison (neither label fits) and reported separately as its own, uncompared count.

NOT split by index. Agentic AI trades under a single cross-index position rule
(max_concurrent_positions=1), so "the next BUY_CE" can legitimately land on either index
-- splitting by index too would shred an already-thin population into buckets too small
to mean anything. If this ever needs re-checking per index, that's a deliberate follow-up,
not done here.

Population: any origin, exact match (default AGENTIC_AI) -- --origin can point this at
any single-value origin (QUICK_SCALP, AUTONOMOUS_AI, SIGNAL, ...), not a LIKE pattern,
since every origin this applies to today is one fixed string.

Usage:
    python -m scripts.sequential_outcome_backtest --db data/trading.db
    python -m scripts.sequential_outcome_backtest --db data/trading.db --origin AUTONOMOUS_AI
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sequential_outcome_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same live-history trust minimum used throughout scripts/*.py

DIRECTIONS = ("BUY_CE", "BUY_PE")


@dataclass
class ClosedTrade:
    trade_id: str
    signal: str
    entry_time: str  # ISO string, sortable as-is (same format for every row)
    pnl_percent: float
    result: str


@dataclass
class Sample:
    pnl_percent: float
    is_win: bool


def _load_trades(db_path: str, origin: str) -> list[ClosedTrade]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, signal, entry_time, pnl_percent, result
            FROM strategy_trades
            WHERE origin = ?
              AND status = 'CLOSED'
              AND pnl_percent IS NOT NULL
              AND signal IN ('BUY_CE', 'BUY_PE')
            ORDER BY entry_time ASC
            """,
            (origin,),
        ).fetchall()
    finally:
        connection.close()
    return [
        ClosedTrade(
            trade_id=str(row["trade_id"]), signal=str(row["signal"]),
            entry_time=str(row["entry_time"]), pnl_percent=float(row["pnl_percent"]),
            result=str(row["result"]),
        )
        for row in rows
    ]


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20261102)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def classify_by_preceding_result(trades: list[ClosedTrade]) -> dict[str, list[Sample]]:
    """trades must already be in chronological order for ONE direction. Returns
    {"following_loss": [...], "following_win": [...], "following_breakeven": [...]} --
    the first trade in the list has no predecessor and is excluded entirely."""
    buckets: dict[str, list[Sample]] = {
        "following_loss": [], "following_win": [], "following_breakeven": [],
    }
    for previous, current in zip(trades, trades[1:]):
        sample = Sample(pnl_percent=current.pnl_percent, is_win=(current.result == "WIN"))
        if previous.result == "LOSS":
            buckets["following_loss"].append(sample)
        elif previous.result == "WIN":
            buckets["following_win"].append(sample)
        else:
            buckets["following_breakeven"].append(sample)
    return buckets


def _report_bucket(label: str, samples: list[Sample]) -> None:
    if not samples:
        logger.info("  %-20s n=0", label)
        return
    n = len(samples)
    wins = sum(1 for s in samples if s.is_win)
    mean_pnl = sum(s.pnl_percent for s in samples) / n
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-20s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%%s",
        label, n, wins / n * 100.0, mean_pnl, flag,
    )


def _bootstrap_compare(a: list[Sample], b: list[Sample]) -> None:
    if len(a) < 2 or len(b) < 2:
        logger.info(
            "  bootstrap comparison: too few observations (n=%d vs n=%d) to compute a CI.",
            len(a), len(b),
        )
        return
    lo, hi = _bootstrap_mean_diff([s.pnl_percent for s in a], [s.pnl_percent for s in b])
    verdict = (
        "following-a-loss reliably BETTER" if lo > 0
        else "following-a-loss reliably WORSE" if hi < 0
        else "no reliable difference at this sample size"
    )
    logger.info(
        "  bootstrap 90%% CI on mean_pnl diff (following-loss - following-win) = [%+.2f, %+.2f] -> %s",
        lo, hi, verdict,
    )


def run_direction(direction: str, trades: list[ClosedTrade]) -> None:
    logger.info("-" * 100)
    logger.info("%s (n=%d closed trades)", direction, len(trades))
    if len(trades) < 2:
        logger.info("  Fewer than 2 closed trades -- no predecessor exists for any of them, nothing to compare.")
        return
    buckets = classify_by_preceding_result(trades)
    _report_bucket("following a LOSS", buckets["following_loss"])
    _report_bucket("following a WIN", buckets["following_win"])
    if buckets["following_breakeven"]:
        _report_bucket("following BREAKEVEN", buckets["following_breakeven"])
        logger.info("    (excluded from the comparison below -- neither label fits a breakeven predecessor)")
    _bootstrap_compare(buckets["following_loss"], buckets["following_win"])


def run_backtest(trades: list[ClosedTrade]) -> None:
    logger.info("=" * 100)
    logger.info("DOES THE PRECEDING SAME-DIRECTION TRADE'S RESULT PREDICT THE NEXT ONE'S OUTCOME?")
    logger.info("=" * 100)
    for direction in DIRECTIONS:
        run_direction(direction, [t for t in trades if t.signal == direction])

    logger.info("-" * 100)
    logger.info("POOLED ACROSS BOTH DIRECTIONS (each direction's own sequence, concatenated)")
    pooled_following_loss: list[Sample] = []
    pooled_following_win: list[Sample] = []
    for direction in DIRECTIONS:
        buckets = classify_by_preceding_result([t for t in trades if t.signal == direction])
        pooled_following_loss.extend(buckets["following_loss"])
        pooled_following_win.extend(buckets["following_win"])
    _report_bucket("following a LOSS", pooled_following_loss)
    _report_bucket("following a WIN", pooled_following_win)
    _bootstrap_compare(pooled_following_loss, pooled_following_win)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument(
        "--origin", default="AGENTIC_AI",
        help="Exact origin to analyze (not a LIKE pattern -- e.g. AGENTIC_AI, QUICK_SCALP, AUTONOMOUS_AI).",
    )
    args = parser.parse_args()

    trades = _load_trades(args.db, args.origin)
    logger.info("Loaded %d closed %s trade(s) with a recorded pnl_percent.", len(trades), args.origin)
    if not trades:
        logger.error(
            "No usable closed %s trades found. Either data/trading.db has no history for this "
            "origin yet, or this sandbox has no real data at all (expected here -- see "
            "CLAUDE.md). Run this on the machine with real trade history.",
            args.origin,
        )
        return 0

    run_backtest(trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
