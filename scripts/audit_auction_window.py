"""What do the stored index candles actually look like during the CAS window?

Deliverable 1 of the CAS impact assessment. Answers, from data rather than from
the circular, whether 15:15-15:30 index bars are frozen, gapped, or normal --
and whether spot and futures diverge in that window as NSE described.

THE FUTURES COMPARISON IS THE ACTUAL TEST
-----------------------------------------
A flat index in isolation is ambiguous: a quiet fifteen minutes looks the same
as a frozen feed. Index FUTURES are not auctioned and keep trading
continuously, so they are the control. If spot goes flat while futures keep
moving in the same minutes, the freeze is structural rather than a quiet
market, and that is exactly the divergence NSE confirmed on 3 Aug. This is why
the script wants BANKNIFTY_FUT / NIFTY_FUT loaded too, and says so when they
are missing rather than silently reporting half the picture.

WHAT TO DO WITH THE ANSWER
--------------------------
If the window is frozen, the exposure is NOT order execution -- index options
trade continuously to 15:40 and every exit in this system prices off option
premium, not spot. The exposure is that fifteen flat bars per session enter the
candle store and quietly deflate ATR, decay ADX, and dilute any return computed
over a window that includes them.

Usage:
    python -m scripts.audit_auction_window --db data/trading.db
    python -m scripts.audit_auction_window --db data/trading.db --from 2026-08-03
    python -m scripts.audit_auction_window --db data/trading.db --show-bars
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

from app.market_hours import (
    AUCTION_WINDOW_END,
    AUCTION_WINDOW_START,
    CAS_EFFECTIVE_DATE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("audit_auction_window")

# Widened either side of the auction so the transition is visible rather than
# just its interior -- the interesting question is whether behaviour CHANGES at
# 15:15, which needs the minutes before it for contrast.
AUDIT_START = "15:05"
AUDIT_END = "15:45"


def _load(db_path: str, table: str, from_date: str | None) -> dict[tuple[str, str], list[tuple]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            f"""
            SELECT index_symbol, ts_ist, open, high, low, close, volume
            FROM {table}
            WHERE interval = 'ONE_MINUTE'
              AND time(ts_ist) BETWEEN ? AND ?
              {"AND date(ts_ist) >= ?" if from_date else ""}
            ORDER BY index_symbol, ts_ist
            """,
            (AUDIT_START + ":00", AUDIT_END + ":00", *( (from_date,) if from_date else () )),
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for symbol, ts_raw, open_, high, low, close, volume in rows:
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
        grouped[(str(symbol).upper(), ts.date().isoformat())].append(
            (ts, float(open_), float(high), float(low), float(close), float(volume or 0))
        )
    return grouped


def _describe(bars: list[tuple]) -> tuple[int, int, float, bool]:
    """(bar count, distinct closes, total range, is_frozen) for a set of bars."""
    if not bars:
        return 0, 0, 0.0, False
    closes = [bar[4] for bar in bars]
    highs = [bar[2] for bar in bars]
    lows = [bar[3] for bar in bars]
    distinct = len(set(closes))
    span = max(highs) - min(lows)
    # "Frozen" means the index did not move at all across the window. One
    # distinct close over fifteen bars is not a quiet market, it is a feed that
    # is not updating.
    return len(bars), distinct, span, distinct <= 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--table", default="candles")
    parser.add_argument("--from", dest="from_date", default=None, help="Only sessions on/after YYYY-MM-DD")
    parser.add_argument("--show-bars", action="store_true", help="Print every bar in the window")
    args = parser.parse_args()

    grouped = _load(args.db, args.table, args.from_date)
    if not grouped:
        logger.error(
            "No ONE_MINUTE bars between %s and %s in %s. Note the table is 'candles' with an "
            "'index_symbol' column -- there is no 'index_candle'/'index_name'.",
            AUDIT_START, AUDIT_END, args.db,
        )
        return 1

    sessions = sorted({day for _, day in grouped})
    logger.info("=" * 86)
    logger.info("Index behaviour around the Closing Auction Session (%s-%s IST)",
                AUCTION_WINDOW_START.strftime("%H:%M"), AUCTION_WINDOW_END.strftime("%H:%M"))
    logger.info("CAS effective %s. Sessions found: %s..%s", CAS_EFFECTIVE_DATE, sessions[0], sessions[-1])
    logger.info("=" * 86)

    frozen_by_day: dict[str, list[str]] = defaultdict(list)
    for (symbol, day), bars in sorted(grouped.items()):
        before = [b for b in bars if b[0].time() < AUCTION_WINDOW_START]
        during = [b for b in bars if AUCTION_WINDOW_START <= b[0].time() < AUCTION_WINDOW_END]
        after = [b for b in bars if b[0].time() >= AUCTION_WINDOW_END]

        n_b, d_b, span_b, _ = _describe(before)
        n_d, d_d, span_d, frozen = _describe(during)
        n_a, d_a, span_a, _ = _describe(after)

        post_cas = datetime.fromisoformat(day).date() >= CAS_EFFECTIVE_DATE
        logger.info(
            "%-14s %s %s  before[%2db %2dv %6.1fpts]  DURING[%2db %2dv %6.1fpts]%s  after[%2db %2dv %6.1fpts]",
            symbol, day, "CAS" if post_cas else "pre",
            n_b, d_b, span_b, n_d, d_d, span_d,
            "  <-- FROZEN" if frozen and n_d > 1 else "",
            n_a, d_a, span_a,
        )
        if frozen and n_d > 1:
            frozen_by_day[day].append(symbol)
        if args.show_bars:
            for ts, open_, high, low, close, volume in bars:
                logger.info("      %s  O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
                            ts.strftime("%H:%M"), open_, high, low, close, volume)

    logger.info("=" * 86)
    logger.info("SPOT vs FUTURES during the window (futures are NOT auctioned -- they are the control):")
    any_pair = False
    for day in sessions:
        for spot_symbol in ("NIFTY", "BANKNIFTY"):
            spot = grouped.get((spot_symbol, day))
            futures = grouped.get((f"{spot_symbol}_FUT", day))
            if not spot or not futures:
                continue
            any_pair = True
            _, spot_distinct, spot_span, spot_frozen = _describe(
                [b for b in spot if AUCTION_WINDOW_START <= b[0].time() < AUCTION_WINDOW_END]
            )
            _, fut_distinct, fut_span, fut_frozen = _describe(
                [b for b in futures if AUCTION_WINDOW_START <= b[0].time() < AUCTION_WINDOW_END]
            )
            verdict = (
                "STRUCTURAL FREEZE (spot flat, futures moving)"
                if spot_frozen and not fut_frozen
                else "both flat -- genuinely quiet, or both feeds stalled"
                if spot_frozen and fut_frozen
                else "spot still moving -- no freeze observed"
            )
            logger.info(
                "  %-10s %s  spot %2d distinct / %5.1f pts  vs  futures %2d distinct / %5.1f pts  -> %s",
                spot_symbol, day, spot_distinct, spot_span, fut_distinct, fut_span, verdict,
            )
    if not any_pair:
        logger.warning(
            "  No futures bars stored for these sessions, so the control is missing and a flat "
            "spot cannot be distinguished from a quiet market. Archive them first: "
            "scripts/backfill_futures.py (stored under '<INDEX>_FUT' in the same table)."
        )

    logger.info("=" * 86)
    if frozen_by_day:
        logger.warning("Frozen index windows found on %s session(s):", len(frozen_by_day))
        for day, symbols in sorted(frozen_by_day.items()):
            logger.warning("  %s: %s", day, ", ".join(symbols))
        logger.warning(
            "These bars are not wrong data to discard -- they are what the exchange publishes. "
            "The problem is that they enter indicator windows as though they were quiet trading: "
            "ATR deflates, ADX decays, and any return over a window containing them is diluted. "
            "Nothing errors and nothing looks wrong on a chart."
        )
        logger.info(
            "Live trading is NOT exposed: entries stop at 15:15 and every exit prices off option "
            "premium, which is continuously traded until 15:40. The exposure is the stored series "
            "that indicators and backtests read."
        )
    else:
        logger.info(
            "No frozen windows detected. If these sessions are post-%s, either the index feed "
            "does update through the auction or the stored bars come from a source that "
            "interpolates -- worth knowing which before relying on it.", CAS_EFFECTIVE_DATE,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
