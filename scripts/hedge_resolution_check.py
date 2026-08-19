"""Before/after check for the hedge-resolution SYSTEM_PROMPT change (19 Aug 2026).

WHY THIS EXISTS
----------------
The reasoning-hedge backtest (19 Aug, scripts/reasoning_hedge_backtest.py) found no
reliable outcome correlation for hedge language in ai_reasoning, at any category, even
isolated. That's a statement about DETECTING the pattern after the fact, not about
whether the underlying behaviour -- stating a real risk, then trading anyway -- is sound
reasoning. A new SYSTEM_PROMPT paragraph in app/ai/originator.py now instructs the model
to explicitly resolve any risk it names before trading, or decide NONE instead.

This is a prompt change, not a gate, so it doesn't need a pre-deployment backtest the way
a hard block would -- it needs the same before/after distribution-check discipline as the
confidence-scale prompt fix (scripts/confidence_distribution_check.py). This script is
that check for THIS change, reusing the exact hedge categorization already built rather
than re-implementing it (classify_hedge from reasoning_hedge_backtest.py), so "hedged" means
the same thing here as it did in the outcome backtest.

WHAT IT REPORTS, per time window
----------------------------------
- Total decisions, and how many had hedge language in their reasoning (any category)
- The HEDGE-THEN-TRADE RATE: of decisions with hedge language, what fraction resulted in
  a trade (BUY_CE/BUY_PE) rather than NONE. This is the direct measure of the pattern the
  prompt change targets -- it should go DOWN after the change if the model is actually
  converting more hedged setups to NONE instead of trading through them.
- Trade volume: how many trades actually opened in the window. Expected to drop somewhat
  if the fix works (that's the mechanism, not a problem) -- but not to near-zero, which
  would mean overcorrection.
- Win rate on trades that DID open and have since closed, in the window. This is the
  actual goal: if the model only trades once it can articulate a specific resolution, the
  trades that remain should be higher quality.

Run once now to log the pre-change baseline, and again after 1-2 weeks with --since set to
the deployment timestamp. Do not judge this from a handful of decisions -- same standard as
every other check in this project.

Usage:
    python -m scripts.hedge_resolution_check --db data/trading.db
    python -m scripts.hedge_resolution_check --db data/trading.db --since "2026-08-19 12:00:00"
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hedge_resolution_check")

try:
    from scripts.reasoning_hedge_backtest import classify_hedge
except ImportError:
    # Allow running as a standalone script without the package on sys.path.
    import os
    import sys as _sys

    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts.reasoning_hedge_backtest import classify_hedge

TRADE_DECISIONS = ("BUY_CE", "BUY_PE")


def run_check(connection: sqlite3.Connection, since: str | None) -> None:
    query = "SELECT decision, reasoning, trade_id FROM ai_origination_logs WHERE reasoning IS NOT NULL AND reasoning != ''"
    params: list[object] = []
    if since:
        query += " AND timestamp >= ?"
        params.append(since)
    rows = connection.execute(query, params).fetchall()

    if not rows:
        logger.info("No ai_origination_logs rows with reasoning found%s.", f" since {since}" if since else "")
        return

    total = len(rows)
    hedged_rows = [r for r in rows if classify_hedge(r[1])[0] is True]
    hedged_and_traded = [r for r in hedged_rows if r[0] in TRADE_DECISIONS]
    all_traded = [r for r in rows if r[0] in TRADE_DECISIONS]

    logger.info("Total decisions with reasoning: %d%s", total, f" (since {since})" if since else "")
    logger.info(
        "Hedged decisions: %d (%.1f%% of all decisions)",
        len(hedged_rows), len(hedged_rows) / total * 100.0,
    )
    if hedged_rows:
        rate = len(hedged_and_traded) / len(hedged_rows) * 100.0
        logger.info(
            "HEDGE-THEN-TRADE RATE: %d/%d hedged decisions resulted in a trade rather than NONE (%.1f%%)",
            len(hedged_and_traded), len(hedged_rows), rate,
        )
    else:
        logger.info("HEDGE-THEN-TRADE RATE: n/a (no hedged decisions in this window)")

    logger.info("Trade volume in window: %d (%.1f%% of all decisions)", len(all_traded), len(all_traded) / total * 100.0)

    trade_ids = [r[2] for r in all_traded if r[2]]
    if trade_ids:
        placeholders = ",".join("?" for _ in trade_ids)
        closed = connection.execute(
            f"SELECT result FROM strategy_trades WHERE trade_id IN ({placeholders}) AND status = 'CLOSED'",
            trade_ids,
        ).fetchall()
        if closed:
            wins = sum(1 for (result,) in closed if result == "WIN")
            logger.info(
                "Win rate on closed trades from this window: %d/%d (%.1f%%)",
                wins, len(closed), wins / len(closed) * 100.0,
            )
        else:
            logger.info("No closed trades yet from this window's %d opened trade(s).", len(trade_ids))
    else:
        logger.info("No trades opened in this window.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--since", default=None, help="ISO date/datetime (UTC) -- only decisions at/after this timestamp")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        try:
            connection.execute("SELECT COUNT(*) FROM ai_origination_logs").fetchone()
        except sqlite3.OperationalError:
            logger.error(
                "No ai_origination_logs table found in %s. Either this sandbox has no real "
                "AI Origination history yet, or the wrong --db path was given.", args.db,
            )
            return 0
        run_check(connection, args.since)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
