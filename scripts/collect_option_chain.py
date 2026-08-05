"""Manual driver and health check for the option-chain archive.

The collector normally runs itself as a scheduler job inside the live app. This
script exists for three things the scheduler cannot do:

  * PROBE the broker's actual response shape before trusting months of
    collection to it. Field names in getMarketData are not guaranteed stable
    across SDK versions, and an archive of null open interest is worth exactly
    nothing -- but it accumulates silently and looks fine until someone tries
    to use it. Run --probe once after deploying.
  * STATUS: how much has accumulated, over what period, and how large the file
    has grown. The archive is deliberately unbounded, so its size is the one
    thing about it that needs occasional eyes.
  * PLAN: what would be collected, without calling the broker at all, which is
    how to check the strike band and expiry selection are sane.

Usage:
    python -m scripts.collect_option_chain --status
    python -m scripts.collect_option_chain --plan
    python -m scripts.collect_option_chain --once --probe
    python -m scripts.collect_option_chain --once --force    # outside hours

NOTE ON --force: outside session hours the broker returns stale or empty
quotes. A forced run checks that the plumbing works; it does not collect data
worth keeping, and it will write a snapshot row anyway. Prefer --plan and
--probe for verification.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from app.config import get_settings
from app.option_chain import (
    _CYCLE_STAMP_PATH,
    _fetch_spots,
    build_contract_list,
    claim_cycle_slot,
    collect_once,
)
from app.option_chain_store import archive_status, resolve_path
from app.smartapi_client import SmartAPIClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("collect_option_chain")

# A session is 09:15-15:30, so 75 five-minute cycles, ~21 trading days a month.
SESSIONS_PER_MONTH = 21


def _report_status() -> int:
    status = archive_status()
    path = resolve_path()
    logger.info("Option-chain archive: %s", path)
    if not status.rows:
        logger.info("  Empty. Nothing collected yet.")
        logger.info(
            "  If the app has been running through a session and this is still empty, "
            "check for [CHAIN] lines in the app log -- the collector logs every skip "
            "with its reason."
        )
        return 0

    logger.info("  Rows:              %s", f"{status.rows:,}")
    logger.info("  Snapshots:         %s", f"{status.distinct_snapshots:,}")
    logger.info("  First:             %s", status.first_snapshot)
    logger.info("  Last:              %s", status.last_snapshot)
    logger.info("  File size:         %.1f MB", status.size_bytes / 1_048_576)

    if status.distinct_snapshots:
        per_snapshot = status.rows / status.distinct_snapshots
        bytes_per_row = status.size_bytes / status.rows
        logger.info("  Rows per snapshot: %.0f", per_snapshot)
        # Projection from measured bytes-per-row, not an assumed row size --
        # index overhead and SQLite page slack are both real and neither is
        # guessable to better than a factor.
        monthly_rows = per_snapshot * 75 * SESSIONS_PER_MONTH
        logger.info(
            "  Projected growth:  ~%.0f MB/month, ~%.1f GB/year at this width and cadence",
            monthly_rows * bytes_per_row / 1_048_576,
            monthly_rows * 12 * bytes_per_row / 1_073_741_824,
        )
        logger.info(
            "  This file is intentionally unbounded and is NOT the trading database. "
            "If the disk gets tight, move it with OPTION_CHAIN_DB_PATH or archive off "
            "older months -- do not reduce the strike band, which would make the "
            "history inconsistent with itself."
        )

    if status.last_snapshot:
        stale_days = (datetime.now() - status.last_snapshot).days
        if stale_days >= 3:
            logger.warning(
                "  Last snapshot is %s days old. The collector may have stopped -- "
                "check that OPTION_CHAIN_COLLECTION_ENABLED is on and the app is running.",
                stale_days,
            )
    return 0


def _report_plan(settings) -> int:
    """What would be collected, using no broker calls at all.

    Spot comes from the stored candles rather than a quote, so this stays
    runnable at any hour and costs nothing against the rate limit.
    """
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.db_models import Candle, IndexConfig

    spots: dict[str, float] = {}
    with SessionLocal() as session:
        for index in session.scalars(select(IndexConfig)):
            if not index.enabled or not index.spot_token:
                continue
            close = session.scalar(
                select(Candle.close)
                .where(Candle.index_symbol == index.symbol)
                .order_by(Candle.ts_ist.desc())
                .limit(1)
            )
            if close:
                spots[index.symbol] = float(close)

    if not spots:
        logger.error(
            "No stored candles to locate ATM. Run scripts/backfill_candles.py first -- "
            "the strike band is anchored on spot and cannot be planned without one."
        )
        return 1

    logger.info("Planning from last stored closes: %s", spots)
    contracts = build_contract_list(
        settings,
        strike_band=settings.option_chain_strike_band,
        expiry_count=settings.option_chain_expiry_count,
        spots=spots,
    )
    if not contracts:
        logger.error("No contracts selected. Check the instrument cache and index config.")
        return 1

    by_group: dict[tuple[str, str], list] = {}
    for contract in contracts:
        by_group.setdefault((contract.index_symbol, contract.expiry), []).append(contract)
    for (index_symbol, expiry), group in sorted(by_group.items()):
        strikes = sorted({c.strike for c in group})
        logger.info(
            "  %-10s %s: %s strikes (%.0f..%.0f), %s contracts",
            index_symbol, expiry, len(strikes), strikes[0], strikes[-1], len(group),
        )

    requests = -(-len(contracts) // 50)
    logger.info(
        "Total: %s contracts -> %s quote requests + 1 spot + %s greeks = ~%s per cycle",
        len(contracts), requests, len(by_group), requests + 1 + len(by_group),
    )
    logger.info(
        "At a %s-minute cadence that is ~%s rows per session and ~%s rows per month.",
        settings.option_chain_interval_minutes,
        f"{len(contracts) * 75:,}",
        f"{len(contracts) * 75 * SESSIONS_PER_MONTH:,}",
    )
    return 0


def _probe(smartapi: SmartAPIClient, settings) -> int:
    """Print one raw quote row so field names can be checked against the parser.

    This is the step that catches the failure mode worth catching: the
    collector reads open interest from "opnInterest" with "openInterest" as a
    fallback, and if the SDK ever reports it under a third name the archive
    fills with nulls and nothing complains.
    """
    spots = _fetch_spots(smartapi)
    if not spots:
        logger.error("Could not fetch spot prices; cannot select an ATM band to probe.")
        return 1
    logger.info("Spots: %s", spots)

    contracts = build_contract_list(
        settings,
        strike_band=1,
        expiry_count=1,
        spots=spots,
    )
    if not contracts:
        logger.error("No contracts selected to probe.")
        return 1

    sample = contracts[0]
    rows = smartapi.get_market_data("FULL", {sample.exchange: [sample.symboltoken]})
    if not rows:
        logger.error(
            "getMarketData(FULL) returned nothing for %s. Outside market hours this can "
            "be normal; during a session it is not.", sample.tradingsymbol,
        )
        return 1

    logger.info("Raw FULL quote for %s:", sample.tradingsymbol)
    logger.info("%s", json.dumps(rows[0], indent=2, default=str))
    present = set(rows[0])
    for label, names in (
        ("open interest", ("opnInterest", "openInterest")),
        ("volume", ("tradeVolume", "volume")),
        ("ltp", ("ltp",)),
    ):
        matched = [n for n in names if n in present]
        if matched:
            logger.info("  %-14s -> %s   OK", label, matched[0])
        else:
            logger.warning(
                "  %-14s -> NOT FOUND under %s. app/option_chain.py will store null for "
                "this field; add the real key there before collecting further.",
                label, " or ".join(names),
            )

    greeks = smartapi.get_option_greeks(sample.index_symbol, sample.expiry)
    if greeks:
        logger.info("optionGreek returned %s rows; sample:", len(greeks))
        logger.info("%s", json.dumps(greeks[0], indent=2, default=str))
    else:
        logger.warning(
            "optionGreek returned nothing. Implied volatility will be null across the "
            "archive; OI, volume, LTP and spot are unaffected."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="Archive size, coverage and growth projection")
    parser.add_argument("--plan", action="store_true", help="What would be collected; makes no broker calls")
    parser.add_argument("--once", action="store_true", help="Run a single collection cycle now")
    parser.add_argument("--probe", action="store_true", help="Print a raw quote row and check field names")
    parser.add_argument("--force", action="store_true", help="Ignore the market-hours gate (plumbing check only)")
    parser.add_argument(
        "--ignore-disabled", action="store_true",
        help="Collect even when OPTION_CHAIN_COLLECTION_ENABLED is false (deliberate manual runs only)",
    )
    args = parser.parse_args()

    if not any((args.status, args.plan, args.once, args.probe)):
        parser.print_help()
        return 1

    settings = get_settings()

    if args.status:
        return _report_status()
    if args.plan:
        return _report_plan(settings)

    # The kill switch has to work HERE too, not just in the app.
    #
    # OPTION_CHAIN_COLLECTION_ENABLED=false stops the in-app scheduler job,
    # because main.py declines to register it. It does nothing about a cron
    # entry or a systemd unit invoking this script -- and an external caller is
    # the leading suspect for the 4 Aug storm (2,890 rate-limit errors, far more
    # than a 5-minute in-app job could produce). Turning the flag off and
    # believing collection had stopped, while a crash-looping unit kept
    # authenticating every few seconds, is exactly the wrong thing to be
    # confident about.
    #
    # --ignore-disabled exists so a deliberate manual run is still possible
    # without editing config. Diagnostics above this point (--status, --plan,
    # --probe) are unaffected: they are cheap, manual, and are what you need
    # WHILE collection is switched off.
    if args.once and not settings.option_chain_collection_enabled and not args.ignore_disabled:
        logger.error(
            "OPTION_CHAIN_COLLECTION_ENABLED is false -- refusing to collect. If you did not "
            "run this by hand, something is invoking it on a schedule and the env flag alone "
            "will NOT stop that: find the caller (systemctl list-units | grep -i option; "
            "crontab -l; sudo crontab -l). For a deliberate one-off, pass --ignore-disabled."
        )
        return 1

    # The interval guard runs BEFORE authenticate, deliberately. Login is
    # itself a rate-limited endpoint on the same API key live trading uses, so
    # a re-invocation loop is a login storm regardless of whether the sweep
    # that follows would have been cheap. Authenticating first and checking
    # afterwards is what turned an over-frequent caller into 2,890
    # "exceeding access rate" errors in a single day on 4 Aug.
    if args.once and not claim_cycle_slot(settings.option_chain_interval_minutes):
        logger.error(
            "Refusing to run: the previous cycle was too recent. A refusal here means "
            "something ALREADY ran a cycle within the interval -- find out what before "
            "overriding. To override once the caller is understood: rm %s",
            _CYCLE_STAMP_PATH,
        )
        return 1

    smartapi = SmartAPIClient(settings)
    smartapi.authenticate()

    if args.probe:
        return _probe(smartapi, settings)

    # dedicated_client reflects whether SEPARATE credentials are configured,
    # not the fact that this script owns its own session. A second session on
    # the SAME API key shares the same quota, so claiming dedication here would
    # skip the yield-to-live-trading check on exactly the setup that most needs
    # it. This previously passed True unconditionally.
    is_dedicated = bool(settings.smartapi_analytics_api_key)
    stored = collect_once(
        smartapi, settings, force=args.force,
        dedicated_client=is_dedicated,
        # Already claimed above, before authenticating. Claiming twice would
        # have the second check see a stamp zero seconds old and refuse.
        claim_slot=False,
    )
    logger.info("Stored %s rows%s", stored, "" if is_dedicated else " (shared rate-limit budget)")
    if not stored and not args.force:
        logger.info("Nothing stored. Outside market hours this is expected; use --force to test plumbing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
