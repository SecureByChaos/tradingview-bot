"""Does a hard ADX floor predict better AI Origination outcomes?

THE TRIGGER (25 Aug, one trade, not evidence on its own)
----------------------------------------------------------
Nifty 50 BUY_PE, AI Origination/OpenAI, confidence 0.66. The dashboard's own
tradability read showed ADX 19.6 -- "Not tradable" -- at decision time. The
model's own reasoning named the same caution ("ADX is below 20... no fresh
breakout") and traded anyway, resolving it with a self-described structural
break (ORB_BREAK_DOWN + PDL_BREAK were both active). Asked directly: nothing
in _open_trade (app/ai/originator.py) currently gates on ADX at all --
_classify_tradability's TRENDING/MARGINAL/NOT_TRADABLE read
(app/platform.py) is explicitly informational-only, shown on the dashboard,
never consulted by the trading path. The only three real hard gates today
are the DTE floor, the same-direction consecutive-loss gate, and the 0.60
confidence floor.

This is the same class of question CLAUDE.md's "Trend-age caution moved to a
hard gate" entry (11 Aug) already flagged and deliberately left unresolved:
trend_duration_pct_of_session (closely related to ADX -- both describe "is
there a real trend") was kept as a soft prompt-only caution specifically
because a single day's anecdote is exactly the overfitting error this
project guards against, and a sweep range (80/90/95%) was left for a real
backtest rather than a committed number. Same standard applies here: build
the backtest, do not ship a hard gate from one trade.

WHAT THIS SCRIPT TESTS
-----------------------
Real AI Origination history (ai_origination_logs JOIN strategy_trades) --
the population an ADX gate would actually act on. ADX at decision time is
already logged per-row (AIOriginationLog.adx), so this is a filter on data
already collected, not a new computation.

Buckets on the exact bands _classify_tradability already uses (ADX_NO_TREND
= 20, ADX_TRENDING = 25, from app/market_context.py -- not reinvented here):
  <20    NOT_TRADABLE
  20-25  MARGINAL
  >=25   TRENDING

Two candidate hard-floor cuts are swept (block below 20, block below 25),
each via a bootstrap comparison of below-floor vs at-or-above-floor mean
P&L -- the same floor-bootstrap shape confidence_sizing_backtest.py already
used to validate the 0.60 confidence floor. A trade with no recorded ADX
(pre-dates the field, or a genuine gap) is reported in its own bucket and
excluded from every floor comparison rather than silently defaulted to a
side.

Per the project's standing rule (see break_confirmation_backtest.py,
trend_age_gate_backtest.py, same_direction_entries_backtest.py): report
what the data shows, including "not enough evidence" as an acceptable
outcome. No gate is added to app/ai/originator.py by this script -- that is
a deliberate follow-up decision, made only if a floor clears the trust
minimum with a bootstrap CI that excludes zero.

Usage:
    python -m scripts.adx_gate_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("adx_gate_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses

# Matches app/market_context.py's ADX_NO_TREND / ADX_TRENDING exactly --
# not reinvented here. app/platform.py's _classify_tradability reads these
# same two constants for the dashboard's TRENDING/MARGINAL/NOT_TRADABLE label.
ADX_NO_TREND = 20.0
ADX_TRENDING = 25.0

ADX_BUCKETS = (
    (0.0, ADX_NO_TREND, "<20 (NOT_TRADABLE)"),
    (ADX_NO_TREND, ADX_TRENDING, "20-25 (MARGINAL)"),
    (ADX_TRENDING, 1000.0, ">=25 (TRENDING)"),
)

CANDIDATE_FLOORS = (ADX_NO_TREND, ADX_TRENDING)


@dataclass
class Entry:
    trade_id: str
    index_symbol: str
    decision: str
    adx: float | None
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _load_entries(db_path: str) -> list[Entry]:
    """MFE/MAE come from strategy_trade_ticks, not StrategyTrade.highest_price/
    lowest_price -- same reasoning confidence_sizing_backtest.py's own
    _load_entries docstring already gives: those two columns are only
    reliably maintained going forward from the 24 Aug lowest_price fix, and
    this population spans trades opened both before and after that fix, so
    ticks stay the one source that's correct across the whole history."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                l.trade_id    AS trade_id,
                l.decision    AS decision,
                l.adx         AS adx,
                t.index_symbol AS index_symbol,
                t.entry_price AS entry_price,
                t.pnl_percent AS pnl_percent,
                t.result      AS result
            FROM ai_origination_logs l
            JOIN strategy_trades t ON t.trade_id = l.trade_id
            WHERE l.decision IN ('BUY_CE', 'BUY_PE')
              AND l.trade_id IS NOT NULL
              AND t.status = 'CLOSED'
              AND t.pnl_percent IS NOT NULL
            """
        ).fetchall()
        tick_extremes = {
            row["trade_id"]: (row["low"], row["high"])
            for row in connection.execute(
                """
                SELECT trade_id, MIN(premium) AS low, MAX(premium) AS high
                FROM strategy_trade_ticks
                GROUP BY trade_id
                """
            ).fetchall()
        }
    finally:
        connection.close()

    entries: list[Entry] = []
    for row in rows:
        trade_id = str(row["trade_id"])
        entry_price = row["entry_price"]
        extremes = tick_extremes.get(trade_id)
        mfe = mae = None
        if extremes and entry_price:
            low, high = extremes
            if low is not None and high is not None:
                # Every AI Origination trade is long (BUY_CE/BUY_PE).
                mfe = (high - entry_price) / entry_price * 100.0
                mae = (low - entry_price) / entry_price * 100.0
        entries.append(Entry(
            trade_id=trade_id,
            index_symbol=str(row["index_symbol"]),
            decision=str(row["decision"]),
            adx=row["adx"],
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260825)
    diffs = []
    for _ in range(rounds):
        sample_a = [rng.choice(a) for _ in a]
        sample_b = [rng.choice(b) for _ in b]
        diffs.append(sum(sample_a) / len(sample_a) - sum(sample_b) / len(sample_b))
    diffs.sort()
    lo = diffs[int(0.05 * rounds)]
    hi = diffs[int(0.95 * rounds) - 1]
    return lo, hi


def _report_bucket(label: str, entries: list[Entry]) -> None:
    if not entries:
        logger.info("  %-24s n=0", label)
        return
    n = len(entries)
    wins = sum(1 for e in entries if e.is_win)
    mean_pnl = sum(e.pnl_percent for e in entries) / n
    mfes = [e.mfe_percent for e in entries if e.mfe_percent is not None]
    maes = [e.mae_percent for e in entries if e.mae_percent is not None]
    mfe_txt = f"{sum(mfes) / len(mfes):+.2f}%" if mfes else "n/a"
    mae_txt = f"{sum(maes) / len(maes):+.2f}%" if maes else "n/a"
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-24s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_mfe=%-8s mean_mae=%-8s%s",
        label, n, wins / n * 100.0, mean_pnl, mfe_txt, mae_txt, flag,
    )


def run_adx_buckets(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("PART 1: OUTCOME BY ADX BUCKET (all closed AI Origination trades)")
    logger.info("=" * 100)

    with_adx = [e for e in entries if e.adx is not None]
    no_adx = [e for e in entries if e.adx is None]
    if no_adx:
        logger.info(
            "%d of %d entries have no recorded ADX (predate the field, or a genuine gap) -- "
            "reported separately, excluded from every bucket/floor comparison below.",
            len(no_adx), len(entries),
        )
        _report_bucket("no ADX recorded", no_adx)
        logger.info("-" * 100)

    bucket_sizes = []
    for lo, hi, label in ADX_BUCKETS:
        bucket = [e for e in with_adx if lo <= e.adx < hi]
        bucket_sizes.append(len(bucket))
        _report_bucket(label, bucket)

    logger.info("-" * 100)
    logger.info("Per index:")
    for index_symbol in sorted({e.index_symbol for e in with_adx}):
        logger.info("  %s:", index_symbol)
        for lo, hi, label in ADX_BUCKETS:
            bucket = [e for e in with_adx if e.index_symbol == index_symbol and lo <= e.adx < hi]
            _report_bucket(f"    {label}", bucket)

    logger.info("-" * 100)
    logger.info("PART 2: CANDIDATE HARD-FLOOR CHECK")
    logger.info("=" * 100)
    for floor in CANDIDATE_FLOORS:
        below = [e for e in with_adx if e.adx < floor]
        at_or_above = [e for e in with_adx if e.adx >= floor]
        logger.info("Floor: block entries with ADX < %.0f", floor)
        _report_bucket("  below floor (blocked)", below)
        _report_bucket("  at/above floor (kept)", at_or_above)
        if len(below) >= 2 and len(at_or_above) >= 2:
            lo, hi = _bootstrap_mean_diff(
                [e.pnl_percent for e in below], [e.pnl_percent for e in at_or_above],
            )
            verdict = (
                f"blocked population reliably WORSE -- floor at {floor:.0f} is supported"
                if hi < 0
                else f"blocked population reliably BETTER -- floor at {floor:.0f} would hurt"
                if lo > 0
                else "no reliable difference at this sample size"
            )
            trust = (
                "" if min(len(below), len(at_or_above)) >= MIN_BUCKET_LIVE
                else "  [smaller bucket below the trust minimum -- read as suggestive, not confirmed]"
            )
            logger.info(
                "  bootstrap 90%% CI on mean_pnl(below) - mean_pnl(at/above): [%+.2f, %+.2f] -> %s%s",
                lo, hi, verdict, trust,
            )
        else:
            logger.info("  Too few observations on one side for a bootstrap comparison.")
        logger.info("-" * 100)

    if bucket_sizes and min(bucket_sizes) < MIN_BUCKET_LIVE:
        logger.info(
            "At least one ADX bucket is below the %s-observation trust minimum. Per this "
            "project's standing rule, that is an expected, reportable state at the current "
            "history size -- not a reason to pick a floor from it.",
            MIN_BUCKET_LIVE,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    entries = _load_entries(args.db)
    if not entries:
        logger.error(
            "No closed AI Origination entries with a joinable ai_origination_logs row found. "
            "Either data/trading.db has no history yet, or this sandbox has no real data at "
            "all (expected here -- see CLAUDE.md). Run this on the machine with real trade history."
        )
        return 1

    run_adx_buckets(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
