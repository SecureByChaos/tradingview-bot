"""Backfill 1-minute FUTIDX candles for each enabled index's current-month
futures contract, and keep them upserted as this is re-run.

WHY THIS EXISTS
---------------
Index tokens (BANKNIFTY/NIFTY spot) always report volume 0 on SmartAPI --
there is no such thing as "spot volume," an index isn't a traded instrument.
VWAP needs the current-month FUTIDX contract instead, which is a real traded
instrument with real volume. This is the only path to backtesting BNV5.1 and
BNV6, which depend on VWAP and could not be assessed in week 1 for exactly
this reason (see docs/ai-origination-roadmap.md).

WHY THIS MUST START NOW AND RUN REPEATEDLY
-------------------------------------------
Nothing has been collecting FUTIDX candles before this script existed --
there is no history to backfill before today, unlike the option-candle pull
this mirrors. SmartAPI serves roughly 28 days of 1-minute history per
request, so the earliest available history shrinks by a day for every day
this isn't running. And unlike a fixed past option-expiry window pulled
once, a futures contract itself rolls to a new expiry every month --
re-running this script always re-resolves "the current month" from the
instrument master (same nearest-unexpired-first logic
app/option_finder.py already uses for options), so it automatically picks
up the new contract after a rollover instead of continuing to poll an
expired one.

Run this on a recurring schedule (daily is plenty -- the 1-minute history
window covers weeks) to keep the archive current. Safe to re-run any time
outside market hours; storage is an idempotent upsert
(app.market_data.store_bars), so overlapping ranges get re-written, not
duplicated.

USAGE
-----
    python -m scripts.backfill_futures
    python -m scripts.backfill_futures --days 28   # override history window
    python -m scripts.backfill_futures --dry-run    # resolve contracts only, no API calls

Storage: reuses the existing candles table (app.market_data /
app.db_models.Candle), keyed under "<INDEX_SYMBOL>_FUT" as its index_symbol
-- e.g. "BANKNIFTY_FUT" -- so existing load_bars/resample tooling works
unmodified. Volume on these rows is real (unlike the spot index rows), which
is the entire point.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.db_models import IndexConfig
from app.market_data import ONE_MINUTE, parse_smartapi_row, store_bars
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIClient
from app.time_utils import IST, utc_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_futures")

# Matches backfill_candles.py's own 1-minute ceiling -- SmartAPI serves
# roughly this much 1-minute history per request; asking for more just gets
# silently truncated rather than erroring, so there's no benefit to a wider
# default.
DEFAULT_HISTORY_DAYS = 28
CHUNK_DAYS = 25

FUT_SUFFIX = "_FUT"


def _load_scrip_master(settings: Any) -> pd.DataFrame:
    cache_path = settings.instrument_cache_path
    if not cache_path.exists():
        raise SystemExit(
            f"Instrument master cache not found at {cache_path}. Start the app once "
            "so it downloads, or run OptionFinder._load_instruments manually."
        )
    frame = pd.DataFrame(json.loads(cache_path.read_text(encoding="utf-8")))
    required = {"exch_seg", "instrumenttype", "name", "symbol", "expiry", "token"}
    missing = required - set(frame.columns)
    if missing:
        raise SystemExit(f"Instrument master missing columns: {', '.join(sorted(missing))}")
    return frame


def _current_month_future(scrip: pd.DataFrame, index: IndexConfig) -> dict[str, Any] | None:
    """Nearest-expiry FUTIDX contract for this index's underlying, or None if
    the instrument master has none. Nearest-unexpired-first, the same
    resolution rule app/option_finder.py already uses for options, so
    "current month" always means whatever the exchange itself would call
    current rather than something inferred from the calendar."""
    frame = scrip[
        (scrip["exch_seg"] == index.exchange_segment)
        & (scrip["instrumenttype"] == "FUTIDX")
        & (scrip["name"].astype(str).str.upper() == index.instrument_name.upper())
    ].copy()
    if frame.empty:
        return None
    frame["expiry_dt"] = pd.to_datetime(frame["expiry"], format="%d%b%Y", errors="coerce").dt.date
    today = datetime.now(IST).date()
    frame = frame[frame["expiry_dt"].notna() & (frame["expiry_dt"] >= today)]
    if frame.empty:
        return None
    row = frame.sort_values("expiry_dt").iloc[0]
    return {
        "tradingsymbol": str(row["symbol"]),
        "symboltoken": str(row["token"]),
        "exchange": str(row["exch_seg"]),
        "expiry": str(row["expiry"]),
    }


def _fetch_range(
    smartapi: SmartAPIClient, contract: dict[str, Any], start: datetime, end: datetime, chunk_days: int
) -> list:
    bars = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        for attempt in range(3):
            try:
                rows = smartapi.get_candles(
                    exchange=contract["exchange"],
                    symboltoken=contract["symboltoken"],
                    interval=ONE_MINUTE,
                    from_dt=cursor.strftime("%Y-%m-%d %H:%M"),
                    to_dt=chunk_end.strftime("%Y-%m-%d %H:%M"),
                )
                bars.extend(parse_smartapi_row(row) for row in rows)
                logger.info(
                    "  %s %s..%s -> %s rows", contract["tradingsymbol"], cursor.date(), chunk_end.date(), len(rows)
                )
                break
            except Exception as exc:
                if attempt == 2:
                    logger.warning("  %s %s failed after 3 attempts: %s", contract["tradingsymbol"], cursor.date(), exc)
                else:
                    logger.info("  retry %s after %s", attempt + 1, exc)
        cursor = chunk_end
    return bars


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS, help="Days of 1-minute history to pull")
    parser.add_argument("--dry-run", action="store_true", help="Resolve current-month contracts only, no API calls")
    parser.add_argument(
        "--during-market-hours", action="store_true",
        help=(
            "Allow fetching while NSE is open despite sharing the SmartAPI account's rate-limit "
            "budget with live origination. Off by default -- same reasoning as "
            "backfill_candles.py and pull_option_candles.py."
        ),
    )
    args = parser.parse_args()

    if not args.dry_run and not args.during_market_hours and check_market_hours(utc_now()) is None:
        # check_market_hours returns None specifically when the timestamp IS
        # within NSE trading hours -- see app/signal_validation.py.
        logger.error(
            "Refusing to fetch during NSE market hours: this script authenticates its own SmartAPI "
            "session and can exhaust the account's shared rate-limit budget, degrading live "
            "origination's own candle refresh. Re-run outside market hours, use --dry-run "
            "(no SmartAPI calls), or pass --during-market-hours to override."
        )
        return 1

    settings = get_settings()
    scrip = _load_scrip_master(settings)

    with SessionLocal() as session:
        indexes = [index for index in session.scalars(select(IndexConfig)) if index.enabled]
    if not indexes:
        logger.error("No enabled indexes configured.")
        return 1

    contracts: dict[str, dict[str, Any]] = {}
    for index in indexes:
        contract = _current_month_future(scrip, index)
        if contract is None:
            logger.warning("%s: no FUTIDX contract found in instrument master", index.symbol)
            continue
        contracts[index.symbol] = contract
        logger.info(
            "%s -> current-month future %s (expiry %s)", index.symbol, contract["tradingsymbol"], contract["expiry"]
        )

    if not contracts:
        logger.error("No FUTIDX contracts resolved for any enabled index.")
        return 1

    if args.dry_run:
        logger.info("Dry run -- %s contract(s) resolved, no API calls made.", len(contracts))
        return 0

    smartapi = SmartAPIClient(settings)
    smartapi.authenticate()
    now = datetime.now()

    with SessionLocal() as session:
        for index_symbol, contract in contracts.items():
            fut_symbol = f"{index_symbol}{FUT_SUFFIX}"
            logger.info(
                "Fetching %s (%s) 1-minute history (%s days)", fut_symbol, contract["tradingsymbol"], args.days
            )
            bars = _fetch_range(smartapi, contract, now - timedelta(days=args.days), now, CHUNK_DAYS)
            if not bars:
                logger.warning("%s: no candles returned", fut_symbol)
                continue
            written = store_bars(session, fut_symbol, ONE_MINUTE, bars)
            logger.info("%s -> %s rows stored", fut_symbol, written)

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
