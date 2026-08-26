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
A decision is a candidate violation of the new instruction if ALL THREE:
  1. It actually opened a trade (decision IN ('BUY_CE', 'BUY_PE')). A NONE
     decision can never violate "don't call a stale move fresh and trade on
     it" -- nothing was traded. See the real-data bug this fixed, below.
  2. Its reasoning text contains freshness/newness language ("fresh", "newly
     confirm", "just confirm", "new breakout", "newly break") -- matching the
     exact phrasing the new prompt paragraph names.
  3. Its own logged context shows trend_duration_pct_of_session >= FRESHNESS_
     TREND_PCT_FLOOR (70, matching the prompt's own "roughly 70-80% or higher")
     OR move_extent_atr >= FRESHNESS_ATR_FLOOR (5.0, chosen from the 26 Aug
     trigger trade's own 5.99 ATR reading as a starting point, not a validated
     threshold -- this script is a diagnostic flag for manual review, not a
     gate, so a conservative floor that over-flags slightly is preferable to
     one that under-flags).

A decision missing either context field is not flagged either way -- "unknown"
and "condition not met" are different facts, same convention as every other
context-dependent check in this project.

REAL-DATA BUG FOUND AND FIXED THE SAME DAY (26 Aug 2026, first live run)
--------------------------------------------------------------------------
The first real run against production found 38/46 "flagged" decisions --
every single one was decision=NONE, and every single one's reasoning used
freshness language NEGATED ("there is no fresh breakout", "no fresh
momentum"), citing the absence of a fresh confirmation as the reason to
correctly decline. _mentions_freshness is a bare substring match with no
negation awareness, so "no fresh breakout" and "a fresh confirmed break"
both matched identically -- the check was flagging the prompt working
exactly as intended (the model citing high trend_duration_pct_of_session /
move_extent_atr to justify NOT trading) as if it were 38 violations of it.
Condition 1 above (decision must be BUY_CE/BUY_PE) is the fix: a self-
consistency violation requires a trade to have actually been taken on the
contradicted "fresh" framing, which a NONE decision definitionally cannot
be. This does not fully solve keyword negation in general (a BUY decision
could theoretically still use "no fresh X" language about something other
than its own entry thesis) but real data gives zero evidence that pattern
occurs, so it is not built against speculatively -- restricting to trade
decisions removes 100% of the real false positives observed. run_outcome_
backtest() below was never affected: it already only ever reads rows from
strategy_trades, which only contains trades that opened.

OUTCOME BACKTEST (26 Aug 2026, added after a second request to evaluate this
with the same statistical discipline as every other backtest in this project)
------------------------------------------------------------------------------
The section above is a decision-level AUDIT -- it flags candidates for manual
review, nothing more. It does not say whether flagged trades actually perform
worse. run_outcome_backtest() answers that, restricted to CLOSED AI Origination
trades (StrategyTrade.ai_reasoning/market_context_json, not the decision-level
ai_origination_logs table, since only opened trades have a real pnl_percent):
flagged (freshness language + context contradicts, same two-part test as
above) vs not-flagged, win rate / mean P&L / mean MAE per bucket, and a
bootstrap 90% CI on the mean P&L difference -- same MIN_BUCKET_LIVE=20 trust
minimum and the same bootstrap-resampling shape as every other live-history
backtest in this project (e.g. reasoning_hedge_backtest.py's _bootstrap_mean_
diff; duplicated here rather than imported, per this project's established
per-script convention). A CI that excludes zero is the one bar this project
has required before treating any prior finding (confidence floor, hedge
categories, stop-distance sweep) as real; a CI that crosses zero is reported
exactly as plainly as a real effect would be -- "not yet enough evidence" is
an intended, useful outcome of this check, not a failure of it.

Usage:
    python -m scripts.freshness_resolution_check --db data/trading.db
    python -m scripts.freshness_resolution_check --db data/trading.db --since "2026-08-26 12:00:00"
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("freshness_resolution_check")

FRESHNESS_KEYWORDS = ("fresh", "newly confirm", "just confirm", "new breakout", "newly break")
FRESHNESS_TREND_PCT_FLOOR = 70.0
FRESHNESS_ATR_FLOOR = 5.0
BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses


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


TRADE_DECISIONS = ("BUY_CE", "BUY_PE")


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
    # NONE/ERROR decisions are excluded up front -- only a decision that
    # actually opened a trade (BUY_CE/BUY_PE) can violate "don't call a stale
    # move fresh and trade on it". See the module docstring's "REAL-DATA BUG"
    # section: without this, "there is no fresh breakout" (a correct NONE
    # decline) and "a fresh confirmed break" (the real violation shape) match
    # the same bare keyword search.
    trade_rows = [r for r in rows if r[2] in TRADE_DECISIONS]
    freshness_rows = [r for r in trade_rows if _mentions_freshness(r[4])]
    flagged = [r for r in freshness_rows if _context_contradicts_freshness(r[5])]

    logger.info("Total decisions with reasoning: %d%s", total, f" (since {since})" if since else "")
    logger.info(
        "Of those, decisions that opened a trade (BUY_CE/BUY_PE): %d", len(trade_rows),
    )
    logger.info(
        "Trade decisions using freshness/newness language: %d%s",
        len(freshness_rows),
        f" ({len(freshness_rows) / len(trade_rows) * 100.0:.1f}%% of trade decisions)" if trade_rows else "",
    )
    logger.info(
        "FLAGGED (opened a trade + freshness language + trend_duration_pct_of_session >= %.0f or "
        "move_extent_atr >= %.1f in the same context): %d",
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


@dataclass
class TradeEntry:
    trade_id: str
    pnl_percent: float
    is_win: bool
    mae_percent: float | None
    flagged: bool


def _load_trade_entries(connection: sqlite3.Connection, since: str | None) -> list[TradeEntry]:
    """CLOSED AI Origination trades with a recorded reasoning + market context.
    market_context_json (StrategyTrade) is the same MarketContext.as_dict() shape
    as ai_origination_logs.context_json -- both are written from the same
    market_context object at entry time -- so _context_contradicts_freshness
    applies unchanged. MAE from strategy_trade_ticks, not the stored lowest_price
    column, per the same reasoning reasoning_hedge_backtest.py's _load_entries
    documents: lowest_price is pinned at entry_price for this always-long
    population and is not a real adverse-excursion figure."""
    query = (
        "SELECT trade_id, entry_price, ai_reasoning, market_context_json, pnl_percent, result "
        "FROM strategy_trades "
        "WHERE origin LIKE 'AI_ORIGIN_%' AND status = 'CLOSED' "
        "AND ai_reasoning IS NOT NULL AND ai_reasoning != '' "
        "AND market_context_json IS NOT NULL AND pnl_percent IS NOT NULL"
    )
    params: list[object] = []
    if since:
        query += " AND entry_time >= ?"
        params.append(since)
    rows = connection.execute(query, params).fetchall()

    tick_extremes = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT trade_id, MIN(premium) AS low, MAX(premium) AS high FROM strategy_trade_ticks GROUP BY trade_id"
        ).fetchall()
    }

    entries: list[TradeEntry] = []
    for trade_id, entry_price, reasoning, context_json, pnl_percent, result in rows:
        flagged = _mentions_freshness(reasoning or "") and _context_contradicts_freshness(context_json)
        mae = None
        extremes = tick_extremes.get(trade_id)
        if extremes and entry_price:
            low, _high = extremes
            if low is not None:
                mae = (low - entry_price) / entry_price * 100.0
        entries.append(TradeEntry(
            trade_id=str(trade_id), pnl_percent=float(pnl_percent),
            is_win=(result == "WIN"), mae_percent=mae, flagged=flagged,
        ))
    return entries


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260826)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def _report_bucket(label: str, entries: list[TradeEntry]) -> None:
    if not entries:
        logger.info("  %-14s n=0", label)
        return
    n = len(entries)
    wins = sum(1 for e in entries if e.is_win)
    mean_pnl = sum(e.pnl_percent for e in entries) / n
    maes = [e.mae_percent for e in entries if e.mae_percent is not None]
    mae_txt = f"{sum(maes) / len(maes):+.2f}%" if maes else "n/a"
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-14s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_mae=%-8s%s",
        label, n, wins / n * 100.0, mean_pnl, mae_txt, flag,
    )


