"""Does AI Origination's real trading correlate at all with the one signal
this project's own two-year backtest actually found and replicated -- midday
setups (EMA_STACK / ST_ALIGNED / ORB_BREAK / PDH_PDL_BREAK) between 11:00 and
14:00 IST -- or is that edge getting diluted by everything else the model
weighs (ADX, trend-age, chop, DI-direction) that this project has separately
found does NOT carry real signal?

BACKGROUND
----------
31 Jul 2026 walk-forward analysis (CLAUDE.md, "Indicator setups showed a
fit-window edge") found EMA_STACK / ST_ALIGNED / ORB_BREAK[hold=2] /
PDH_PDL_BREAK between 11:00-14:00 replicated a positive forward-index edge
across 6 walk-forward windows on both indices -- the one candidate that has
survived every check this project has run. The subsequent holdout test on
exactly this signal found the entry timing was NOT the problem (win rate
52-59%) -- the RISK CONSTRUCTION was (win/loss ratio 0.53-0.68, average win
~6% capped by trail/target exits against average loss ~9-11% because the
stop rarely got there first). That is a separate, already-answered question
about a rule-based-strategy replay of this signal over 2-year archived
candles -- see stop_distance_backtest.py and the holdout record itself.

THIS SCRIPT asks a different, live question that has never been checked:
does AI Origination's real 2026 trade history behave any differently when a
real decision happens to land inside this exact validated window+setup
combination, versus outside it? The model already sees every one of these
setups in its own prompt (market_context.setups, persisted verbatim on every
decision via app/ai/origination_log.py's record_decision) -- this measures
whether being right about them shows up in real outcomes, since nothing has
ever measured that. A positive, sample-adequate result here would mean the
already-validated signal is coming through in live decisions; a null result
would mean either the model isn't weighting it usefully, or the other
signals it also sees (most of which this project has found are NOT
predictive on their own -- see adx_gate_backtest.py, same_direction_
entries_backtest.py) are diluting it.

METHODOLOGY
-----------
For each closed AI Origination trade, reads its own logged setups (from the
ai_origination_logs row joined via trade_id) and its own decision timestamp
(IST, via the same db_timestamp_to_ist() shift every other script in this
project uses -- plain sqlite3 reads a DateTime(timezone=True) column back
with no offset marker at all, so the raw numbers are always the UTC value
regardless of what fromisoformat happens to parse out). A trade is
"validated" if BOTH:
  - a DIRECTION-MATCHED setup from the 31 Jul finding was active at decision
    time -- BUY_CE needs one of EMA_STACK_UP / ST_ALIGNED_UP / ORB_BREAK_UP /
    PDH_BREAK; BUY_PE needs EMA_STACK_DOWN / ST_ALIGNED_DOWN / ORB_BREAK_DOWN
    / PDL_BREAK (PDH_BREAK/PDL_BREAK carry no _UP/_DOWN suffix -- they are
    inherently one direction each, per app/market_context.py's own setup
    names), AND
  - the decision's own timestamp falls inside 11:00-14:00 IST.
Every trade not meeting both conditions is "not validated". Same bootstrap
90% CI / MIN_BUCKET_LIVE=20 trust-minimum discipline, and the same
MFE/MAE-from-ticks derivation, as every other real-history backtest in this
project (see adx_gate_backtest.py's own _load_entries docstring for why
ticks are used instead of highest_price/lowest_price).

This is index-direction-adjacent but NOT the same limitation every setup_
significance-style 2-year-archive script in this project carries -- these
are real trades with real premium P&L, not an index-direction-only proxy.

Usage:
    python -m scripts.validated_setup_window_backtest --db data/trading.db
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("validated_setup_window_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same trust minimum every other live-history backtest in this project uses
_IST_OFFSET = timedelta(hours=5, minutes=30)

WINDOW_START = time(11, 0)
WINDOW_END = time(14, 0)

UP_SETUPS = {"EMA_STACK_UP", "ST_ALIGNED_UP", "ORB_BREAK_UP", "PDH_BREAK"}
DOWN_SETUPS = {"EMA_STACK_DOWN", "ST_ALIGNED_DOWN", "ORB_BREAK_DOWN", "PDL_BREAK"}


def db_timestamp_to_ist(raw: str) -> datetime:
    """Same conversion as scripts/stall_exit_backtest.py's own helper,
    duplicated rather than imported per this project's per-script
    convention -- see that function's own docstring for why the naive
    +5:30 shift is always correct against this app's real data."""
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    naive_utc = parsed.replace(tzinfo=None) if parsed.tzinfo is None else (parsed - parsed.utcoffset()).replace(tzinfo=None)
    return naive_utc + _IST_OFFSET


def _is_validated(decision: str, setups: list[str], decision_ist: datetime) -> bool:
    if not (WINDOW_START <= decision_ist.time() < WINDOW_END):
        return False
    matching = UP_SETUPS if decision == "BUY_CE" else DOWN_SETUPS
    return bool(matching.intersection(setups))


@dataclass
class Entry:
    trade_id: str
    index_symbol: str
    decision: str
    validated: bool
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _load_entries(db_path: str) -> list[Entry]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                l.trade_id     AS trade_id,
                l.decision     AS decision,
                l.setups       AS setups,
                l.timestamp    AS timestamp,
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
        try:
            setups = json.loads(row["setups"] or "[]")
        except (TypeError, ValueError):
            setups = []
        decision_ist = db_timestamp_to_ist(row["timestamp"])
        validated = _is_validated(str(row["decision"]), setups, decision_ist)

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
            validated=validated,
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))
    return entries


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260828)
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


def run_backtest(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info(
        "VALIDATED-WINDOW BACKTEST: %d closed AI Origination trades -- does landing inside the "
        "31 Jul validated setup+window combination (EMA_STACK/ST_ALIGNED/ORB_BREAK/PDH_PDL_BREAK, "
        "11:00-14:00 IST) show up in real outcomes?",
        len(entries),
    )
    logger.info("=" * 100)
    if not entries:
        logger.info("No closed AI Origination trades with joinable decision logs found.")
        return

    validated = [e for e in entries if e.validated]
    not_validated = [e for e in entries if not e.validated]
    logger.info("VALIDATED (matched setup + 11:00-14:00 IST window):")
    _report_bucket("  validated", validated)
    logger.info("NOT VALIDATED (everything else):")
    _report_bucket("  not validated", not_validated)
    logger.info("-" * 100)

    for symbol in sorted({e.index_symbol for e in entries}):
        logger.info("Per index -- %s:", symbol)
        _report_bucket("  validated", [e for e in validated if e.index_symbol == symbol])
        _report_bucket("  not validated", [e for e in not_validated if e.index_symbol == symbol])
    logger.info("-" * 100)

    a = [e.pnl_percent for e in validated]
    b = [e.pnl_percent for e in not_validated]
    if len(a) >= 2 and len(b) >= 2:
        lo, hi = _bootstrap_mean_diff(a, b)
        trust = "" if min(len(a), len(b)) >= MIN_BUCKET_LIVE else "  [below trust minimum -- read as suggestive, not confirmed]"
        verdict = (
            "validated reliably BETTER -- the model IS getting real value from this signal"
            if lo > 0
            else "validated reliably WORSE -- do not treat this as a positive signal in live decisions"
            if hi < 0
            else "no reliable difference at this sample size"
        )
        logger.info(
            "bootstrap 90%% CI on mean_pnl(validated) - mean_pnl(not validated): [%+.2f, %+.2f] -> %s%s",
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
    run_backtest(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
