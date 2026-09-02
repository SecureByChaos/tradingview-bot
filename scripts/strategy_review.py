"""Full cross-strategy review over a rolling real-history window.

WHAT THIS IS
-------------
A single report over the last --days (default 60) of CLOSED trades, broken
out by strategy/origin bucket -- the same shape as the ad hoc per-day
analysis this project has been doing by hand off uploaded trade-history
exports, but over a real multi-week window and across every population at
once instead of one day at a time. Reuses the same origin-isolation rule
CLAUDE.md states repeatedly: match AI Origination with `LIKE 'AI_ORIGIN_%'`,
never `!= 'SIGNAL'`, and AI_ALT_* shadow trades are excluded outright --
they were never a position anyone held, and mixing them into the trade
population would misrepresent it the same way the removed AI Alternatives
comparison page used to.

Per bucket: trade count, win rate, net P&L (sum, capital-weighted return%),
mean P&L% per trade, and an exit-reason breakdown. A second section repeats
the giveback check already run by hand on 1-2 Sep (does a loss's own MFE
run positive before finishing negative, and by how much), now over the
whole window and every bucket instead of two days -- so whatever pattern
showed up there can be read as one data point or as a real trend, not
guessed at.

MFE/MAE FROM TICKS, NOT highest_price/lowest_price
-----------------------------------------------------
Same reason near_target_lock_backtest.py and every confidence/exit-outcome
backtest in this project already made this choice: lowest_price was pinned
at the entry-time seed value for every long trade until the 24 Aug fix (see
CLAUDE.md), so a highest_price/lowest_price-derived MAE is unreliable for
anything opened before that date. strategy_trade_ticks (the 30s premium
samples the live monitor already records) has no such history -- a trade
with too few recorded ticks is reported as excluded, never silently
defaulted to "no favorable move".

Usage:
    python -m scripts.strategy_review --db data/trading.db
    python -m scripts.strategy_review --db data/trading.db --days 14
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("strategy_review")

DEFAULT_DAYS = 60
MIN_TICKS_FOR_EXCURSION = 2


@dataclass
class Trade:
    trade_id: str
    bucket: str
    entry_price: float
    net_pnl: float
    pnl_percent: float
    investment_amount: float
    result: str
    exit_reason: str | None


def _bucket(origin: str, strategy_name: str) -> str | None:
    """Display bucket name, or None to exclude (AI_ALT_* shadow trades)."""
    if origin == "SIGNAL":
        return f"Signal: {strategy_name}"
    if origin.startswith("AI_ORIGIN_"):
        provider = origin.removeprefix("AI_ORIGIN_").title()
        return f"AI Origination ({provider})"
    if origin == "VALIDATED_SIGNAL":
        return "Validated Signal"
    if origin == "AUTONOMOUS_AI":
        return "Autonomous AI"
    if origin == "QUICK_SCALP":
        return "Quick Scalp"
    return None  # AI_ALT_* and anything unrecognised -- never a real position


def _load_trades(db_path: str, since_utc: str) -> list[Trade]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, origin, strategy_name, entry_price, net_pnl,
                   pnl_percent, investment_amount, result, exit_reason
            FROM strategy_trades
            WHERE status = 'CLOSED' AND entry_time >= ?
            ORDER BY entry_time
            """,
            (since_utc,),
        ).fetchall()
    finally:
        connection.close()
    trades = []
    for row in rows:
        bucket = _bucket(str(row["origin"]), str(row["strategy_name"]))
        if bucket is None:
            continue
        trades.append(
            Trade(
                trade_id=str(row["trade_id"]), bucket=bucket,
                entry_price=float(row["entry_price"] or 0.0),
                net_pnl=float(row["net_pnl"] or 0.0),
                pnl_percent=float(row["pnl_percent"] or 0.0),
                investment_amount=float(row["investment_amount"] or 0.0),
                result=str(row["result"]), exit_reason=row["exit_reason"],
            )
        )
    return trades


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


@dataclass
class BucketStats:
    n: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    net_pnl_sum: float = 0.0
    investment_sum: float = 0.0
    pnl_percent_sum: float = 0.0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    # Giveback: losses whose own MFE (from ticks) ran positive before the
    # trade finished negative -- and by how much, on average.
    losses_with_ticks: int = 0
    losses_with_positive_mfe: int = 0
    mfe_sum_for_giveback_losses: float = 0.0


