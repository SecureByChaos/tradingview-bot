"""Persistent SmartAPI WebSocket feed driving Quick Scalp's entry signal --
built 8 Sep 2026 as part of the "NIFTY 50 VWAP 2sigma Scalp Engine" spec's
full rebuild (see app.quick_scalp's own module docstring for the rest of
that rebuild). This module is the ONE genuinely new architectural piece the
spec asked for that the original 4 Sep build explicitly declined: real tick
aggregation instead of periodic REST candle polling.

WHY THIS EXISTS
----------------
The 4 Sep build's own "NAMED DEVIATIONS" section argued that building a raw
WebSocket tick-aggregation pipeline was "a materially larger, separate
infrastructure project this codebase doesn't have." That is still true in
the sense that this is genuinely new code, not a small tweak -- but the 8
Sep spec explicitly asks for it again, naming the exact problem REST polling
causes for this specific strategy: a 1-minute-cadence poll can only ever
see a bar's CLOSE up to a minute late, plus whatever the poll's own request
latency adds on top -- for a strategy whose whole edge is reacting to a
rejection wick within its own 1-minute window, that lag is the "false-
breakout chop" the spec's Section 1 names as bottleneck #1. This module
closes that gap: bars are finalized the instant a new tick's minute rolls
over, not up to 60+ seconds later on the next scheduler firing.

WHAT THIS MODULE DOES AND DOES NOT DO
----------------------------------------
This is a BAR-COMPLETION engine, not a full re-implementation of the spec's
own reference MemorySafeFeedEngine/QuickScalpManager classes. It aggregates
live ticks into 1-minute Bar objects (app.market_data.Bar, the same type
every other consumer in this codebase already uses) and persists each
completed bar via store_bars -- exactly the same DB row a REST poll would
have written, just sooner and closer to the true tick data. All of the
actual VWAP/sigma/RSI/setup-trigger MATH still lives in app.quick_scalp's
existing, already-tested pure functions (_compute_vwap_bands, rsi,
vwap_scalp_action), re-run against the DB's stored session-to-date bars on
every bar-close callback -- this deliberately does NOT hand-roll a second,
parallel incremental VWAP/RSI implementation that could silently diverge
from the canonical one every other strategy in this app also reads. Once a
bar closes, recomputing VWAP/sigma/RSI over roughly 375 bars (a full
session) is negligible work; the spec's own "O(1) per bar" framing is a
real, correct optimization concern for a HIGH-frequency, per-TICK engine,
not for something that only needs to run once per completed BAR.

Position-level monitoring (the option-premium stop/target, the structural
index-level stop, the 3-minute breakeven-trail-or-scratch decision) is
DELIBERATELY left on app.quick_scalp's existing fast-poll scheduler job
(quick-scalp-exit-check, 5-second IntervalTrigger -- see app/scheduler.py),
not moved onto a second, dynamically-resubscribing option-contract WS
channel. This is a real, named departure from the spec's own
`on_option_tick` design: building a SECOND subscription-management system
(subscribe on entry, unsubscribe on exit, per open position) is materially
more moving parts for a decision (has 180 seconds elapsed, is the option
now profitable enough to cover costs) that does not need tick-level
precision -- a 5-second poll resolves it within 5 seconds of the true
180-second mark, an error smaller than this strategy's own round-trip cost
buffer. This also directly matches an already-established precedent in
this exact codebase: app.validated_signal's own 5-second exit poll was
built for the identical reason (a genuinely continuous WS position monitor
was scoped out there too, in favour of a fast poll this project already
trusts).

A REAL, MATERIAL RESOURCE COST, NAMED EXPLICITLY
----------------------------------------------------
This is a SECOND persistent background thread and WebSocket connection,
alongside app.live_feed.IndexFeed's existing one -- on a production box that
this same week's CLAUDE.md history documents as already memory-constrained
(a 412Mi Lightsail instance running six-plus concurrent scheduler jobs).
Requested and built anyway, with this tradeoff surfaced plainly before
building rather than glossed over -- see the PR/CLAUDE.md entry for this
change for the explicit choice made. If this box's capacity pressure gets
materially worse after deploying this, this feed (like AI Origination
before it) is a real, specific thing to consider pausing -- unlike the
scheduler-job strategies, pausing this one means not starting QuickScalpFeed
at all in app/main.py's lifespan, since it isn't scheduler-driven.

NOT VERIFIED AGAINST THE REAL FEED
-------------------------------------
Same standing constraint as app.live_feed.IndexFeed (this sandbox has no
network path to Angel One): built directly against SmartWebSocketV2's
installed source (SmartApi==1.5.5), not tested against a live tick stream.
Assumptions specifically needing confirmation once deployed, beyond the two
IndexFeed's own docstring already names (LTP paise-scaling, reconnect
behaviour):

  1. `volume_trade_for_the_day` (QUOTE mode, byte offset 67-75 in the
     installed SDK's _parse_binary_data) is documented by its own name as a
     CUMULATIVE running total for the session, not a per-tick trade size --
     this module computes per-minute volume as the delta between
     consecutive readings, clamped at zero (a session reset, a stale first
     reading after reconnect, or an out-of-order tick would otherwise
     produce a negative delta). If this field is actually per-tick rather
     than cumulative, every computed per-minute volume will read far too
     low.
  2. Bar boundaries are bucketed on local wall-clock time (`time.time()`),
     not the tick's own `exchange_timestamp` field -- deliberately, since
     that field's unit (seconds vs. milliseconds epoch) is not confirmed
     from static reading alone, and network/processing latency between a
     real exchange tick and this process receiving it is small enough
     relative to a 60-second bar that wall-clock bucketing is an accepted
     approximation.
  3. NSE_FO (exchange type 2) is assumed correct for a FUTIDX contract's
     WebSocket subscription, mirroring index spot's own NSE_CM (type 1)
     already confirmed working in app.live_feed -- read from the installed
     SDK's own class constants, not observed against a real futures tick.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.market_data import ONE_MINUTE, Bar, store_bars
from app.signal_validation import check_market_hours
from app.time_utils import IST, utc_now

logger = logging.getLogger(__name__)

_PAISE_PER_RUPEE = 100.0

# SmartWebSocketV2 mode/exchange-type constants, duplicated as plain ints
# rather than importing the SDK at module level -- same reasoning as
# app.live_feed.IndexFeed's identical choice (see that module's _run
# docstring): keep SmartApi's websocket machinery out of the process unless
# this feed actually starts.
_LTP_MODE = 1
_QUOTE_MODE = 2
_SPOT_EXCHANGE_TYPE = 1   # NSE_CM
_FUTURES_EXCHANGE_TYPE = 2  # NSE_FO

_RECONNECT_DELAY_SECONDS = 10.0
_CLOSED_MARKET_POLL_SECONDS = 300.0


@dataclass
class _FormingBar:
    minute_bucket: int
    open: float
    high: float
    low: float
    close: float


def _minute_bucket_to_ts_ist(minute_bucket: int) -> datetime:
    """Integer epoch-minutes -> naive-IST datetime, matching every other
    Bar.ts_ist in this codebase (see app.market_data.parse_smartapi_row's
    own reasoning for why naive-IST, not aware)."""
    aware_utc = datetime.fromtimestamp(minute_bucket * 60, tz=timezone.utc)
    return aware_utc.astimezone(IST).replace(tzinfo=None, second=0, microsecond=0)


class QuickScalpFeed:
    """Background WS feed aggregating live ticks into 1-minute Bars for
    Quick Scalp's entry signal. One instance for the whole process (mirrors
    app.live_feed.IndexFeed's own single-instance-per-process reasoning --
    uvicorn runs this app with no --workers).

    `on_bar_closed(index_symbol)` is called synchronously, on this feed's
    own background thread, once a completed bar has already been persisted
    via store_bars -- the callback's job is only to read that bar back
    (alongside the rest of today's session) and run the entry check; it
    should open and close its own DB session, the same convention every
    other scheduler-job-driven entry point in this codebase already
    follows."""

    def __init__(
        self,
        smartapi_client: Any,
        option_finder: Any,
        session_factory: Callable[[], Any],
        on_bar_closed: Callable[[str], None],
        indexes: list[Any],
    ) -> None:
        self._client = smartapi_client
        self._option_finder = option_finder
        self._session_factory = session_factory
        self._on_bar_closed = on_bar_closed
        self._indexes = [idx for idx in indexes if idx.spot_token and idx.spot_exchange]
        self._lock = threading.Lock()
        self._forming: dict[str, _FormingBar] = {}
        self._minute_volume: dict[str, dict[int, float]] = {}
        self._last_futures_cum_volume: dict[str, float] = {}
        self._spot_token_to_symbol: dict[str, str] = {}
        self._futures_token_to_symbol: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._stop_requested = False

    def start(self) -> None:
        if not self._indexes:
            logger.warning("[QUICK_SCALP_FEED] No enabled index has a spot token configured; feed not started")
            return
        self._stop_requested = False
        self._thread = threading.Thread(target=self._run, name="quick-scalp-feed", daemon=True)
        self._thread.start()
        logger.info("[QUICK_SCALP_FEED] Started background feed thread for %s", [i.symbol for i in self._indexes])

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:
                logger.exception("[QUICK_SCALP_FEED] Error closing websocket during shutdown")

    # -- tick handling ---------------------------------------------------

    def _on_spot_tick(self, symbol: str, price: float, minute_bucket: int) -> None:
        closed_bar: Bar | None = None
        with self._lock:
            forming = self._forming.get(symbol)
            volumes = self._minute_volume.setdefault(symbol, {})
            if forming is None:
                self._forming[symbol] = _FormingBar(minute_bucket, price, price, price, price)
                return
            if minute_bucket == forming.minute_bucket:
                forming.high = max(forming.high, price)
                forming.low = min(forming.low, price)
                forming.close = price
                return
            # Minute rolled over -- finalize the just-completed bar before
            # starting a new one at this tick's price.
            volume = volumes.pop(forming.minute_bucket, 0.0)
            closed_bar = Bar(
                ts_ist=_minute_bucket_to_ts_ist(forming.minute_bucket),
                open=forming.open, high=forming.high, low=forming.low, close=forming.close,
                volume=volume,
            )
            self._forming[symbol] = _FormingBar(minute_bucket, price, price, price, price)
        if closed_bar is not None:
            self._finalize_bar(symbol, closed_bar)

    def _on_futures_tick(self, symbol: str, cumulative_volume: float | None, minute_bucket: int) -> None:
        if cumulative_volume is None:
            return
        with self._lock:
            last = self._last_futures_cum_volume.get(symbol)
            self._last_futures_cum_volume[symbol] = cumulative_volume
            if last is None:
                return  # No baseline yet this connection -- delta unknown, skip.
            delta = max(0.0, cumulative_volume - last)
            if delta <= 0:
                return
            bucket = self._minute_volume.setdefault(symbol, {})
            bucket[minute_bucket] = bucket.get(minute_bucket, 0.0) + delta

    def _finalize_bar(self, symbol: str, bar: Bar) -> None:
        try:
            with self._session_factory() as db:
                store_bars(db, symbol, ONE_MINUTE, [bar])
                db.commit()
        except Exception:
            logger.exception("[QUICK_SCALP_FEED] Failed to persist bar for %s", symbol)
            return
        try:
            self._on_bar_closed(symbol)
        except Exception:
            logger.exception("[QUICK_SCALP_FEED] on_bar_closed callback failed for %s", symbol)

    def _handle_open(self, wsapp: Any, spot_tokens: list[str], futures_tokens: list[str]) -> None:
        logger.info("[QUICK_SCALP_FEED] Connected; subscribing spot=%s futures=%s", spot_tokens, futures_tokens)
        try:
            if spot_tokens:
                self._ws.subscribe(
                    "quickscalp-spot", _LTP_MODE,
                    [{"exchangeType": _SPOT_EXCHANGE_TYPE, "tokens": spot_tokens}],
                )
            if futures_tokens:
                self._ws.subscribe(
                    "quickscalp-fut", _QUOTE_MODE,
                    [{"exchangeType": _FUTURES_EXCHANGE_TYPE, "tokens": futures_tokens}],
                )
        except Exception:
            logger.exception("[QUICK_SCALP_FEED] Subscribe failed")

    def _handle_data(self, wsapp: Any, message: dict[str, Any]) -> None:
        try:
            token = message.get("token")
            raw_ltp = message.get("last_traded_price")
            if token is None or raw_ltp is None:
                return
            price = float(raw_ltp) / _PAISE_PER_RUPEE
            minute_bucket = int(time.time() // 60)

            spot_symbol = self._spot_token_to_symbol.get(token)
            if spot_symbol is not None:
                self._on_spot_tick(spot_symbol, price, minute_bucket)
                return

            futures_symbol = self._futures_token_to_symbol.get(token)
            if futures_symbol is not None:
                self._on_futures_tick(futures_symbol, message.get("volume_trade_for_the_day"), minute_bucket)
        except Exception:
            logger.exception("[QUICK_SCALP_FEED] Error processing tick: %r", message)

    def _handle_error(self, *args: Any) -> None:
        logger.warning("[QUICK_SCALP_FEED] Feed error: %s", args)

    def _handle_close(self, wsapp: Any) -> None:
        logger.warning("[QUICK_SCALP_FEED] Feed closed")

    # -- connection lifecycle ---------------------------------------------

    def _resolve_tokens(self) -> tuple[list[str], list[str]]:
        """Rebuilds the spot/futures token maps fresh for this connection
        attempt -- the futures leg specifically needs this, since the
        near-month FUTIDX contract rolls at expiry and a stale token would
        silently stop producing volume ticks with no error."""
        self._spot_token_to_symbol = {idx.spot_token: idx.symbol for idx in self._indexes}
        futures_map: dict[str, str] = {}
        for index in self._indexes:
            try:
                contract = self._option_finder.find_current_futures_contract(index)
            except Exception as exc:
                logger.info("[QUICK_SCALP_FEED] %s: futures contract lookup failed (%s), volume will fall back to equal-weighted", index.symbol, exc)
                continue
            if contract is not None:
                futures_map[str(contract["symboltoken"])] = index.symbol
        self._futures_token_to_symbol = futures_map
        return list(self._spot_token_to_symbol), list(self._futures_token_to_symbol)

    def _run(self) -> None:
        """Outer reconnect loop -- same shape as app.live_feed.IndexFeed's
        own _run (deferred SDK import, market-hours gate, blocking connect()
        per attempt), duplicated rather than shared since the two feeds
        subscribe to different token sets/modes and serve different
        purposes; see that module's own docstring for why each design
        choice here mirrors it."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        was_closed = False
        while not self._stop_requested:
            closed_reason = check_market_hours(utc_now())
            if closed_reason is not None:
                if not was_closed:
                    logger.info(
                        "[QUICK_SCALP_FEED] Market closed (%s); pausing connection attempts",
                        closed_reason.replace("Signal received ", "", 1),
                    )
                    was_closed = True
                time.sleep(_CLOSED_MARKET_POLL_SECONDS)
                continue
            if was_closed:
                logger.info("[QUICK_SCALP_FEED] Market open again; resuming connection attempts")
                was_closed = False

            jwt_token = self._client.jwt_token
            feed_token = self._client.feed_token
            if not jwt_token or not feed_token:
                logger.info("[QUICK_SCALP_FEED] Waiting for SmartAPI authentication before connecting")
                time.sleep(_RECONNECT_DELAY_SECONDS)
                continue

            spot_tokens, futures_tokens = self._resolve_tokens()
            self._last_futures_cum_volume = {}
            try:
                ws = SmartWebSocketV2(
                    auth_token=jwt_token,
                    api_key=self._client.settings.smartapi_api_key,
                    client_code=self._client.settings.smartapi_client_id,
                    feed_token=feed_token,
                    max_retry_attempt=5,
                    retry_strategy=1,
                    retry_delay=5,
                    retry_multiplier=2,
                )
                self._ws = ws
                ws.on_open = lambda wsapp: self._handle_open(wsapp, spot_tokens, futures_tokens)
                ws.on_data = self._handle_data
                ws.on_error = self._handle_error
                ws.on_close = self._handle_close
                ws.connect()  # blocks until the connection closes for any reason
            except Exception:
                logger.exception("[QUICK_SCALP_FEED] Feed connection attempt failed")

            if self._stop_requested:
                return
            logger.warning("[QUICK_SCALP_FEED] Feed disconnected; reconnecting in %.0fs", _RECONNECT_DELAY_SECONDS)
            time.sleep(_RECONNECT_DELAY_SECONDS)