def run_outcome_backtest(connection: sqlite3.Connection, since: str | None) -> None:
    entries = _load_trade_entries(connection, since)
    logger.info("=" * 100)
    logger.info(
        "FRESHNESS-FLAGGED OUTCOME BACKTEST (%d closed AI Origination trades with reasoning + context)%s",
        len(entries), f" since {since}" if since else "",
    )
    logger.info("=" * 100)
    if not entries:
        logger.info("No closed AI Origination trades with reasoning + market context in this window.")
        return

    flagged = [e for e in entries if e.flagged]
    not_flagged = [e for e in entries if not e.flagged]
    _report_bucket("flagged", flagged)
    _report_bucket("not flagged", not_flagged)

    if len(flagged) >= 2 and len(not_flagged) >= 2:
        lo, hi = _bootstrap_mean_diff([e.pnl_percent for e in flagged], [e.pnl_percent for e in not_flagged])
        trust = (
            "" if min(len(flagged), len(not_flagged)) >= MIN_BUCKET_LIVE
            else "  [below trust minimum on the thinner side -- treat as suggestive, not confirmed]"
        )
        verdict = (
            "flagged reliably WORSE" if hi < 0
            else "flagged reliably BETTER" if lo > 0
            else "no reliable difference at this sample size (CI crosses zero)"
        )
        logger.info(
            "bootstrap 90%% CI on mean_pnl(flagged) - mean_pnl(not flagged): [%+.2f, %+.2f] -> %s%s",
            lo, hi, verdict, trust,
        )
    else:
        logger.info("Too few observations in one bucket for a bootstrap comparison.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--since", default=None, help="ISO date/datetime (UTC) -- only decisions/trades at/after this timestamp")
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
        run_outcome_backtest(connection, args.since)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
