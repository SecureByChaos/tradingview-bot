"""Backfill index candles and verify the two data paths agree.

Pulls two things:

  * ~2 years of 5-MINUTE candles per index, for offline backtesting. SmartAPI
    serves roughly 100 days of 5-minute per request, so 2 years is ~5 calls per
    index.
  * ~30 days of 1-MINUTE candles per index, which is the live path's source of
    truth (5/15/60-minute are resampled from it).

THE EQUIVALENCE CHECK IS THE POINT
----------------------------------
Those two paths are different sources. The backtest fits parameters on
5-minute bars served directly by the exchange; live computes 5-minute bars by
resampling its own 1-minute store. If those disagree -- bucket boundaries
misaligned, minutes missing, sessions starting on a different anchor -- then a
parameter fitted offline is being applied to subtly different data live, and
nothing downstream will announce the mismatch. It will just quietly not work.

So this script compares them across the ~30-day window where both exist, and
reports the disagreement rate before any fitted parameter is trusted.

Usage:
    python -m scripts.backfill_candles --years 2
    python -m scripts.backfill_candles --verify-only
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.db_models import IndexConfig
from app.market_data import (
    FIVE_MINUTE,
    ONE_MINUTE,
    Bar,
    load_bars,
    parse_smartapi_row,
    resample,
    store_bars,
)
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIClient
from app.time_utils import utc_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_candles")

# SmartAPI per-request limits. Chunk below them rather than at them -- a
# request that trips the limit returns an error, not a truncated result.
FIVE_MIN_CHUNK_DAYS = 90
ONE_MIN_CHUNK_DAYS = 25
ONE_MIN_MAX_HISTORY_DAYS = 28

# How closely resampled-from-1-minute must match exchange-served 5-minute.
# Not exact equality: the exchange and a resample can legitimately differ in
# the last decimal on a thin bar. A systematic disagreement is the concern.
PRICE_TOLERANCE = 0.05
MAX_ACCEPTABLE_MISMATCH_PERCENT = 1.0


def _fetch_range(
    smartapi: SmartAPIClient, index: IndexConfig, interval: str, start: datetime, end: datetime, chunk_days: int
) -> list[Bar]:
    bars: list[Bar] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        for attempt in range(3):
            try:
                rows = smartapi.get_candles(
                    exchange=index.spot_exchange,
                    symboltoken=index.spot_token,
                    interval=interval,
                    from_dt=cursor.strftime("%Y-%m-%d %H:%M"),
                    to_dt=chunk_end.strftime("%Y-%m-%d %H:%M"),
                )
                bars.extend(parse_smartapi_row(row) for row in rows)
                logger.info(
                    "  %s %s %s..%s -> %s rows",
                    index.symbol, interval, cursor.date(), chunk_end.date(), len(rows),
                )
                break
            except Exception as exc:
                if attempt == 2:
                    logger.warning("  %s %s %s failed after 3 attempts: %s", index.symbol, interval, cursor.date(), exc)
                else:
                    logger.info("  retry %s after %s", attempt + 1, exc)
        cursor = chunk_end
    return bars


def verify_equivalence(db, index_symbol: str) -> bool:
    """Compare resampled-from-1-minute against exchange-served 5-minute on the
    window where both exist. Returns True if they agree closely enough."""
    bars_1m = load_bars(db, index_symbol, ONE_MINUTE)
    direct_5m = load_bars(db, index_symbol, FIVE_MINUTE)
    if not bars_1m or not direct_5m:
        logger.warning("[VERIFY] %s: missing one of the two series, cannot compare", index_symbol)
        return False

    derived = {bar.ts_ist: bar for bar in resample(bars_1m, FIVE_MINUTE)}
    overlap = [bar for bar in direct_5m if bar.ts_ist in derived]
    if not overlap:
        logger.warning(
            "[VERIFY] %s: no overlapping timestamps at all -- this usually means the "
            "resample bucket anchor disagrees with the exchange's. Check _bucket_start.",
            index_symbol,
        )
        return False

    mismatches = []
    for exchange_bar in overlap:
        mine = derived[exchange_bar.ts_ist]
        for field in ("open", "high", "low", "close"):
            if abs(getattr(mine, field) - getattr(exchange_bar, field)) > PRICE_TOLERANCE:
                mismatches.append((exchange_bar.ts_ist, field, getattr(mine, field), getattr(exchange_bar, field)))
                break

    rate = len(mismatches) / len(overlap) * 100
    logger.info(
        "[VERIFY] %s: %s overlapping 5-min bars, %s mismatched (%.2f%%)",
        index_symbol, len(overlap), len(mismatches), rate,
    )
    for ts, field, mine_value, theirs in mismatches[:5]:
        logger.info("    %s %s: resampled=%.2f exchange=%.2f", ts, field, mine_value, theirs)

    if rate > MAX_ACCEPTABLE_MISMATCH_PERCENT:
        logger.error(
            "[VERIFY] %s: disagreement above %.1f%% -- do NOT trust parameters fitted on "
            "the 5-minute history until this is resolved.",
            index_symbol, MAX_ACCEPTABLE_MISMATCH_PERCENT,
        )
        return False
    logger.info("[VERIFY] %s: within tolerance.", index_symbol)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=float, default=2.0, help="Years of 5-minute history")
    parser.add_argument("--verify-only", action="store_true", help="Skip fetching, just compare stored series")
    parser.add_argument(
        "--during-market-hours", action="store_true",
        help=(
            "Allow fetching while NSE is open despite sharing the SmartAPI account's rate-limit "
            "budget with live origination. Off by default -- see the Friday 11:48-14:33 outage "
            "in the week 2 roadmap, where an analysis run overlapping market hours is the leading "
            "suspect for starving live origination's own candle refresh."
        ),
    )
    args = parser.parse_args()

    if not args.verify_only and not args.during_market_hours and check_market_hours(utc_now()) is None:
        # check_market_hours returns None specifically when the timestamp IS
        # within NSE trading hours (it returns a string only to explain why
        # it *isn't*) -- see app/signal_validation.py.
        logger.error(
            "Refusing to fetch during NSE market hours: this script authenticates its own SmartAPI "
            "session and can exhaust the account's shared rate-limit budget, degrading live "
            "origination's own candle refresh. Re-run outside market hours, use --verify-only "
            "(no SmartAPI calls), or pass --during-market-hours to override."
        )
        return 1

    settings = get_settings()
    with SessionLocal() as session:
        indexes = [
            index for index in session.scalars(select(IndexConfig))
            if index.enabled and index.spot_token
        ]
        if not indexes:
            logger.error("No enabled indexes with a spot token configured.")
            return 1
        logger.info("Indexes: %s", ", ".join(i.symbol for i in indexes))

        if not args.verify_only:
            smartapi = SmartAPIClient(settings)
            smartapi.authenticate()
            now = datetime.now()
            for index in indexes:
                logger.info("Fetching %s 5-minute history (%.1f years)", index.symbol, args.years)
                five_min = _fetch_range(
                    smartapi, index, FIVE_MINUTE,
                    now - timedelta(days=int(args.years * 365)), now, FIVE_MIN_CHUNK_DAYS,
                )
                store_bars(session, index.symbol, FIVE_MINUTE, five_min)

                logger.info("Fetching %s 1-minute history (%s days)", index.symbol, ONE_MIN_MAX_HISTORY_DAYS)
                one_min = _fetch_range(
                    smartapi, index, ONE_MINUTE,
                    now - timedelta(days=ONE_MIN_MAX_HISTORY_DAYS), now, ONE_MIN_CHUNK_DAYS,
                )
                store_bars(session, index.symbol, ONE_MINUTE, one_min)

        logger.info("--- Equivalence check ---")
        all_ok = all(verify_equivalence(session, index.symbol) for index in indexes)

    if not all_ok:
        logger.error("At least one index failed the equivalence check. See above.")
        return 2
    logger.info("All indexes passed. Backtest and live paths agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
