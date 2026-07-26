"""Pull 1-minute historical candles for the option contracts AI Origination
actually traded, so the trailing-stop parameters can be tuned against the real
premium path instead of against MFE/MAE extremes alone.

WHY THIS IS TIME-CRITICAL
-------------------------
Angel One serves getCandleData only for instruments still present in the live
scrip master. An expired F&O contract drops out and its intraday history
becomes permanently unretrievable -- there is no archive to go back to. Most of
the 20-24 Jul dataset traded the 28-Jul expiry, so this must run BEFORE that
expiry. After it, this data does not exist anywhere.

WHAT IT GIVES YOU THAT MFE/MAE CANNOT
-------------------------------------
MFE/MAE are extremes without sequence. A trade that went +9%, fell back to +4%,
then ran to +18% has the same MFE as one that rose straight to +18% -- but a
trailing stop would have exited the first at +4% and carried the second to
target. Only the ordered premium path separates them, which is exactly what the
+8%/5% parameter choice rests on.

It also unlocks two-directional replay scoring: with premium paths for strikes
around the ones traded, a future prompt change can be scored on trades it WOULD
have taken, not only on trades production actually took.

USAGE
-----
    python -m scripts.pull_option_candles --start 2026-07-20 --end 2026-07-24

    # dry run: show what would be fetched, make no API calls
    python -m scripts.pull_option_candles --start 2026-07-20 --end 2026-07-24 --dry-run

    # widen/narrow the strike band around each traded strike (default 2)
    python -m scripts.pull_option_candles --strike-band 3

Output: data/option_candles/<TRADINGSYMBOL>_<TOKEN>.csv, one file per contract,
columns timestamp_ist,open,high,low,close,volume. Existing files are skipped so
the script is safely resumable if it dies partway or hits a rate limit.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.db_models import StrategyTrade
from app.smartapi_client import SmartAPIClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pull_option_candles")

OUTPUT_DIR = Path("data/option_candles")
# Market hours with a small cushion either side. getCandleData returns nothing
# outside session hours anyway, but keeping the window tight reduces payload
# size and makes partial-day failures easier to spot.
SESSION_START = "09:15"
SESSION_END = "15:30"


def _traded_contracts(start: date, end: date) -> list[dict[str, Any]]:
    """Every distinct option contract AI Origination traded in the window."""
    with SessionLocal() as session:
        trades = list(
            session.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.origin.like("AI_ORIGIN_%"),
                )
            )
        )
    contracts: dict[str, dict[str, Any]] = {}
    for trade in trades:
        entry_date = trade.entry_time.date() if trade.entry_time else None
        if entry_date is None or entry_date < start or entry_date > end:
            continue
        contracts[trade.tradingsymbol] = {
            "tradingsymbol": trade.tradingsymbol,
            "symboltoken": trade.symboltoken,
            "exchange": trade.exchange,
            "index_symbol": trade.index_symbol,
            "strike": trade.strike,
            "option_type": trade.option_type,
            "expiry": trade.expiry,
        }
    return list(contracts.values())


def _load_scrip_master(settings: Any) -> pd.DataFrame:
    """Reuse the same cached instrument master OptionFinder already maintains,
    so the strike-band expansion resolves real tokens rather than guessing at
    symbol naming conventions."""
    cache_path = settings.instrument_cache_path
    if not cache_path.exists():
        raise SystemExit(
            f"Instrument master cache not found at {cache_path}. Start the app once "
            "so it downloads, or run OptionFinder._load_instruments manually."
        )
    frame = pd.DataFrame(json.loads(cache_path.read_text(encoding="utf-8")))
    frame["strike_normalized"] = pd.to_numeric(frame["strike"], errors="coerce")
    frame = frame[frame["strike_normalized"].notna()].copy()
    frame["strike_normalized"] = frame["strike_normalized"].apply(
        lambda value: value / 100 if value > 100000 else value
    )
    return frame


def _expand_strike_band(
    contracts: list[dict[str, Any]], scrip: pd.DataFrame, band: int, strike_intervals: dict[str, int]
) -> list[dict[str, Any]]:
    """Add +/-N strikes either side of each traded strike, both CE and PE.

    The neighbours are what make two-directional replay possible: to score a
    trade the enriched prompt would have taken but production skipped, you need
    the premium path of a contract that was never actually traded.
    """
    expanded: dict[str, dict[str, Any]] = {c["tradingsymbol"]: c for c in contracts}
    for contract in contracts:
        interval = strike_intervals.get(contract["index_symbol"], 100)
        for step in range(-band, band + 1):
            target_strike = contract["strike"] + (step * interval)
            for option_type in ("CE", "PE"):
                if step == 0 and option_type == contract["option_type"]:
                    continue
                matches = scrip[
                    (scrip["exch_seg"] == contract["exchange"])
                    & (scrip["expiry"].astype(str) == _angel_expiry(contract["expiry"]))
                    & (scrip["strike_normalized"] == target_strike)
                    & (scrip["symbol"].astype(str).str.upper().str.endswith(option_type))
                ]
                if matches.empty:
                    continue
                row = matches.iloc[0]
                symbol = str(row["symbol"])
                if symbol in expanded:
                    continue
                expanded[symbol] = {
                    "tradingsymbol": symbol,
                    "symboltoken": str(row["token"]),
                    "exchange": contract["exchange"],
                    "index_symbol": contract["index_symbol"],
                    "strike": int(target_strike),
                    "option_type": option_type,
                    "expiry": contract["expiry"],
                    "neighbour": True,
                }
    return list(expanded.values())


def _angel_expiry(expiry: str) -> str:
    """StrategyTrade.expiry is stored however OptionFinder read it; the scrip
    master uses DDMMMYYYY uppercase. Normalize both ways defensively."""
    raw = str(expiry).strip()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d%b%Y").upper()
        except ValueError:
            continue
    return raw.upper()


def _trading_days(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2026-07-20", help="First trade date to include (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-07-24", help="Last trade date to include (YYYY-MM-DD)")
    parser.add_argument("--strike-band", type=int, default=2, help="Strikes either side of each traded strike")
    parser.add_argument("--dry-run", action="store_true", help="List contracts without calling the API")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    settings = get_settings()
    contracts = _traded_contracts(start, end)
    if not contracts:
        logger.error("No AI Origination trades found between %s and %s", start, end)
        return 1
    logger.info("Found %s distinct traded contracts", len(contracts))

    strike_intervals: dict[str, int] = {}
    with SessionLocal() as session:
        from app.db_models import IndexConfig

        for index in session.scalars(select(IndexConfig)):
            strike_intervals[index.symbol] = index.strike_interval or 100

    scrip = _load_scrip_master(settings)
    all_contracts = _expand_strike_band(contracts, scrip, args.strike_band, strike_intervals)
    logger.info(
        "Expanded to %s contracts with a +/-%s strike band (%s traded, %s neighbours)",
        len(all_contracts), args.strike_band, len(contracts), len(all_contracts) - len(contracts),
    )

    days = _trading_days(start, end)
    logger.info("Trading days in range: %s", ", ".join(d.isoformat() for d in days))

    if args.dry_run:
        for contract in sorted(all_contracts, key=lambda c: c["tradingsymbol"]):
            logger.info(
                "  %s token=%s strike=%s %s%s",
                contract["tradingsymbol"], contract["symboltoken"], contract["strike"],
                contract["option_type"], " (neighbour)" if contract.get("neighbour") else " (TRADED)",
            )
        logger.info("Dry run -- %s contracts x %s days = %s API calls would be made",
                    len(all_contracts), len(days), len(all_contracts) * len(days))
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    smartapi = SmartAPIClient(settings)
    smartapi.authenticate()

    ok, skipped, failed = 0, 0, 0
    for contract in sorted(all_contracts, key=lambda c: c["tradingsymbol"]):
        out_path = OUTPUT_DIR / f"{contract['tradingsymbol']}_{contract['symboltoken']}.csv"
        if out_path.exists():
            skipped += 1
            continue
        rows: list[list[Any]] = []
        contract_failed = False
        for day in days:
            # One call per contract per day. Fetching the whole range in a
            # single call would be fewer requests, but a single failure would
            # then lose every day rather than one -- and with an immovable
            # expiry deadline, resumability matters more than call count.
            for attempt in range(3):
                try:
                    rows.extend(
                        smartapi.get_candles(
                            exchange=contract["exchange"],
                            symboltoken=contract["symboltoken"],
                            interval="ONE_MINUTE",
                            from_dt=f"{day.isoformat()} {SESSION_START}",
                            to_dt=f"{day.isoformat()} {SESSION_END}",
                        )
                    )
                    break
                except Exception as exc:
                    if attempt == 2:
                        logger.warning("  %s %s failed after 3 attempts: %s", contract["tradingsymbol"], day, exc)
                        contract_failed = True
                    else:
                        logger.info("  %s %s attempt %s failed (%s), retrying", contract["tradingsymbol"], day, attempt + 1, exc)
        if not rows:
            failed += 1
            logger.warning("No candles returned for %s -- contract may already have expired", contract["tradingsymbol"])
            continue
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp_ist", "open", "high", "low", "close", "volume"])
            writer.writerows(rows)
        ok += 1
        logger.info(
            "%s -> %s rows%s%s",
            contract["tradingsymbol"], len(rows),
            " (TRADED)" if not contract.get("neighbour") else "",
            " [PARTIAL - some days failed]" if contract_failed else "",
        )

    logger.info("Done. %s written, %s already present, %s returned nothing.", ok, skipped, failed)
    if failed:
        logger.warning(
            "Contracts returning nothing are most likely already expired and unrecoverable. "
            "Check whether they were traded contracts (critical) or neighbours (nice-to-have)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