def _summarize(trades: list[Trade], ticks_by_trade: dict[str, list[float]]) -> dict[str, BucketStats]:
    stats: dict[str, BucketStats] = {}
    for t in trades:
        s = stats.setdefault(t.bucket, BucketStats())
        s.n += 1
        s.net_pnl_sum += t.net_pnl
        s.investment_sum += t.investment_amount
        s.pnl_percent_sum += t.pnl_percent
        if t.result == "WIN":
            s.wins += 1
        elif t.result == "LOSS":
            s.losses += 1
        elif t.result == "BREAKEVEN":
            s.breakevens += 1
        reason = t.exit_reason or "UNKNOWN"
        s.exit_reasons[reason] = s.exit_reasons.get(reason, 0) + 1

        if t.result == "LOSS":
            premiums = ticks_by_trade.get(t.trade_id, [])
            if len(premiums) >= MIN_TICKS_FOR_EXCURSION and t.entry_price > 0:
                mfe_percent = (max(premiums) - t.entry_price) / t.entry_price * 100.0
                s.losses_with_ticks += 1
                if mfe_percent > 0:
                    s.losses_with_positive_mfe += 1
                    s.mfe_sum_for_giveback_losses += mfe_percent
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    since = datetime.utcnow() - timedelta(days=args.days)
    since_utc = since.strftime("%Y-%m-%d %H:%M:%S.%f")

    trades = _load_trades(args.db, since_utc)
    logger.info("=" * 100)
    logger.info("Strategy review: last %s days (since %s UTC)", args.days, since_utc[:10])
    logger.info("=" * 100)
    logger.info("Closed trades, all strategies, AI_ALT_* excluded: %s", len(trades))
    if not trades:
        logger.error("No closed trades in this window. Nothing to report.")
        return 1

    ticks_by_trade = _load_ticks_by_trade(args.db, [t.trade_id for t in trades])
    stats = _summarize(trades, ticks_by_trade)

    logger.info("=" * 100)
    logger.info(
        "  %-28s %5s %6s %11s %11s %9s", "bucket", "n", "win%", "net_pnl_Rs", "return%", "mean_pnl%",
    )
    for bucket in sorted(stats, key=lambda b: -stats[b].n):
        s = stats[bucket]
        win_rate = s.wins / s.n * 100.0 if s.n else 0.0
        capital_return = s.net_pnl_sum / s.investment_sum * 100.0 if s.investment_sum > 0 else 0.0
        mean_pnl = s.pnl_percent_sum / s.n if s.n else 0.0
        logger.info(
            "  %-28s %5d %5.1f%% %10.2f %10.2f%% %8.2f%%",
            bucket, s.n, win_rate, s.net_pnl_sum, capital_return, mean_pnl,
        )

    logger.info("=" * 100)
    logger.info("EXIT REASON BREAKDOWN")
    for bucket in sorted(stats, key=lambda b: -stats[b].n):
        s = stats[bucket]
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(s.exit_reasons.items(), key=lambda kv: -kv[1]))
        logger.info("  %-28s %s", bucket, reasons)

    logger.info("=" * 100)
    logger.info("GIVEBACK CHECK -- of each bucket's losses, how many had a positive MFE first")
    logger.info(
        "  %-28s %8s %14s %14s", "bucket", "losses", "w/ tick data", "MFE>0 before loss",
    )
    total_losses_with_ticks = 0
    total_giveback = 0
    for bucket in sorted(stats, key=lambda b: -stats[b].n):
        s = stats[bucket]
        if s.losses == 0:
            continue
        giveback_rate = (
            s.losses_with_positive_mfe / s.losses_with_ticks * 100.0 if s.losses_with_ticks else 0.0
        )
        mean_giveback_mfe = (
            s.mfe_sum_for_giveback_losses / s.losses_with_positive_mfe
            if s.losses_with_positive_mfe else 0.0
        )
        logger.info(
            "  %-28s %8d %14d %6d (%.0f%%, mean MFE %.2f%%)",
            bucket, s.losses, s.losses_with_ticks, s.losses_with_positive_mfe,
            giveback_rate, mean_giveback_mfe,
        )
        total_losses_with_ticks += s.losses_with_ticks
        total_giveback += s.losses_with_positive_mfe

    logger.info("=" * 100)
    if total_losses_with_ticks:
        logger.info(
            "OVERALL: %s of %s losses with tick data (%.0f%%) had a positive MFE before finishing "
            "negative.", total_giveback, total_losses_with_ticks,
            total_giveback / total_losses_with_ticks * 100.0,
        )
    logger.info(
        "Losses excluded from the giveback check for fewer than %s recorded ticks are not counted "
        "in either column above -- reported separately per bucket only via the 'losses' vs "
        "'w/ tick data' gap.", MIN_TICKS_FOR_EXCURSION,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
