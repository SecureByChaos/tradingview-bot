"""Post-hoc check for the self-consistency SYSTEM_PROMPT change (26 Aug 2026).

WHY THIS EXISTS
----------------
A real trade (26 Aug, Nifty PE, confidence 0.89) resolved a self-stated exhaustion
risk by calling the setup a "fresh confirmed break" -- but trend_duration_pct_of_
session was 100.0 in the SAME context the model was reasoning from. The 19-20 Aug
hedge-resolution fix (see hedge_resolution_check.py) already requires the model to
produce a specific resolution rather than a bare "but" -- it does not check whether
that resolution is actually TRUE relative to data already in the prompt. A new
SYSTEM_PROMPT paragraph now tells the model not to call a move "fresh"/"newly
confirmed" when trend_duration_pct_of_session is high, or a breakout "new" when
move_extent_atr is already large.

This is a prompt change, not a gate -- same before/after distribution-check
discipline as every other prompt-only fix this project has shipped (confidence
scale, hedge resolution). Run once now for the baseline, again after 1-2 weeks
with --since set to the deployment timestamp.

WHAT IT FLAGS
--------------
A decision is a candidate violation of the new instruction if BOTH:
  1. Its reasoning text contains freshness/newness language ("fresh", "newly
     confirm", "just confirm", "new breakout", "newly break") -- matching the
     exact phrasing the new prompt paragraph names.
  2. Its own logged context shows trend_duration_pct_of_session >= FRESHNESS_
     TREND_PCT_FLOOR (70, matching the prompt's own "roughly 70-80% or higher")
     OR move_extent_atr >= FRESHNESS_ATR_FLOOR (5.0, chosen from the 26 Aug
     trigger trade's own 5.99 ATR reading as a starting point, not a validated
     threshold -- this script is a diagnostic flag for manual review, not a
     gate, so a conservative floor that over-flags slightly is preferable to
     one that under-flags).

A decision missing either field in its context_json is not flagged either way --
"unknown" and "condition not met" are different facts, same convention as every
other context-dependent check in this project.

Usage:
    python -m scripts.freshness_resolution_check --db data/trading.db
    python -m scripts.freshness_resolution_check --db data/trading.db --since "2026-08-26 12:00:00"
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("freshness_resolution_check")

FRESHNESS_KEYWORDS = ("fresh", "newly confirm", "just confirm", "new breakout", "newly break")
FRESHNESS_TREND_PCT_FLOOR = 70.0
FRESHNESS_ATR_FLOOR = 5.0


def _mentions_freshness(reasoning: str) -> bool:
    lowered = reasoning.lower()
    return any(keyword in lowered for keyword in FRESHNESS_KEYWORDS)


def _context_contradicts_freshness(context_json: str) -> bool:
    try:
        context = json.loads(context_json or "{}")
    except (TypeError, ValueError):
        return False
    trend_pct = context.get("trend_duration_pct_of_session")
    move_atr = context.get("move_extent_atr")
    if trend_pct is not None and trend_pct >= FRESHNESS_TREND_PCT_FLOOR:
        return True
    if move_atr is not None and move_atr >= FRESHNESS_ATR_FLOOR:
        return True
    return False


def run_check(connection: sqlite3.Connection, since: str | None) -> None:
    query = (
        "SELECT timestamp, index_name, decision, trade_id, reasoning, context_json "
        "FROM ai_origination_logs WHERE reasoning IS NOT NULL AND reasoning != ''"
    )
    params: list[object] = []
    if since:
        query += " AND timestamp >= ?"
        params.append(since)
    rows = connection.execute(query, params).fetchall()

    if not rows:
        logger.info("No ai_origination_logs rows with reasoning found%s.", f" since {since}" if since else "")
        return

    total = len(rows)
    freshness_rows = [r for r in rows if _mentions_freshness(r[4])]
    flagged = [r for r in freshness_rows if _context_contradicts_freshness(r[5])]

    logger.info("Total decisions with reasoning: %d%s", total, f" (since {since})" if since else "")
    logger.info(
        "Decisions using freshness/newness language: %d (%.1f%% of all decisions)",
        len(freshness_rows), len(freshness_rows) / total * 100.0,
    )
    logger.info(
        "FLAGGED (freshness language + trend_duration_pct_of_session >= %.0f or move_extent_atr >= %.1f "
        "in the same context): %d",
        FRESHNESS_TREND_PCT_FLOOR, FRESHNESS_ATR_FLOOR, len(flagged),
    )
    if flagged:
        logger.info("-" * 100)
        for timestamp, index_name, decision, trade_id, reasoning, _context_json in flagged:
            logger.info(
                "  %s %s %s%s: %s",
                timestamp, index_name, decision,
                f" (trade {trade_id})" if trade_id else "",
                reasoning[:200] + ("..." if len(reasoning) > 200 else ""),
            )
    else:
        logger.info("No candidate violations found in this window.")


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
