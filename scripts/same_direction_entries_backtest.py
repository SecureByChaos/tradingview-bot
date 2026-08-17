"""Does same_direction_entries_today at entry predict AI Origination outcomes?

BACKGROUND
----------
`_same_direction_entries_today` (app/ai/originator.py) counts how many AI Origination
entries already opened today, same index + same direction (BUY_CE/BUY_PE), across both
providers, before a given decision. Since 11 Aug 2026 a hard gate
(`_MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK = 2`) blocks a new entry outright once that
count reaches 2 for its own direction -- shipped from a same-day anecdote (7
same-direction entries across two indices), not a backtest, per CLAUDE.md's "Trend-age
caution moved to a hard gate" entry. `trend_age_gate_backtest.py` validated a related but
different question at the time (a same-direction-count *proxy* reconstructed from the
2-year index-level candle archive, since no real AI Origination history existed yet).

This script asks the more direct version now that real history exists: bucket every
closed AI Origination trade by its OWN stored same_direction_entries_today[trade.signal]
value at entry (0, 1, 2, 3+) and check win rate / mean P&L / mean MAE (and MFE) per
bucket. Because the gate has blocked count>=2 since 11 Aug, expect buckets 2 and 3+ to be
thin or empty except for pre-gate history -- report that plainly rather than hiding it.

Two comparisons, both policy-relevant:
  1. bucket 0 vs bucket 1 -- would tightening the gate to >=1 (instead of >=2) have been
     justified?
  2. bucket <2 (0+1 combined) vs bucket >=2 -- does the data support the threshold
     actually shipped?

Population: every closed AI Origination trade (origin LIKE 'AI_ORIGIN_%') whose
market_context_json contains a same_direction_entries_today value for its own signal.
Trades predating this field (no key present) are excluded, not defaulted to 0 --
"unknown" and "zero prior entries" are different facts.

Usage:
    python -m scripts.same_direction_entries_backtest --db data/trading.db
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
logger = logging.getLogger("same_direction_entries_backtest")

BOOTSTRAP_ROUNDS = 10000
MIN_BUCKET_LIVE = 20  # same live-history trust minimum used throughout scripts/*.py

BUCKET_LABELS = ("0", "1", "2", "3+")


@dataclass
class Entry:
    trade_id: str
    index_symbol: str
    same_direction_count: int
    pnl_percent: float
    mfe_percent: float | None
    mae_percent: float | None
    is_win: bool


def _bucket_label(count: int) -> str:
    return "3+" if count >= 3 else str(count)


def _load_entries(db_path: str) -> list[Entry]:
    """MFE/MAE come from strategy_trade_ticks (real 30s premium samples), not from
    StrategyTrade.highest_price/lowest_price -- see confidence_sizing_backtest.py's
    _load_entries docstring for why the stored columns are unusable for this (lowest_price
    stays pinned at its entry-time seed value for every long trade, so a lowest_price-
    derived MAE is deterministically 0.00% and not a real adverse excursion). Mirrored
    here rather than duplicating the bug."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                trade_id, index_symbol, signal, entry_price, pnl_percent, result,
                market_context_json
            FROM strategy_trades
            WHERE origin LIKE 'AI_ORIGIN_%'
              AND status = 'CLOSED'
              AND pnl_percent IS NOT NULL
              AND market_context_json IS NOT NULL
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
    skipped_no_field = 0
    for row in rows:
        try:
            context = json.loads(row["market_context_json"])
        except (TypeError, ValueError):
            skipped_no_field += 1
            continue
        counts = context.get("same_direction_entries_today") or {}
        signal = row["signal"]
        if signal not in counts:
            # Field predates this trade, or this signal wasn't tracked -- unknown,
            # not zero. Excluded rather than defaulted.
            skipped_no_field += 1
            continue

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
            same_direction_count=int(counts[signal]),
            pnl_percent=float(row["pnl_percent"]),
            mfe_percent=mfe,
            mae_percent=mae,
            is_win=(row["result"] == "WIN"),
        ))

    if skipped_no_field:
        logger.info(
            "Excluded %d closed AI Origination trade(s) with no same_direction_entries_today "
            "value for their own signal (field predates the trade, or malformed JSON).",
            skipped_no_field,
        )
    return entries


def _bootstrap_mean_diff(a: list[float], b: list[float], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    """90% CI on mean(a) - mean(b) via independent resampling of each group."""
    rng = random.Random(20260815)
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


def _bootstrap_compare(label: str, a: list[Entry], b: list[Entry]) -> None:
    if len(a) < 2 or len(b) < 2:
        logger.info("%s: too few observations for a bootstrap comparison (n=%d vs n=%d).", label, len(a), len(b))
        return
    lo, hi = _bootstrap_mean_diff([e.pnl_percent for e in a], [e.pnl_percent for e in b])
    verdict = (
        "reliably WORSE" if hi < 0
        else "reliably BETTER" if lo > 0
        else "no reliable difference at this sample size"
    )
    logger.info(
        "%s: bootstrap 90%% CI on mean_pnl diff = [%+.2f, %+.2f] -> first group is %s  (n=%d vs n=%d)",
        label, lo, hi, verdict, len(a), len(b),
    )


def run_buckets(entries: list[Entry]) -> None:
    logger.info("=" * 100)
    logger.info("OUTCOME BY same_direction_entries_today AT ENTRY")
    logger.info("=" * 100)
    buckets: dict[str, list[Entry]] = {label: [] for label in BUCKET_LABELS}
    for entry in entries:
        buckets[_bucket_label(entry.same_direction_count)].append(entry)
    for label in BUCKET_LABELS:
        _report_bucket(label, buckets[label])

    logger.info("-" * 100)
    logger.info("Would tightening the gate to >=1 (instead of the shipped >=2) have been justified?")
    _bootstrap_compare("  bucket 0 vs bucket 1", buckets["0"], buckets["1"])

    logger.info("-" * 100)
    logger.info("Does the data support the threshold actually shipped (block at >=2)?")
    below_gate = buckets["0"] + buckets["1"]
    at_or_above_gate = buckets["2"] + buckets["3+"]
    _bootstrap_compare("  <2 (0+1 combined) vs >=2", below_gate, at_or_above_gate)

    thin = [label for label in BUCKET_LABELS if len(buckets[label]) < MIN_BUCKET_LIVE]
    if thin:
        logger.info("-" * 100)
        logger.info(
            "Buckets below the %d-observation trust minimum: %s. Since the >=2 gate has blocked new "
            "entries there since 11 Aug 2026, expect these to stay thin going forward -- only "
            "pre-gate history can ever populate them further.",
            MIN_BUCKET_LIVE, ", ".join(thin),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    args = parser.parse_args()

    entries = _load_entries(args.db)
    logger.info(
        "Loaded %d closed AI Origination trade(s) with a recorded same_direction_entries_today value.",
        len(entries),
    )
    if not entries:
        logger.error(
            "No usable closed AI Origination entries found. Either data/trading.db has no AI "
            "Origination history yet, or this sandbox has no real data at all (expected here -- "
            "see CLAUDE.md). Run this on the machine with real trade history."
        )
        return 0

    run_buckets(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
