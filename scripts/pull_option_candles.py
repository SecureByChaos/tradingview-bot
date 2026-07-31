"""Pull 1-minute historical candles for the option contracts AI Origination
actually traded, so the trailing-stop parameters can be tuned against the real
premium path instead of against MFE/MAE extremes alone.

WHY THIS IS TIME-CRITICAL, EVERY TIME YOU RUN IT
-------------------------------------------------
Angel One serves getCandleData only for instruments still present in the live
scrip master. An expired F&O contract drops out and its intraday history
becomes permanently unretrievable -- there is no archive to go back to. This
is not a one-time deadline: it recurs every expiry cycle. The original 20-24
Jul archive was deadline-bound by the 28-Jul expiry; whatever contracts are
trading now have their own, later deadline. --start/--end have no default for
exactly this reason -- there is no date range that stays correct to fall back
on, and a stale default would silently go looking for trades in an
already-expired window instead of failing loudly.

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
    # --start/--end must cover dates with actual AI Origination trades already
    # recorded for the contract(s) you want archived -- this pulls candles for
    # contracts that were traded, not a symbol/expiry you name directly.
    python -m scripts.pull_option_candles --start 2026-08-03 --end 2026-08-07

    # dry run: show what would be fetched, make no API calls
    python -m scripts.pull_option_candles --start 2026-08-03 --end 2026-08-07 --dry-run

    # widen/narrow the strike band around each traded strike (default 2)
    python -m scripts.pull_option_candles --start 2026-08-03 --end 2026-08-07 --strike-band 3

    # EXPIRY MODE -- archive a named expiry's ATM band regardless of what was
    # traded. Needed for premium calibration, which requires a DTE RANGE: Bank
    # Nifty trades a ~27 DTE monthly while the archive only covers 0-10 DTE, so
    # every Bank Nifty coefficient is currently extrapolated. Trade-driven
    # pulling cannot fix that, because it only ever reaches the DTE buckets
    # that happened to be traded -- and with the 5-DTE floor now in place there
    # may be no Bank Nifty trades to drive a pull from at all.
    python -m scripts.pull_option_candles --start 2026-08-03 --end 2026-08-07 \
        --index BANKNIFTY --expiry 28AUG2026 --strike-band 4

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
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIClient
from app.time_utils import utc_now

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


def _latest_spot(index_symbol: str) -> float | None:
    """Most recent stored close for an index, used to locate ATM.

    Read from the candle store rather than SmartAPI on purpose: it needs no
    authentication, costs nothing against the shared rate-limit budget, and
    works in --dry-run. ATM only needs to be right to the nearest strike
    interval, so a close from the last session is entirely adequate.
    """
    from app.db_models import Candle

    with SessionLocal() as session:
        return session.scalar(
            select(Candle.close)
            .where(Candle.index_symbol == index_symbol)
            .order_by(Candle.ts_ist.desc())
            .limit(1)
        )


def _expiry_contracts(
    index_symbol: str, expiry: str, band: int, scrip: pd.DataFrame,
    strike_intervals: dict[str, int], exchange: str, spot_override: float | None = None,
) -> list[dict[str, Any]]:
    """Contracts around ATM for a NAMED expiry, independent of what was traded.

    The trade-driven path above can only archive contracts AI Origination
    actually took. That is the wrong tool for calibration, for two reasons:

      * The premium coefficients need a DTE RANGE. Bank Nifty currently trades
        a ~27 DTE monthly while the archive only covers 0-10 DTE, so every Bank
        Nifty figure is extrapolated -- and no amount of trade-driven archiving
        fixes that if the trades keep landing in the same bucket.
      * With the 5-DTE floor now in place, Bank Nifty may not trade at all on
        most days, so there may be no trades to drive a pull from.
    """
    interval = strike_intervals.get(index_symbol, 100)
    spot = spot_override if spot_override else _latest_spot(index_symbol)
    if not spot:
        raise SystemExit(
            f"No stored candles for {index_symbol} to locate ATM, and no --spot given. "
            "Run scripts/backfill_candles.py first, or pass --spot."
        )
    atm = round(float(spot) / interval) * interval
    logger.info(
        "%s: spot ~%.2f -> ATM %s, band +/-%s x %s points",
        index_symbol, float(spot), atm, band, interval,
    )

    wanted_expiry = _angel_expiry(expiry)
    index_options = scrip[
        (scrip["exch_seg"] == exchange)
        & (scrip["name"].astype(str).str.upper() == index_symbol.upper())
        & (scrip["symbol"].astype(str).str.upper().str.endswith(("CE", "PE")))
    ]
    if index_options.empty:
        raise SystemExit(
            f"No {index_symbol} options at all on {exchange} in the instrument master. "
            "Check --index and --exchange."
        )

    on_expiry = index_options[index_options["expiry"].astype(str).str.upper() == wanted_expiry]
    if on_expiry.empty:
        # Guessing an expiry date is the single most likely way to use this
        # mode wrong, so answer the question rather than restating it.
        available = sorted(
            {str(e).upper() for e in index_options["expiry"].dropna().unique()},
            key=lambda text: (_parse_expiry_sort_key(text), text),
        )
        raise SystemExit(
            f"No {index_symbol} contracts on {exchange} for expiry {wanted_expiry}.\n"
            f"Available expiries in the instrument master:\n  "
            + "\n  ".join(available[:20])
            + ("\n  ..." if len(available) > 20 else "")
        )

    contracts: dict[str, dict[str, Any]] = {}
    for step in range(-band, band + 1):
        target_strike = atm + (step * interval)
        for option_type in ("CE", "PE"):
            # Tolerance rather than float equality: strike_normalized is a
            # divided float, so an exact == against an int can miss.
            matches = on_expiry[
                ((on_expiry["strike_normalized"] - target_strike).abs() < 0.5)
                & (on_expiry["symbol"].astype(str).str.upper().str.endswith(option_type))
            ]
            if matches.empty:
                continue
            row = matches.iloc[0]
            symbol = str(row["symbol"])
            contracts[symbol] = {
                "tradingsymbol": symbol,
                "symboltoken": str(row["token"]),
                "exchange": exchange,
                "index_symbol": index_symbol,
                "strike": int(target_strike),
                "option_type": option_type,
                "expiry": expiry,
                # Not traded -- selected by proximity to ATM. Labelled so the
                # dry-run listing doesn't claim these came from trade history.
                "atm_band": True,
            }
    if not contracts:
        listed = sorted({float(s) for s in on_expiry["strike_normalized"].dropna().unique()})
        nearest = min(listed, key=lambda s: abs(s - atm)) if listed else None
        raise SystemExit(
            f"{index_symbol} {wanted_expiry} exists but has no strikes within the "
            f"+/-{band} band around ATM {atm}. Nearest listed strike: {nearest}. "
            "The strike interval may be wrong for this index -- check Settings > Instruments."
        )
    return list(contracts.values())


def _parse_expiry_sort_key(text: str) -> datetime:
    parsed = _parse_expiry_maybe(text)
    return parsed if parsed else datetime.max


def _parse_expiry_maybe(text: str) -> datetime | None:
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(text).strip().upper(), fmt)
        except ValueError:
            continue
    return None


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
    # No defaults, deliberately -- see the module docstring's "WHY THIS IS
    # TIME-CRITICAL" section. A hardcoded fallback window would always be an
    # already-expired one sooner or later, and would fail by silently
    # returning nothing rather than telling the caller their dates are wrong.
    parser.add_argument("--start", required=True, help="First session date to fetch (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Last session date to fetch (YYYY-MM-DD)")
    parser.add_argument("--strike-band", type=int, default=2, help="Strikes either side of ATM / each traded strike")
    parser.add_argument(
        "--expiry", default="",
        help=(
            "Archive a NAMED expiry's ATM band instead of whatever was traded, e.g. "
            "28AUG2026. Needed for calibration: the coefficients require a DTE range, and "
            "the trade-driven path can only reach DTE buckets that were actually traded."
        ),
    )
    parser.add_argument("--index", default="", help="Index symbol, required with --expiry (e.g. BANKNIFTY)")
    parser.add_argument("--exchange", default="NFO", help="Option exchange segment for --expiry mode")
    parser.add_argument(
        "--spot", type=float, default=None,
        help="Override the spot used to locate ATM in --expiry mode. Defaults to the latest stored candle close.",
    )
    parser.add_argument("--dry-run", action="store_true", help="List contracts without calling the API")
    parser.add_argument(
        "--during-market-hours", action="store_true",
        help=(
            "Allow pulling while NSE is open despite sharing the SmartAPI account's rate-limit "
            "budget with live origination. Off by default -- even an urgent pre-expiry pull "
            "shouldn't silently degrade live trading's own data quality; use this only when "
            "the deadline genuinely can't wait until after close."
        ),
    )
    args = parser.parse_args()

    if not args.dry_run and not args.during_market_hours and check_market_hours(utc_now()) is None:
        # check_market_hours returns None specifically when the timestamp IS
        # within NSE trading hours -- see app/signal_validation.py.
        logger.error(
            "Refusing to pull during NSE market hours: this script authenticates its own SmartAPI "
            "session and can exhaust the account's shared rate-limit budget, degrading live "
            "origination's own candle refresh. Re-run outside market hours, use --dry-run "
            "(no SmartAPI calls), or pass --during-market-hours if the expiry deadline can't wait."
        )
        return 1

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    if args.expiry and not args.index:
        logger.error("--expiry requires --index (e.g. --index BANKNIFTY)")
        return 1

    settings = get_settings()
    strike_intervals: dict[str, int] = {}
    with SessionLocal() as session:
        from app.db_models import IndexConfig

        for index in session.scalars(select(IndexConfig)):
            strike_intervals[index.symbol] = index.strike_interval or 100

    scrip = _load_scrip_master(settings)

    if args.expiry:
        all_contracts = _expiry_contracts(
            args.index.upper(), args.expiry, args.strike_band, scrip,
            strike_intervals, args.exchange.upper(), args.spot,
        )
        logger.info(
            "Expiry mode: %s contracts for %s %s (+/-%s strikes around ATM)",
            len(all_contracts), args.index.upper(), args.expiry, args.strike_band,
        )
    else:
        contracts = _traded_contracts(start, end)
        if not contracts:
            logger.error(
                "No AI Origination trades found between %s and %s. If you meant to archive a "
                "specific expiry regardless of what was traded, use --expiry/--index.",
                start, end,
            )
            return 1
        logger.info("Found %s distinct traded contracts", len(contracts))
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
                contract["option_type"],
                " (ATM band)" if contract.get("atm_band")
                else (" (neighbour)" if contract.get("neighbour") else " (TRADED)"),
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
