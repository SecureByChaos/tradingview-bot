"""Periodic option-chain snapshots: open interest, IV, volume, LTP, spot.

COLLECTION ONLY. Nothing here produces a signal, a score or a threshold, and
nothing in the live trading path reads what it writes. See
app/option_chain_store.py for why this data is worth accumulating and why it
lives in its own database file.

THE RATE-LIMIT BUDGET, HONESTLY
-------------------------------
The instruction for this collector was to keep it on a budget separate from
live origination, because on 31 July an analysis job sharing the quota is the
leading suspect for starving live origination's candle refresh for nearly three
hours.

The uncomfortable part: Angel One rate-limits per API KEY, not per session.
Authenticating a second SmartAPI client on the same credentials therefore buys
no extra budget at all -- it would just remove the process-wide throttle that
currently serialises quote calls, which is strictly worse than sharing. A
genuinely separate budget requires a genuinely separate API key.

So there are two modes, and the difference between them is real:

  * SEPARATE (SMARTAPI_ANALYTICS_API_KEY et al configured) -- a dedicated
    client on its own key. Actually independent; live trading cannot be
    starved by this collector.
  * SHARED (nothing configured, the default) -- reuses the live client and its
    throttle, and is therefore NOT isolated. Mitigated by being strictly
    subordinate: a hard per-cycle call cap, and a full skip whenever the live
    client has been rate-limited recently. That is yielding, not isolation, and
    the startup log says so rather than implying otherwise.

Cost either way is small: ~7 requests per 5-minute cycle against a budget
measured per second. The 31 July outage came from a bulk historical backfill,
which is a different order of magnitude from this.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.db_models import IndexConfig
from app.option_chain_store import store_snapshot
from app.signal_validation import check_market_hours
from app.smartapi_client import SmartAPIClient
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)

# getMarketData accepts at most 50 tokens per request.
_TOKENS_PER_REQUEST = 50
# Hard ceiling per cycle in SHARED mode. At the configured defaults a cycle
# needs ~7 requests; anything approaching this cap means the configuration has
# grown past what a subordinate job should be spending, and the cycle is cut
# short rather than allowed to creep.
_MAX_REQUESTS_PER_CYCLE = 20
# In SHARED mode, skip entirely if the live client hit a rate limit this
# recently. The collector's data is worth nothing next to a missed exit check.
_YIELD_AFTER_RATE_LIMIT_MINUTES = 15


@dataclass(frozen=True)
class ChainContract:
    index_symbol: str
    expiry: str
    strike: float
    option_type: str
    symboltoken: str
    tradingsymbol: str
    exchange: str


def _parse_expiry(text: str) -> date | None:
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(text).strip().upper(), fmt).date()
        except ValueError:
            continue
    return None


def _load_scrip(settings: Settings) -> list[dict[str, Any]]:
    """Read the instrument master OptionFinder already caches.

    Plain json rather than pandas: this runs every five minutes inside the live
    process, and building a ~100k-row DataFrame each cycle to filter a few
    hundred rows is a waste of a constrained box's memory.
    """
    path: Path = settings.instrument_cache_path
    if not path.exists():
        logger.info("[CHAIN] No instrument cache at %s yet; skipping cycle", path)
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[CHAIN] Could not read instrument cache: %s", exc)
        return []


def _normalise_strike(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Angel reports some strikes in paise. Same normalisation OptionFinder uses.
    return value / 100 if value > 100000 else value


def select_expiries(expiry_dates: set[date], today: date, count: int) -> list[date]:
    """The nearest N expiries, plus the nearest monthly beyond them.

    "Current month and next month" is ambiguous for an index with weeklies:
    Nifty's nearest two expiries are usually both inside the current month,
    while Bank Nifty's nearest expiry IS a monthly. Taking the nearest N covers
    the contracts actually being traded, and adding the next monthly guarantees
    the longer-dated end is represented on the weekly index too -- which is the
    part with no coverage today.

    A monthly is identified as the last expiry falling in its calendar month,
    which is how NSE defines it, rather than by a date rule that holidays break.
    """
    future = sorted(d for d in expiry_dates if d >= today)
    if not future:
        return []
    chosen = future[:count]

    last_of_month: dict[tuple[int, int], date] = {}
    for expiry in future:
        key = (expiry.year, expiry.month)
        if key not in last_of_month or expiry > last_of_month[key]:
            last_of_month[key] = expiry
    monthlies = set(last_of_month.values())

    # Only reach further out if the nearest expiries missed the monthly
    # entirely. Bank Nifty has no weeklies, so its nearest two ARE monthlies
    # and appending a third would collect a contract nobody trades at 60 DTE.
    # Nifty's nearest two are usually mid-month weeklies, and there the monthly
    # is genuinely absent and worth adding -- it is the only long-dated Nifty
    # coverage this archive will ever get.
    if not any(expiry in monthlies for expiry in chosen):
        next_monthly = next((m for m in sorted(monthlies) if m not in chosen), None)
        if next_monthly is not None:
            chosen = sorted({*chosen, next_monthly})
    return chosen


def build_contract_list(
    settings: Settings, strike_band: int, expiry_count: int, spots: dict[str, float]
) -> list[ChainContract]:
    """ATM +/- strike_band, both sides, across the selected expiries.

    Spot has to be known first: the band is anchored on ATM, and an index that
    moved 2% since the last cycle would otherwise have its band centred in the
    wrong place, quietly recording a lopsided chain.
    """
    scrip = _load_scrip(settings)
    if not scrip:
        return []

    with SessionLocal() as session:
        indexes = [
            index for index in session.scalars(select(IndexConfig))
            if index.enabled and index.spot_token
        ]

    today = to_ist(utc_now()).date()
    contracts: list[ChainContract] = []

    for index in indexes:
        spot = spots.get(index.symbol)
        if not spot:
            logger.info("[CHAIN] %s: no spot available this cycle, skipping", index.symbol)
            continue
        interval = index.strike_interval or 100
        name = (index.instrument_name or index.symbol).upper()
        segment = index.exchange_segment

        rows = []
        for item in scrip:
            if item.get("exch_seg") != segment:
                continue
            if str(item.get("name", "")).upper() != name:
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol.endswith(("CE", "PE")):
                continue
            expiry = _parse_expiry(item.get("expiry", ""))
            strike = _normalise_strike(item.get("strike"))
            if expiry is None or strike is None:
                continue
            rows.append((expiry, strike, symbol, item))

        if not rows:
            logger.info("[CHAIN] %s: no option contracts in the instrument master", index.symbol)
            continue

        expiries = select_expiries({r[0] for r in rows}, today, expiry_count)
        atm = round(float(spot) / interval) * interval
        wanted = {atm + step * interval for step in range(-strike_band, strike_band + 1)}

        for expiry, strike, symbol, item in rows:
            if expiry not in expiries:
                continue
            # Tolerance rather than equality: strike is a divided float.
            if not any(abs(strike - target) < 0.5 for target in wanted):
                continue
            contracts.append(
                ChainContract(
                    index_symbol=index.symbol,
                    expiry=expiry.strftime("%d%b%Y").upper(),
                    strike=strike,
                    option_type="CE" if symbol.endswith("CE") else "PE",
                    symboltoken=str(item.get("token")),
                    tradingsymbol=symbol,
                    exchange=segment,
                )
            )

        logger.info(
            "[CHAIN] %s: spot %.2f -> ATM %s, %s expiries %s",
            index.symbol, float(spot), atm, len(expiries),
            ", ".join(e.strftime("%d%b%Y").upper() for e in expiries),
        )
    return contracts


def _fetch_spots(smartapi: SmartAPIClient) -> dict[str, float]:
    """Every configured index's spot in one request, keyed by symbol.

    Batched deliberately: one getMarketData LTP call for all indices costs a
    single slot against the throttle, where get_index_spot per index would cost
    one each and serialise a second apart.
    """
    with SessionLocal() as session:
        indexes = [
            index for index in session.scalars(select(IndexConfig))
            if index.enabled and index.spot_token
        ]
    if not indexes:
        return {}

    by_exchange: dict[str, list[str]] = {}
    token_to_symbol: dict[str, str] = {}
    for index in indexes:
        by_exchange.setdefault(index.spot_exchange, []).append(str(index.spot_token))
        token_to_symbol[str(index.spot_token)] = index.symbol

    try:
        response = smartapi.get_market_data("LTP", by_exchange)
    except Exception as exc:
        logger.warning("[CHAIN] Spot fetch failed: %s", exc)
        return {}

    spots: dict[str, float] = {}
    for row in response:
        symbol = token_to_symbol.get(str(row.get("symbolToken") or ""))
        try:
            ltp = float(row.get("ltp") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and ltp > 0:
            spots[symbol] = ltp
    return spots


def _fetch_greeks(smartapi: SmartAPIClient, contracts: list[ChainContract]) -> dict[tuple, float]:
    """(index, expiry, strike, option_type) -> implied volatility.

    Separate endpoint from the quote data, and an optional one: if the
    installed SmartAPI SDK has no optionGreek method, or the call fails, the
    snapshot still stores OI, volume, LTP and spot. IV is the one field worth
    having but not worth losing everything else over.

    UNITS ARE UNVERIFIED. Stored exactly as reported, no scaling. A 3 Aug probe
    read impliedVolatility 5.81 on a Bank Nifty 22-DTE contract whose own
    premium implies something nearer 15% by a straddle estimate, so the figure
    is either on a different convention or not what its name suggests. Recorded
    raw so it can be reconciled later; do NOT treat it as a percentage without
    checking it against a contract whose premium you can price independently.
    """
    wanted = {(c.index_symbol, c.expiry) for c in contracts}
    greeks: dict[tuple, float] = {}
    for index_symbol, expiry in sorted(wanted):
        try:
            rows = smartapi.get_option_greeks(index_symbol, expiry)
        except Exception as exc:
            logger.info("[CHAIN] Greeks unavailable for %s %s: %s", index_symbol, expiry, exc)
            continue
        for row in rows:
            strike = _normalise_strike(row.get("strikePrice"))
            option_type = str(row.get("optionType") or "").upper()
            raw_iv = row.get("impliedVolatility")
            if strike is None or option_type not in {"CE", "PE"}:
                continue
            try:
                iv = float(raw_iv)
            except (TypeError, ValueError):
                continue
            greeks[(index_symbol, expiry, round(strike, 2), option_type)] = iv
    return greeks


def _should_yield_to_live_trading(smartapi: SmartAPIClient) -> bool:
    """True when the shared client has been rate-limited recently.

    The collector is subordinate by construction. If the live client is already
    under pressure, a missed snapshot costs one row in an archive that will not
    be read for months; a missed exit check costs money.
    """
    try:
        health = smartapi.get_broker_health()
    except Exception:
        return False
    last = health.get("last_rate_limited")
    if not isinstance(last, datetime):
        return False
    age = to_ist(utc_now()) - to_ist(last)
    return age < timedelta(minutes=_YIELD_AFTER_RATE_LIMIT_MINUTES)


def collect_once(
    smartapi: SmartAPIClient,
    settings: Settings | None = None,
    force: bool = False,
    dedicated_client: bool = False,
) -> int:
    """One sweep. Returns rows stored.

    force skips the market-hours gate, for manual probing only -- outside
    session hours the broker returns stale or empty quotes, so a forced run is
    for checking plumbing, not for collecting data worth keeping.
    """
    settings = settings or get_settings()

    if not force and check_market_hours(utc_now()) is not None:
        return 0
    if not dedicated_client and not force and _should_yield_to_live_trading(smartapi):
        logger.info(
            "[CHAIN] Live client was rate-limited within %s min; skipping this cycle. "
            "Configure SMARTAPI_ANALYTICS_* for a genuinely separate budget.",
            _YIELD_AFTER_RATE_LIMIT_MINUTES,
        )
        return 0

    spots = _fetch_spots(smartapi)
    if not spots:
        logger.info("[CHAIN] No spot prices this cycle; nothing to anchor the strike band on")
        return 0

    contracts = build_contract_list(
        settings,
        strike_band=settings.option_chain_strike_band,
        expiry_count=settings.option_chain_expiry_count,
        spots=spots,
    )
    if not contracts:
        return 0

    requests_needed = -(-len(contracts) // _TOKENS_PER_REQUEST)
    if requests_needed > _MAX_REQUESTS_PER_CYCLE:
        logger.warning(
            "[CHAIN] %s contracts would need %s requests, over the %s cap. Truncating. "
            "Reduce OPTION_CHAIN_STRIKE_BAND or OPTION_CHAIN_EXPIRY_COUNT.",
            len(contracts), requests_needed, _MAX_REQUESTS_PER_CYCLE,
        )
        contracts = contracts[: _MAX_REQUESTS_PER_CYCLE * _TOKENS_PER_REQUEST]

    # One timestamp for the whole sweep, truncated to the minute. The individual
    # requests are seconds apart; recording that jitter would make "one
    # snapshot" a time-window query instead of an equality test, for no gain.
    snapshot_ts = to_ist(utc_now()).replace(second=0, microsecond=0, tzinfo=None)

    by_token = {c.symboltoken: c for c in contracts}
    quotes: dict[str, dict] = {}
    for start in range(0, len(contracts), _TOKENS_PER_REQUEST):
        chunk = contracts[start : start + _TOKENS_PER_REQUEST]
        payload: dict[str, list[str]] = {}
        for contract in chunk:
            payload.setdefault(contract.exchange, []).append(contract.symboltoken)
        try:
            for row in smartapi.get_market_data("FULL", payload):
                token = str(row.get("symbolToken") or "")
                if token:
                    quotes[token] = row
        except Exception as exc:
            # Partial sweeps are fine -- rows are keyed per contract, so a
            # failed chunk loses those strikes for this minute and nothing else.
            logger.warning("[CHAIN] Quote chunk failed (%s contracts): %s", len(chunk), exc)

    if not quotes:
        logger.warning("[CHAIN] No quotes returned; nothing stored for %s", snapshot_ts)
        return 0

    greeks = _fetch_greeks(smartapi, contracts)

    def _number(raw: Any) -> float | None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value

    rows: list[dict] = []
    for token, quote in quotes.items():
        contract = by_token.get(token)
        if contract is None:
            continue
        rows.append(
            {
                "snapshot_ts": snapshot_ts,
                "index_symbol": contract.index_symbol,
                "expiry": contract.expiry,
                "strike": contract.strike,
                "option_type": contract.option_type,
                "symboltoken": contract.symboltoken,
                "tradingsymbol": contract.tradingsymbol,
                "ltp": _number(quote.get("ltp")),
                # Angel spells this "opnInterest" in getMarketData. Both
                # spellings are read because the field name is not guaranteed
                # stable across SDK versions and a silent None here would empty
                # the single most valuable column in the archive.
                "open_interest": _number(
                    quote.get("opnInterest") if quote.get("opnInterest") is not None
                    else quote.get("openInterest")
                ),
                "volume": _number(
                    quote.get("tradeVolume") if quote.get("tradeVolume") is not None
                    else quote.get("volume")
                ),
                "implied_volatility": greeks.get(
                    (contract.index_symbol, contract.expiry,
                     round(contract.strike, 2), contract.option_type)
                ),
                "spot": spots.get(contract.index_symbol),
            }
        )

    stored = store_snapshot(rows)
    with_oi = sum(1 for r in rows if r["open_interest"] is not None)
    with_iv = sum(1 for r in rows if r["implied_volatility"] is not None)
    logger.info(
        "[CHAIN] %s: stored %s rows (%s with OI, %s with IV) from %s requests",
        snapshot_ts, stored, with_oi, with_iv, requests_needed,
    )
    if with_oi == 0:
        logger.warning(
            "[CHAIN] Every row came back without open interest. The quote payload's "
            "field name has probably changed -- inspect one raw row with "
            "'python -m scripts.collect_option_chain --once --probe' before letting "
            "this accumulate, since an archive of null OI is worth nothing."
        )
    return stored


def build_collector_client(settings: Settings, live_client: SmartAPIClient) -> tuple[SmartAPIClient, bool]:
    """Dedicated client if separate credentials exist, else the live one.

    Returns (client, is_dedicated). The flag is what decides whether the
    collector may run at full cadence or must yield -- see the module docstring
    on why a second session against the SAME key would be worse than sharing.
    """
    if not settings.smartapi_analytics_api_key:
        return live_client, False
    analytics_settings = settings.as_analytics_credentials()
    logger.info("[CHAIN] Using dedicated analytics SmartAPI credentials (separate rate-limit budget)")
    return SmartAPIClient(analytics_settings), True


def run_chain_collection(smartapi: SmartAPIClient, dedicated: bool = False) -> None:
    """Scheduler entry point. Never raises into the scheduler."""
    try:
        collect_once(smartapi, dedicated_client=dedicated)
    except Exception:
        logger.exception("[CHAIN] Collection cycle failed")
