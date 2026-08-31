"""Does trend age at entry predict AI Origination's win rate?

TRIGGER (31 Aug 2026, a real losing day, not evidence on its own)
--------------------------------------------------------------------
Four closed trades: two afternoon losses entered on very FRESH trends
(trend_duration_pct_of_session 4.8% and 3.2% -- 2-3 bars old, immediately
invalidated, one of them with MFE that never even turned positive), one
loss on a moderately mature trend (62.5%, the model's own reasoning
explicitly named "the move is extended" and traded anyway), and the single
win on a FULLY mature trend (100% of session). That is the opposite pattern
from what this project has spent most of its trend-age effort guarding
against -- every mechanism built so far (the soft prompt caution, the 26
Aug self-consistency requirement, the repeatedly-shelved hard gate) exists
to stop the model from treating an already-mature move as a fresh one.
Nobody has ever tested the mirror-image question: are FRESH entries (a
trend only a few bars old) any LESS reliable than mature ones, at AI
Origination's own real trade history.

WHAT THIS SCRIPT TESTS
------------------------
For every closed AI Origination trade, reads its own logged
trend_duration_pct_of_session (plus trend_duration_bars and move_extent_atr
for context) from the ai_origination_logs row joined via trade_id -- the
exact values the model's own prompt showed it at decision time, nothing
recomputed. Buckets on trend_duration_pct_of_session (explicit starting
points, not validated, same status every new threshold in this project
carries before a backtest looks at it):
  <10%    very fresh   (today's two failing entries were 4.8% / 3.2%)
  10-40%  developing
  40-70%  moderately mature
  >=70%   fully mature (today's win was 100%)
Reports win rate, mean P&L, mean MFE/MAE per bucket, plus a bootstrap 90%
CI comparing the freshest bucket (<10%) against everything else -- same
MIN_BUCKET_LIVE=20 trust minimum and resampling shape as every other
backtest in this project. A trade with no recorded trend-age field
(predates the field, or a genuine gap) is reported in its own bucket and
excluded from every comparison, never silently defaulted to a side.

DESCRIPTIVE ONLY. No gate, threshold, or prompt change is proposed or
built here -- this is the measurement a real decision would need first.
Per this project's own standing discipline: not one trade, not one day, a
real sample with a bootstrap CI that excludes zero before anything ships.

Usage:
    python -m scripts.trend_freshness_check --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("trend_freshness_check")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses

FRESHNESS_BUCKETS = (
    (0.0, 10.0, "<10% (very fresh)"),
    (10.0, 40.0, "10-40% (developing)"),
    (40.0, 70.0, "40-70% (moderately mature)"),
    (70.0, 1000.0, ">=70% (fully mature)"),
)


@dataclass
class Entry:
    trade_id: str
    index_symbol: str
    trend_duration_bars: int | None
    trend_duration_pct: float | None
    move_extent_atr: float | None
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _load_entries(db_path: str) -> list[Entry]:
    """MFE/MAE come from strategy_trade_ticks, not StrategyTrade.highest_price/
    lowest_price -- same reasoning every other real-history backtest in this
    project gives (see adx_gate_backtest.py's own _load_entries docstring):
    those two columns are only reliably maintained since the 24 Aug fix, and
    this population spans trades opened on both sides of it."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                l.trade_id                       AS trade_id,
                l.trend_duration_bars             AS trend_duration_bars,
                l.trend_duration_pct_of_session    AS trend_duration_pct,
                l.move_extent_atr                  AS move_extent_atr,
                t.index_symbol                     AS index_symbol,
                t.entry_price                      AS entry_price,
                t.pnl_percent                      AS pnl_percent,
                t.result                           AS result
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
            trend_duration_bars=row["trend_duration_bars"],
            trend_duration_pct=row["trend_duration_pct"],
            move_extent_atr=row["move_extent_atr"],
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


def _bucket_for(pct: float) -> str:
    for lo, hi, label in FRESHNESS_BUCKETS:
        if lo <= pct < hi:
            return label
    return FRESHNESS_BUCKETS[-1][2]


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260831)
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
        logger.info("  %-28s n=0", label)
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
        "  %-28s n=%-4d win_rate=%5.1f%%  mean_pnl=%+6.2f%%  mean_mfe=%-8s mean_mae=%-8s%s",
        label, n, wins / n * 100.0, mean_pnl, mfe_txt, mae_txt, flag,
    )


def run_check(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info(
        "TREND FRESHNESS CHECK: %d closed AI Origination trades -- does how young the trend "
        "was at entry predict the outcome?",
        len(entries),
    )
    logger.info("=" * 100)
    if not entries:
        logger.info("No closed AI Origination trades with a joinable decision log found.")
        return

    with_pct = [e for e in entries if e.trend_duration_pct is not None]
    missing = [e for e in entries if e.trend_duration_pct is None]
    if missing:
        logger.info(
            "%d of %d entries have no recorded trend_duration_pct_of_session (predate the field, "
            "or a genuine gap) -- reported separately, excluded from every bucket/comparison below.",
            len(missing), len(entries),
        )
        _report_bucket("no trend-age recorded", missing)
        logger.info("-" * 100)

    logger.info("BY FRESHNESS BUCKET:")
    bucketed: dict[str, list[Entry]] = {label: [] for _, _, label in FRESHNESS_BUCKETS}
    for e in with_pct:
        bucketed[_bucket_for(e.trend_duration_pct)].append(e)
    for _, _, label in FRESHNESS_BUCKETS:
        _report_bucket(f"  {label}", bucketed[label])
    logger.info("-" * 100)

    fresh_label = FRESHNESS_BUCKETS[0][2]
    fresh = bucketed[fresh_label]
    rest = [e for e in with_pct if e not in fresh]
    a = [e.pnl_percent for e in fresh]
    b = [e.pnl_percent for e in rest]
    if len(a) >= 2 and len(b) >= 2:
        lo, hi = _bootstrap_mean_diff(a, b)
        trust = "" if min(len(a), len(b)) >= MIN_BUCKET_LIVE else "  [below trust minimum -- read as suggestive, not confirmed]"
        verdict = (
            "very-fresh entries are RELIABLY BETTER -- no support for a freshness floor"
            if lo > 0
            else "very-fresh entries are RELIABLY WORSE -- real support for requiring some minimum trend age"
            if hi < 0
            else "no reliable difference at this sample size"
        )
        logger.info(
            "bootstrap 90%% CI on mean_pnl(<10%% fresh) - mean_pnl(everything else): [%+.2f, %+.2f] -> %s%s",
            lo, hi, verdict, trust,
        )
    else:
        logger.info("Too few observations in one bucket for a bootstrap comparison.")


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
    run_check(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
