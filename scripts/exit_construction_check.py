"""Does AI Origination's own real trade history show the same win/loss
asymmetry that killed the rule-based-strategy holdout test on the one
validated entry signal this project has found?

BACKGROUND
----------
The 31 Jul holdout test (EMA_STACK/ORB_BREAK[hold=2] @ 11:00-14:00, a
rule-based-strategy replay over 2-year archived candles) found win rate was
fine (52-59%) but the WIN/LOSS RATIO was not (0.53-0.68): average win ~6%,
average loss ~9-11%, because the 8%/5% trail/target exits fired well before
the wider fixed stop, capping upside while losses ran close to the full
stop distance. That is a finding about a simulated rule-based-strategy
replay -- a different exit mechanism from AI Origination's own (STOPLOSS /
TARGET / TRAIL_EXIT / STALL_EXIT / TIME_EXIT, see CLAUDE.md's "Exit paths
(AI Origination)" table -- TRAIL_EXIT only after +8% nominal activation,
STALL_EXIT at >=60 min with |P&L| <= 5% and trailing never armed).

THIS SCRIPT measures whether AI Origination's REAL trades show the same
shape, so the next real decision -- whether AI Origination's own exit
construction is worth revisiting -- is made from AI Origination's own
numbers, not by assuming the rule-based-strategy finding transfers
unchanged to a different exit engine and a different signal population.

METHODOLOGY
-----------
Reads every closed AI Origination trade (origin LIKE 'AI_ORIGIN_%'). Reports,
overall and broken down by exit_reason (STOPLOSS / TARGET / TRAIL_EXIT /
STALL_EXIT / TIME_EXIT):
  - count and share of the population
  - win rate
  - mean pnl_percent (signed)
  - among wins only: mean win %
  - among losses only: mean |loss| % (magnitude, for direct comparison)
The headline figure is the same shape the holdout finding used -- mean win
% divided by mean |loss| % -- so it is directly comparable to the 0.53-0.68
number quoted above. A bootstrap 90% CI on (mean |loss| - mean win) tests
whether that asymmetry, if present, is reliable rather than noise -- same
MIN_BUCKET_LIVE=20 trust minimum and resampling shape as every other
backtest in this project.

This is a DESCRIPTIVE report, not a candidate-threshold sweep. No gate,
stop/target/trail parameter, or exit-logic change is proposed or built
here -- only a measurement of whether the same asymmetry is present, which
is the input a real exit-construction decision would need before touching
app/ai/originator.py's stop/target/trail constants (_TRAIL_ACTIVATION_
NOMINAL, _TRAIL_WIDTH_NOMINAL, _MIN_SL_TARGET_PERCENT/_MAX_SL_TARGET_
PERCENT).

Usage:
    python -m scripts.exit_construction_check --db data/trading.db
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("exit_construction_check")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses

EXIT_REASONS = ("STOPLOSS", "TARGET", "TRAIL_EXIT", "STALL_EXIT", "TIME_EXIT")


@dataclass
class Trade:
    trade_id: str
    exit_reason: str | None
    pnl_percent: float
    is_win: bool


def _load_trades(db_path: str) -> list[Trade]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT trade_id, exit_reason, pnl_percent, result
            FROM strategy_trades
            WHERE origin LIKE 'AI_ORIGIN_%'
              AND status = 'CLOSED'
              AND pnl_percent IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        Trade(
            trade_id=str(row["trade_id"]),
            exit_reason=row["exit_reason"],
            pnl_percent=float(row["pnl_percent"]),
            is_win=(row["result"] == "WIN"),
        )
        for row in rows
    ]


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


def _report_shape(label: str, trades: list[Trade], total_population: int) -> None:
    if not trades:
        logger.info("  %-14s n=0", label)
        return
    n = len(trades)
    wins = [t.pnl_percent for t in trades if t.is_win]
    losses = [abs(t.pnl_percent) for t in trades if not t.is_win]
    win_rate = len(wins) / n * 100.0
    mean_pnl = sum(t.pnl_percent for t in trades) / n
    mean_win = sum(wins) / len(wins) if wins else None
    mean_loss = sum(losses) / len(losses) if losses else None
    ratio = (mean_win / mean_loss) if (mean_win and mean_loss) else None
    share = n / total_population * 100.0 if total_population else 0.0
    win_txt = f"{mean_win:+.2f}%" if mean_win is not None else "n/a"
    loss_txt = f"-{mean_loss:.2f}%" if mean_loss is not None else "n/a"
    ratio_txt = f"{ratio:.2f}" if ratio is not None else "n/a"
    flag = "" if n >= MIN_BUCKET_LIVE else "  [BELOW MIN SAMPLE -- treat as anecdote, not evidence]"
    logger.info(
        "  %-14s n=%-4d (%4.1f%% of population)  win_rate=%5.1f%%  mean_pnl=%+6.2f%%  "
        "mean_win=%-8s mean_loss=%-8s win/loss_ratio=%-6s%s",
        label, n, share, win_rate, mean_pnl, win_txt, loss_txt, ratio_txt, flag,
    )


def run_check(trades: list[Trade]) -> None:
    logger.info("=" * 100)
    logger.info(
        "EXIT CONSTRUCTION CHECK: %d closed AI Origination trades -- does the same win/loss "
        "asymmetry the 31 Jul holdout test found (mean win ~6%%, mean loss ~9-11%%, ratio "
        "0.53-0.68) show up in AI Origination's own real exits?",
        len(trades),
    )
    logger.info("=" * 100)
    if not trades:
        logger.info("No closed AI Origination trades found.")
        return

    logger.info("OVERALL:")
    _report_shape("  all trades", trades, len(trades))
    logger.info("-" * 100)
    logger.info("BY EXIT REASON:")
    for reason in EXIT_REASONS:
        _report_shape(f"  {reason}", [t for t in trades if t.exit_reason == reason], len(trades))
    other = [t for t in trades if t.exit_reason not in EXIT_REASONS]
    if other:
        _report_shape("  (other/unrecorded)", other, len(trades))
    logger.info("-" * 100)

    wins = [t.pnl_percent for t in trades if t.is_win]
    losses = [abs(t.pnl_percent) for t in trades if not t.is_win]
    if len(wins) >= 2 and len(losses) >= 2:
        lo, hi = _bootstrap_mean_diff(losses, wins)
        trust = "" if min(len(wins), len(losses)) >= MIN_BUCKET_LIVE else "  [below trust minimum -- read as suggestive, not confirmed]"
        verdict = (
            "losses are RELIABLY BIGGER than wins -- the same asymmetry the holdout found is present here too"
            if lo > 0
            else "wins are RELIABLY BIGGER than losses -- the opposite of the holdout's finding"
            if hi < 0
            else "no reliable difference at this sample size"
        )
        logger.info(
            "bootstrap 90%% CI on mean(|loss|) - mean(win): [%+.2f, %+.2f] -> %s%s",
            lo, hi, verdict, trust,
        )
    else:
        logger.info("Too few wins or losses for a bootstrap comparison.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    try:
        connection.execute("SELECT COUNT(*) FROM strategy_trades").fetchone()
    except sqlite3.OperationalError:
        logger.error(
            "No strategy_trades table found in %s. Either this sandbox has no real "
            "trading history yet, or the wrong --db path was given.", args.db,
        )
        return 0
    finally:
        connection.close()

    trades = _load_trades(args.db)
    run_check(trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
