"""Persistent SmartAPI WebSocket feed for index spot prices.

WHY THIS EXISTS
----------------
The dashboard used to call SmartAPI's REST quote endpoint on every render
(later, on every cache miss after a 5s TTL was added). Both designs still tie
SmartAPI call volume to how often the dashboard is viewed -- N concurrent
viewers or N rapid page loads can still produce close to N calls whenever
they land outside the cache window, and that call shares the same
process-wide 1 req/sec throttle AI Origination's own candle-refresh calls
depend on (see CLAUDE.md, "Dashboard-driven SmartAPI rate exhaustion").

This replaces that with ONE persistent WebSocket connection to Angel One's
live market-data feed, established once at app startup and independent of
dashboard traffic. Dashboard requests read the latest price from an
in-memory store this feed keeps updated -- zero additional SmartAPI calls
per view, whether there is 1 viewer or 1000.

NOT VERIFIED AGAINST THE REAL FEED
------------------------------------
This sandbox has no network path to Angel One (outbound HTTPS/WSS is
proxied and blocked) and no real SmartAPI credentials, so this module could
only be built against SmartWebSocketV2's actual installed source
(SmartApi==1.5.5) and this app's own REST-based price handling, not tested
against a live tick stream. Two assumptions specifically need confirming in
production before trusting this fully:

  1. Price scaling: the WS binary tick's last_traded_price is documented by
     Angel as paise (price * 100). This app's existing REST path
     (SmartAPIClient.get_ltp -> response["data"]["ltp"]) already returns
     rupees directly with no scaling. If the paise assumption is wrong here,
     every price will read exactly 100x too large or too small.
  2. Reconnection behavior under SmartWebSocketV2's own retry logic: its
     _on_error handler retries internally up to max_retry_attempt times with
     blocking sleeps, then gives up and calls close_connection(). _run's
     outer loop is what actually keeps the feed alive indefinitely after
     that -- confirm by deliberately killing the connection once deployed
     (see CLAUDE.md's "Verify" checklist for this feature).

_handle_open/_handle_data/_handle_error/_handle_close are plain instance
methods (not closures inside _run) specifically so they can be unit-tested
directly, without a real websocket connection -- see tests/test_live_feed.py.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.signal_validation import check_market_hours
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

# Angel's WS binary tick reports last_traded_price in paise; this app's own
# REST-based get_ltp() returns rupees directly (see module docstring, point 1).
_PAISE_PER_RUPEE = 100.0

# SmartWebSocketV2 exchangeType/mode constants, duplicated here as plain ints
# rather than importing the SDK at module level (see IndexFeed._run's
# docstring on why that import is deferred). Values confirmed by reading the
# installed SmartApi==1.5.5 source directly.
_EXCHANGE_TYPE_BY_NAME = {"NSE": 1}  # NSE_CM
_DEFAULT_EXCHANGE_TYPE = 1
_LTP_MODE = 1

# How long a feed entry can go without a fresh tick before dashboard reads
# stop trusting it as "live" -- doesn't affect what's returned, only the
# is_live flag callers use to decide whether to show a staleness indicator.
# Wider than a single heartbeat interval (10s, per SmartWebSocketV2) so a
# momentary gap between ticks on a quiet index doesn't false-flag as stale.
_STALE_AFTER_SECONDS = 30.0

# Outer reconnect loop's own backoff, separate from SmartWebSocketV2's
# internal retry (which gives up after max_retry_attempt and returns control
# to us). This is what keeps the feed alive indefinitely across an outage
# lasting longer than the SDK's own retry budget.
_RECONNECT_DELAY_SECONDS = 10.0

# 17 Aug 2026: this thread is not a scheduled job, so it was never covered by
# the 14 Aug scheduler market-hours gate (that only touched apscheduler jobs)
# or the 15 Aug dashboard-tick fix (that only touched dashboard-poll-triggered
# writes) -- see CLAUDE.md's "SmartAPI calls stopped outside market hours" and
# "Dashboard kept 'updating' on closed days" entries. Without its own gate,
# _run's reconnect loop dialed Angel's WS endpoint every _RECONNECT_DELAY_
# SECONDS (10s) all night and every weekend, real connection attempts with
# zero new information. Checked much less often than the open-market
# reconnect delay -- nothing here is time-sensitive while the market's shut,
# and re-deriving "is the market open" every 10s all night is itself pointless
# work.
_CLOSED_MARKET_POLL_SECONDS = 300.0


@dataclass
class _FeedEntry:
    price: float
    updated_monotonic: float


class LiveFeedStore:
    """Thread-safe in-memory store: the background feed thread writes to it,
    dashboard request handlers (a different thread, via FastAPI's sync-def
    threadpool) read from it. Single instance per process is sufficient --
    uvicorn runs this app with no --workers (see Dockerfile), so there is
    exactly one process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _FeedEntry] = {}
        self._connected = False

    def update(self, index_symbol: str, price: float) -> None:
        with self._lock:
            self._entries[index_symbol] = _FeedEntry(price=price, updated_monotonic=time.monotonic())

    def mark_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def get(self, index_symbol: str) -> dict[str, Any] | None:
        """Latest known price for this index, or None if the feed has never
        produced one yet (just started, or never subscribed to this symbol).
        Deliberately does NOT fall back to a fresh SmartAPI call on a miss --
        that would reintroduce the exact per-request cost this feed exists to
        eliminate. Callers should render "unavailable" instead, same as any
        other fail-closed gap in this codebase."""
        with self._lock:
            entry = self._entries.get(index_symbol)
            connected = self._connected
        if entry is None:
            return None
        age = time.monotonic() - entry.updated_monotonic
        return {
            "price": entry.price,
            "is_live": connected and age < _STALE_AFTER_SECONDS,
            "age_seconds": round(age, 1),
        }


