"""Tick-to-bar aggregation for Quick Scalp's entry signal.

WHY THIS EXISTS
----------------
The 4 Sep 2026 build's own "NAMED DEVIATIONS" section argued that building a
raw WebSocket tick-aggregation pipeline was "a materially larger, separate
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

8 SEP 2026, SAME DAY -- MERGED INTO app.live_feed.IndexFeed'S CONNECTION
----------------------------------------------------------------------------
Originally shipped as its own class (`QuickScalpFeed`) owning a second,
independent persistent WebSocket connection alongside app.live_feed's
existing one. Asked directly afterwards: "can't we combine this with the
websocket we already opened" -- yes, and it should have been the design
from the start. `IndexFeed` was already subscribing to every enabled
index's SPOT token in LTP mode; this module's own spot subscription was
therefore a full second connection independently re-pulling the IDENTICAL
tick stream, and a single SmartWebSocketV2 connection already supports
subscribing multiple token sets at different modes at once (this module's
own futures-QUOTE-mode-alongside-spot-LTP-mode subscription already proved
that). So the two were merged: `IndexFeed` now owns the ONE WebSocket
connection and dispatches ticks to whichever consumers are registered --
`LiveFeedStore` for the dashboard (unchanged), and this module's
`ScalpBarAggregator` for Quick Scalp's bar construction (new). See
app/live_feed.py's own module docstring for the connection-management half
of this merge; what's left here is purely the tick-to-bar business logic,
now decoupled from any connection/reconnect concerns of its own.

The one real tradeoff named plainly rather than glossed over: the dashboard
price feed and Quick Scalp's entry-signal feed now share a single
connection and thread, where they were previously isolated -- a problem in
one's tick handling could in principle affect the other's. In practice this
risk is small: both `LiveFeedStore.update()` and `ScalpBarAggregator`'s own
tick handlers are already exception-safe (a bad tick logs and returns,
never raises), and `IndexFeed._handle_data` wraps the whole dispatch in its
own try/except regardless. What this merge actually buys back is real and
concrete: one persistent WebSocket connection instead of two on a
production box this project's own notes already document as memory-
constrained, and the elimination of a genuinely duplicated spot-tick
subscription that was pulling the same data twice.

WHAT THIS MODULE DOES AND DOES NOT DO
----------------------------------------
`ScalpBarAggregator` aggregates live ticks into 1-minute Bar objects
(app.market_data.Bar, the same type every other consumer in this codebase
already uses) and persists each completed bar via store_bars -- exactly
the same DB row a REST poll would have written, just sooner and closer to
the true tick data. All of the actual VWAP/sigma/RSI/setup-trigger MATH
still lives in app.quick_scalp's existing, already-tested pure functions
(_compute_vwap_bands, rsi, vwap_scalp_action), re-run against the DB's
stored session-to-date bars on every bar-close callback -- this
deliberately does NOT hand-roll a second, parallel incremental VWAP/RSI
implementation that could silently diverge from the canonical one every
other strategy in this app also reads. Once a bar closes, recomputing
VWAP/sigma/RSI over roughly 375 bars (a full session) is negligible work;
the spec's own "O(1) per bar" framing is a real, correct optimization
concern for a HIGH-frequency, per-TICK engine, not for something that only
needs to run once per completed BAR.

Position-level monitoring (the option-premium stop/target, the structural
index-level stop, the 3-minute breakeven-trail-or-scratch decision) is
DELIBERATELY left on app.quick_scalp's existing fast-poll scheduler job
(quick-scalp-exit-check, 5-second IntervalTrigger -- see app/scheduler.py),
not moved onto a second, dynamically-resubscribing option-contract WS
channel. This is a real, named departure from the spec's own
`on_option_tick` design: building a SECOND subscription-management system
(subscribe on entry, unsubscribe on exit, per open position) is materially
more moving parts for a decision that does not need tick-level precision --
a 5-second poll resolves it within 5 seconds of the true 180-second mark,
an error smaller than this strategy's own round-trip cost buffer. This also
directly matches an already-established precedent in this exact codebase:
app.validated_signal's own 5-second exit poll was built for the identical
reason.

NOT VERIFIED AGAINST THE REAL FEED
-------------------------------------
Same standing constraint as app.live_feed.IndexFeed (this sandbox has no
network path to Angel One): built directly against SmartWebSocketV2's
installed source (SmartApi==1.5.5), not tested against a live tick stream.
Assumptions specifically needing confirmation once deployed, beyond the two
IndexFeed's own docstring already names (LTP paise-scaling, reconnect
behaviour):

  1. `volume_trade_for_the_day` (QUOTE mode) is documented by its own name
     as a CUMULATIVE running total for the session, not a per-tick trade
     size -- this module computes per-minute volume as the delta between
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
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.market_data import ONE_MINUTE, Bar, store_bars
from app.time_utils import IST

logger = logging.getLogger(__name__)


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


class ScalpBarAggregator:
    """Pure tick-to-bar business logic, no WebSocket connection of its own
    -- app.live_feed.IndexFeed owns the single persistent connection and
    calls resolve_futures_tokens() once per connection attempt, then
    on_spot_tick()/on_futures_tick() per tick (see module docstring for why
    the two were merged 8 Sep 2026).

    `on_bar_closed(index_symbol)` is called synchronously, on IndexFeed's
    own background thread, once a completed bar has already been persisted
    via store_bars -- the callback's job is only to read that bar back
    (alongside the rest of today's session) and run the entry check; it
    should open and close its own DB session, the same convention every
    other scheduler-job-driven entry point in this codebase already
    follows."""

    def __init__(
        self,
        option_finder: Any,
        session_factory: Callable[[], Any],
        on_bar_closed: Callable[[str], None],
        indexes: list[Any],
    ) -> None:
        self._option_finder = option_finder
        self._session_factory = session_factory
        self._on_bar_closed = on_bar_closed
        self._indexes = list(indexes)
        self._lock = threading.Lock()
        self._forming: dict[str, _FormingBar] = {}
        self._minute_volume: dict[str, dict[int, float]] = {}
        self._last_futures_cum_volume: dict[str, float] = {}
        self.futures_token_to_symbol: dict[str, str] = {}

    def resolve_futures_tokens(self) -> list[str]:
        """Rebuilds the futures token map fresh -- IndexFeed calls this once
        per WebSocket connection attempt, since the near-month FUTIDX
        contract rolls at expiry and a stale token would silently stop
        producing volume ticks with no error."""
        futures_map: dict[str, str] = {}
        for index in self._indexes:
            try:
                contract = self._option_finder.find_current_futures_contract(index)
            except Exception as exc:
                logger.info(
                    "[QUICK_SCALP_FEED] %s: futures contract lookup failed (%s), volume will fall back to equal-weighted",
                    index.symbol, exc,
                )
                continue
            if contract is not None:
                futures_map[str(contract["symboltoken"])] = index.symbol
        self.futures_token_to_symbol = futures_map
        return list(futures_map)

    # -- tick handling ---------------------------------------------------

    def on_spot_tick(self, symbol: str, price: float, minute_bucket: int) -> None:
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

    def on_futures_tick(self, symbol: str, cumulative_volume: float | None, minute_bucket: int) -> None:
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
