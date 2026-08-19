"""Per-provider confidence distribution snapshot -- min/max/mean/distinct values.

WHY THIS EXISTS
---------------
The same check used to originally diagnose the Claude/OpenAI confidence-scale
mismatch (18-19 Aug 2026): Claude's decisions clustered 0.10-0.75, mean 0.304;
OpenAI's clustered 0.55-0.97, mean ~0.75-0.83. That diagnosis is what motivated
the SYSTEM_PROMPT rewrite in app/ai/originator.py (calibration guidance added,
no numeric anchor tied to a single adjective, explicit instruction to use the
tails of the 0.0-1.0 range).

This script re-runs that exact check, filterable by --since, so the prompt
change can be judged on its own terms: run once right before deploying to
capture a fresh baseline, then again after 1-2 weeks of live decisions and
compare. Per the request that prompted this change, do not judge success from
a handful of trades -- wait for enough decisions to see the distribution
shape, not just a few points. Success is Claude's range widening meaningfully
toward OpenAI's (ceiling moving well above the old 0.75, real use of both
tails), not a uniform shift, and not the same number of decisions/trades as
before with nothing else different.

Reads app/ai/originator.py's AIOriginationLog table directly (one row per
decision cycle, including NONE and ERROR) -- the same population the original
393/394 vs 2/258 counts came from, not just closed trades (Claude's post-floor
closed-trade sample is far too thin on its own, see
confidence_by_provider_backtest.py).

Usage:
    python -m scripts.confidence_distribution_check --db data/trading.db
    python -m scripts.confidence_distribution_check --db data/trading.db --since 2026-08-20
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("confidence_distribution_check")


def _report_provider(connection: sqlite3.Connection, provider: str, since: str | None) -> None:
    query = (
        "SELECT confidence FROM ai_origination_logs "
        "WHERE provider = ? AND confidence IS NOT NULL"
    )
    params: list[object] = [provider]
    if since:
        query += " AND timestamp >= ?"
        params.append(since)
    values = [row[0] for row in connection.execute(query, params).fetchall()]

    if not values:
        logger.info("%-8s n=0 decisions with a recorded confidence%s", provider, f" since {since}" if since else "")
        return

    distinct = sorted(set(round(v, 2) for v in values))
    below_060 = sum(1 for v in values if v < 0.60)
    logger.info(
        "%-8s n=%-5d min=%.2f max=%.2f mean=%.3f distinct_values=%d  below_0.60=%d (%.1f%%)",
        provider, len(values), min(values), max(values), sum(values) / len(values),
        len(distinct), below_060, below_060 / len(values) * 100.0,
    )
    logger.info("         distinct values: %s", ", ".join(f"{v:.2f}" for v in distinct))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--since", default=None, help="ISO date/datetime (e.g. 2026-08-20) -- only decisions at/after this timestamp")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        try:
            total = connection.execute("SELECT COUNT(*) FROM ai_origination_logs").fetchone()[0]
        except sqlite3.OperationalError:
            logger.error(
                "No ai_origination_logs table found in %s. Either this sandbox has no real "
                "AI Origination history yet, or the wrong --db path was given.", args.db,
            )
            return 0
        logger.info("Total decision rows in ai_origination_logs: %d", total)
        if args.since:
            logger.info("Filtering to timestamp >= %s", args.since)
        logger.info("-" * 100)
        for provider in ("openai", "claude"):
            _report_provider(connection, provider, args.since)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