class IndexFeed:
    """Wraps SmartWebSocketV2 in a single background thread, subscribed to
    every enabled index's spot token. Start once at app startup
    (app/main.py's lifespan), stop once at shutdown. Never raises out of
    start()/stop() -- a feed failure should degrade the dashboard to
    "unavailable", never take down the app or block live trading, which
    doesn't depend on this at all (AI Origination and the trade monitor keep
    using their own existing REST calls, untouched by this module)."""

    def __init__(self, smartapi_client: Any, store: LiveFeedStore, indexes: list[Any]) -> None:
        self._client = smartapi_client
        self.store = store
        self._indexes = [idx for idx in indexes if idx.spot_token and idx.spot_exchange]
        self._token_list = self._build_token_list()
        self._token_to_symbol = self._build_token_to_symbol()
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._stop_requested = False

    def _build_token_list(self) -> list[dict[str, Any]]:
        by_exchange: dict[int, list[str]] = {}
        for index in self._indexes:
            exchange_type = _EXCHANGE_TYPE_BY_NAME.get(index.spot_exchange, _DEFAULT_EXCHANGE_TYPE)
            by_exchange.setdefault(exchange_type, []).append(index.spot_token)
        return [{"exchangeType": exchange_type, "tokens": tokens} for exchange_type, tokens in by_exchange.items()]

    def _build_token_to_symbol(self) -> dict[str, str]:
        return {index.spot_token: index.symbol for index in self._indexes}

    def start(self) -> None:
        if not self._indexes:
            logger.warning("[LIVEFEED] No enabled index has a spot token configured; feed not started")
            return
        self._stop_requested = False
        self._thread = threading.Thread(target=self._run, name="index-live-feed", daemon=True)
        self._thread.start()
        logger.info("[LIVEFEED] Started background feed thread for %s", [i.symbol for i in self._indexes])

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:
                logger.exception("[LIVEFEED] Error closing websocket during shutdown")

    def _handle_open(self, wsapp: Any) -> None:
        logger.info("[LIVEFEED] Connected; subscribing to %s", self._token_list)
        self.store.mark_connected(True)
        try:
            self._ws.subscribe("livefeed01", _LTP_MODE, self._token_list)
        except Exception:
            logger.exception("[LIVEFEED] Subscribe failed")

    def _handle_data(self, wsapp: Any, message: dict[str, Any]) -> None:
        try:
            token = message.get("token")
            symbol = self._token_to_symbol.get(token)
            raw_price = message.get("last_traded_price")
            if symbol is None or raw_price is None:
                return
            self.store.update(symbol, float(raw_price) / _PAISE_PER_RUPEE)
        except Exception:
            logger.exception("[LIVEFEED] Error processing tick: %r", message)

    def _handle_error(self, *args: Any) -> None:
        # SmartWebSocketV2's own _on_error calls this with either
        # ("Reconnect Error", detail) or ("Max retry attempt reached",
        # detail) -- accept *args defensively since that call signature isn't
        # part of any documented contract, just observed in the installed
        # SDK source (its own base on_error(self) takes no args at all).
        logger.warning("[LIVEFEED] Feed error: %s", args)
        self.store.mark_connected(False)

    def _handle_close(self, wsapp: Any) -> None:
        logger.warning("[LIVEFEED] Feed closed")
        self.store.mark_connected(False)

    def _run(self) -> None:
        """Outer reconnect loop. SmartWebSocketV2.connect() blocks (it runs
        websocket-client's run_forever internally) until the connection
        closes, so each iteration of this loop represents one connection
        attempt's full lifetime. Reads jwt_token/feed_token fresh on every
        iteration rather than once at IndexFeed construction, since the
        underlying SmartAPIClient can rotate them independently via its own
        re-auth flow while this feed is running.

        Import deferred to here (not module level) so that simply importing
        app.live_feed -- which app.main does at startup -- never pulls
        SmartApi's websocket machinery into the process unless the feed
        actually starts. Matches this codebase's existing care about import
        graph size (see tests/test_module_imports.py)."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        was_closed = False
        while not self._stop_requested:
            closed_reason = check_market_hours(utc_now())
            if closed_reason is not None:
                if not was_closed:
                    # closed_reason's own wording ("Signal received...") is
                    # written for check_market_hours()'s other caller
                    # (validating an incoming TradingView webhook) -- not
                    # relevant here, so only the reason itself is logged.
                    logger.info(
                        "[LIVEFEED] Market closed (%s); pausing connection attempts",
                        closed_reason.replace("Signal received ", "", 1),
                    )
                    was_closed = True
                self.store.mark_connected(False)
                time.sleep(_CLOSED_MARKET_POLL_SECONDS)
                continue
            if was_closed:
                logger.info("[LIVEFEED] Market open again; resuming connection attempts")
                was_closed = False

            jwt_token = self._client.jwt_token
            feed_token = self._client.feed_token
            if not jwt_token or not feed_token:
                logger.info("[LIVEFEED] Waiting for SmartAPI authentication before connecting")
                self.store.mark_connected(False)
                time.sleep(_RECONNECT_DELAY_SECONDS)
                continue

            try:
                ws = SmartWebSocketV2(
                    auth_token=jwt_token,
                    api_key=self._client.settings.smartapi_api_key,
                    client_code=self._client.settings.smartapi_client_id,
                    feed_token=feed_token,
                    max_retry_attempt=5,
                    retry_strategy=1,  # exponential backoff, SmartWebSocketV2's own internal retry
                    retry_delay=5,
                    retry_multiplier=2,
                )
                self._ws = ws
                ws.on_open = self._handle_open
                ws.on_data = self._handle_data
                ws.on_error = self._handle_error
                ws.on_close = self._handle_close
                self.store.mark_connected(False)
                ws.connect()  # blocks until the connection closes for any reason
            except Exception:
                logger.exception("[LIVEFEED] Feed connection attempt failed")
            finally:
                self.store.mark_connected(False)

            if self._stop_requested:
                return
            logger.warning("[LIVEFEED] Feed disconnected; reconnecting in %.0fs", _RECONNECT_DELAY_SECONDS)
            time.sleep(_RECONNECT_DELAY_SECONDS)
