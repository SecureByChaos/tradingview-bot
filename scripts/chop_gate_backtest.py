"""Does the efficiency ratio (a live chop signal) predict AI Origination outcomes?

THE TRIGGER
------------
A user watching a live index chart reported the market as "clearly choppy and
not tradable" while AI Origination kept taking same-direction entries anyway,
citing ADX/Supertrend/EMA-stack alignment as support each time. Traced to a
real gap: ADX, Supertrend and the EMA stack are all deliberately LAGGING (see
app/indicators.py's adx() docstring -- "ADX typically crosses 20 well after a
move is underway... a filter against trading in chop, not an entry trigger").
None of them can distinguish a clean, persistent move from one that has gone
choppy in just the last hour, because all three describe direction and
whether a bias has held, not how noisy the path getting there was. CPR is the
only existing chop-adjacent signal, and it is a static, once-per-session
prior computed from YESTERDAY's range -- not a live read of today's path.

WHAT WAS BUILT (27 Aug 2026, informational only, no gate)
------------------------------------------------------------
app/market_context.py's compute_efficiency_ratio(): Kaufman's Efficiency
Ratio over the most recent ~1 hour of 5-min bars -- net displacement divided
by total bar-to-bar path length. 1.0 = a dead-straight move; near 0 = as much
back-and-forth as net progress. Now computed on every origination cycle,
shown to the model in the TREND AGE prompt section, and persisted on every
decision (AIOriginationLog.chop_efficiency_ratio). Not backtested before
shipping the field itself -- same precedent as trend_duration_pct_of_session
and move_extent_atr, which were also added as descriptive-only fields first
and only gated later (trend age: 11 Aug, same-direction only; a
trend_duration_pct_of_session hard gate was investigated and explicitly
declined for lack of backtested support, see CLAUDE.md's repeated notes on
that). This script is the backtest that decides whether a chop-based gate
should ever follow -- it does not ship one.

WHAT THIS SCRIPT TESTS
------------------------
Real AI Origination history (ai_origination_logs JOIN strategy_trades) --
same query shape as adx_gate_backtest.py's _load_entries, same MFE/MAE-from-
ticks reasoning (strategy_trades.highest_price/lowest_price is only reliable
going forward from the 24 Aug fix; this population spans both sides of it).
chop_efficiency_ratio is read from the stored context_json, since it was
only added to context_json (via MarketContext.as_dict()) and to
AIOriginationLog's own column on the same day this script was written --
real history before that date has no value to read either way, and is
reported separately rather than silently excluded.

PART 1 buckets on three bands with the SAME qualitative labels the prompt
itself shows the model (app/ai/originator.py's _efficiency_ratio_text,
NOT independently invented here): <0.3 choppy, 0.3-0.5 mixed, >=0.5 clean.
These are a reasonable starting point, not validated -- exactly the same
status CPR_NARROW_MAX_PERCENT/CPR_WIDE_MIN_PERCENT had before any backtest
looked at them.

PART 2 sweeps two candidate floors (block <0.3, block <0.5) via the same
bootstrap-mean-diff shape every other gate backtest in this project uses
(confidence floor, ADX floor, same-direction-loss gate).

Per this project's standing rule: report what the data shows, including
"not enough evidence" as an acceptable, expected outcome at current history
size. No gate is added to app/ai/originator.py by this script.

NOT BUILT IN THIS PASS: a 2-year index-level fallback (mirroring adx_gate_
backtest.py's PART 4). That would need compute_efficiency_ratio threaded
into scripts/backtest/data.py's IndexArrays, a larger change to the shared
backtest data-loading module than this pass's scope -- worth a follow-up if
PART 1/2's real-trade sample turns out too thin to trust on its own, which
at current AI Origination history size (~three months, ~200 trades total)
is the likely outcome for a field that has existed for less than a day.

Usage:
    python -m scripts.chop_gate_backtest --db data/trading.db
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
logger = logging.getLogger("chop_gate_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses

# Matches app/ai/originator.py's _efficiency_ratio_text bucket boundaries
# exactly -- not reinvented here, so this script buckets on the same bands
# the model was actually shown.
CHOP_BUCKETS = (
    (0.0, 0.3, "<0.3 (choppy)"),
    (0.3, 0.5, "0.3-0.5 (mixed)"),
    (0.5, 1.0001, ">=0.5 (clean)"),
)
CANDIDATE_FLOORS = (0.3, 0.5)


@dataclass
class Entry:
    trade_id: str
    index_symbol: str
    decision: str
    chop_efficiency_ratio: float | None
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _load_entries(db_path: str) -> list[Entry]:
    """Same query shape and MFE/MAE-from-ticks reasoning as adx_gate_
    backtest.py's own _load_entries -- duplicated rather than imported, per
    this project's established per-script convention."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                l.trade_id     AS trade_id,
                l.decision     AS decision,
                l.chop_efficiency_ratio AS chop_efficiency_ratio,
                t.index_symbol AS index_symbol,
                t.entry_price  AS entry_price,
                t.pnl_percent  AS pnl_percent,
                t.result       AS result
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
                mfe = (high - entry_price) / entry_price * 100.0
                mae = (low - entry_price) / entry_price * 100.0
        entries.append(Entry(
            trade_id=trade_id,
            index_symbol=str(row["index_symbol"]),
            decision=str(row["decision"]),
            chop_efficiency_ratio=row["chop_efficiency_ratio"],
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260827)
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


def run_chop_buckets(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("PART 1: OUTCOME BY EFFICIENCY-RATIO BUCKET (all closed AI Origination trades)")
    logger.info("=" * 100)

    with_ratio = [e for e in entries if e.chop_efficiency_ratio is not None]
    no_ratio = [e for e in entries if e.chop_efficiency_ratio is None]
    if no_ratio:
        logger.info(
            "%d of %d entries have no recorded chop_efficiency_ratio (predate the field, added "
            "27 Aug 2026, or a genuine gap) -- reported separately, excluded from every "
            "bucket/floor comparison below.",
            len(no_ratio), len(entries),
        )
        _report_bucket("no efficiency ratio", no_ratio)
        logger.info("-" * 100)

    bucket_sizes = []
    for lo, hi, label in CHOP_BUCKETS:
        bucket = [e for e in with_ratio if lo <= e.chop_efficiency_ratio < hi]
        bucket_sizes.append(len(bucket))
        _report_bucket(label, bucket)

    logger.info("-" * 100)
    logger.info("Per index:")
    for index_symbol in sorted({e.index_symbol for e in with_ratio}):
        logger.info("  %s:", index_symbol)
        for lo, hi, label in CHOP_BUCKETS:
            bucket = [e for e in with_ratio if e.index_symbol == index_symbol and lo <= e.chop_efficiency_ratio < hi]
            _report_bucket(f"    {label}", bucket)

    logger.info("-" * 100)
    logger.info("PART 2: CANDIDATE HARD-FLOOR CHECK")
    logger.info("=" * 100)
    for floor in CANDIDATE_FLOORS:
        below = [e for e in with_ratio if e.chop_efficiency_ratio < floor]
        at_or_above = [e for e in with_ratio if e.chop_efficiency_ratio >= floor]
        logger.info("Floor: block entries with efficiency ratio < %.1f", floor)
        _report_bucket("  below floor (blocked)", below)
        _report_bucket("  at/above floor (kept)", at_or_above)
        if len(below) >= 2 and len(at_or_above) >= 2:
            lo, hi = _bootstrap_mean_diff(
                [e.pnl_percent for e in below], [e.pnl_percent for e in at_or_above],
            )
            verdict = (
                f"blocked population reliably WORSE -- floor at {floor:.1f} is supported"
                if hi < 0
                else f"blocked population reliably BETTER -- floor at {floor:.1f} would hurt"
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
            "At least one bucket is below the %s-observation trust minimum. Per this project's "
            "standing rule, that is an expected, reportable state for a field added the same day "
            "as this script -- not a reason to pick a floor from it.",
            MIN_BUCKET_LIVE,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        connection.execute("SELECT COUNT(*) FROM ai_origination_logs").fetchone()
    except sqlite3.OperationalError:
        logger.error(
            "No ai_origination_logs table found in %s. Either this sandbox has no real "
            "AI Origination history yet, or the wrong --db path was given.", args.db,
        )
        return 0
    finally:
        connection.close()

    entries = _load_entries(args.db)
    if not entries:
        logger.error(
            "No closed AI Origination entries with a joinable ai_origination_logs row found. "
            "Run this on the machine with real trade history."
        )
        return 0
    run_chop_buckets(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
