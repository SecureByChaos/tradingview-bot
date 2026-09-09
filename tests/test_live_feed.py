from __future__ import annotations

import sys
import types
from unittest.mock import patch

from app.live_feed import IndexFeed, LiveFeedStore, resolve_spot_for_exit_check, _PAISE_PER_RUPEE, _STALE_AFTER_SECONDS


class FakeIndex:
    def __init__(self, symbol: str, spot_exchange: str, spot_token: str) -> None:
        self.symbol = symbol
        self.spot_exchange = spot_exchange
        self.spot_token = spot_token


class FakeSettings:
    smartapi_api_key = "key"
    smartapi_client_id = "client"


class FakeSmartAPIClient:
    def __init__(self, jwt_token: str | None = "jwt", feed_token: str | None = "feed") -> None:
        self.jwt_token = jwt_token
        self.feed_token = feed_token
        self.settings = FakeSettings()


BANKNIFTY = FakeIndex("BANKNIFTY", "NSE", "99926009")
NIFTY = FakeIndex("NIFTY", "NSE", "99926000")


def test_store_returns_none_before_any_update():
    store = LiveFeedStore()
    assert store.get("BANKNIFTY") is None


def test_store_returns_price_and_is_live_true_when_connected_and_fresh():
    store = LiveFeedStore()
    store.mark_connected(True)
    store.update("BANKNIFTY", 50000.0)
    entry = store.get("BANKNIFTY")
    assert entry["price"] == 50000.0
    assert entry["is_live"] is True
    assert entry["age_seconds"] < 1.0


def test_store_is_live_false_when_not_connected_even_if_recent():
    store = LiveFeedStore()
    store.update("BANKNIFTY", 50000.0)  # never marked connected
    entry = store.get("BANKNIFTY")
    assert entry["price"] == 50000.0
    assert entry["is_live"] is False


def test_store_is_live_false_when_stale_even_if_still_connected():
    store = LiveFeedStore()
    store.mark_connected(True)
    store.update("BANKNIFTY", 50000.0)
    # Force staleness without a real sleep.
    store._entries["BANKNIFTY"].updated_monotonic -= _STALE_AFTER_SECONDS + 1
    entry = store.get("BANKNIFTY")
    assert entry["price"] == 50000.0  # last-known value still served
    assert entry["is_live"] is False


def test_token_list_groups_by_exchange_and_symbol_map():
    feed = IndexFeed(FakeSmartAPIClient(), LiveFeedStore(), [BANKNIFTY, NIFTY])
    assert feed._token_list == [{"exchangeType": 1, "tokens": ["99926009", "99926000"]}]
    assert feed._token_to_symbol == {"99926009": "BANKNIFTY", "99926000": "NIFTY"}


def test_indexes_without_spot_token_are_excluded():
    incomplete = FakeIndex("SENSEX", "", "")
    feed = IndexFeed(FakeSmartAPIClient(), LiveFeedStore(), [BANKNIFTY, incomplete])
    assert feed._token_to_symbol == {"99926009": "BANKNIFTY"}


def test_handle_data_updates_store_with_paise_to_rupee_conversion():
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])
    feed._handle_data(None, {"token": "99926009", "last_traded_price": 5000000})
    entry = store.get("BANKNIFTY")
    assert entry["price"] == 5000000 / _PAISE_PER_RUPEE == 50000.0


def test_handle_data_ignores_unknown_token():
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])
    feed._handle_data(None, {"token": "99999999", "last_traded_price": 12345})
    assert store.get("BANKNIFTY") is None


def test_handle_data_tolerates_malformed_message():
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])
    feed._handle_data(None, {})  # no token, no price -- must not raise
    feed._handle_data(None, {"token": "99926009"})  # missing price -- must not raise
    assert store.get("BANKNIFTY") is None


def test_handle_open_marks_connected_and_subscribes():
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])

    subscribed = {}

    class FakeWS:
        def subscribe(self, correlation_id, mode, token_list):
            subscribed["args"] = (correlation_id, mode, token_list)

    feed._ws = FakeWS()
    feed._handle_open(None)
    assert store.get("BANKNIFTY") is None  # no price yet, but connected
    assert store._connected is True
    assert subscribed["args"][2] == [{"exchangeType": 1, "tokens": ["99926009"]}]


def test_handle_error_and_close_mark_disconnected():
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])
    store.mark_connected(True)
    feed._handle_error("Max retry attempt reached", "Connection closed")
    assert store._connected is False

    store.mark_connected(True)
    feed._handle_close(None)
    assert store._connected is False


def test_handle_error_tolerates_zero_args():
    # Base SmartWebSocketV2.on_error(self) takes no args at all in some
    # paths -- must not raise if called that way either.
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])
    feed._handle_error()
    assert store._connected is False


def test_start_does_nothing_with_no_configured_indexes():
    feed = IndexFeed(FakeSmartAPIClient(), LiveFeedStore(), [])
    feed.start()
    assert feed._thread is None


def test_stop_before_start_does_not_raise():
    feed = IndexFeed(FakeSmartAPIClient(), LiveFeedStore(), [BANKNIFTY])
    feed.stop()  # no thread, no ws -- must be a safe no-op


def test_run_waits_when_tokens_missing_then_connects_once_available():
    """Exercises _run's actual outer loop -- the untested-by-the-above-tests
    part -- against a fake SmartWebSocketV2, without any real network access
    or a real time.sleep. Starts with no jwt/feed token: the loop must wait
    rather than crash. The mocked sleep supplies the tokens on its first call
    (standing in for SmartAPIClient's own background re-auth completing) so
    the loop's next iteration takes the connect path; the fake connect()
    simulates one tick then closing, and requests the feed stop so this
    (otherwise infinite) loop is guaranteed to terminate within the test."""
    store = LiveFeedStore()
    client = FakeSmartAPIClient(jwt_token=None, feed_token=None)
    feed = IndexFeed(client, store, [BANKNIFTY])

    attempts = {"n": 0}

    class FakeWS:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.on_open = None
            self.on_data = None
            self.on_error = None
            self.on_close = None

        def connect(self):
            attempts["n"] += 1
            self.on_open(self)
            self.on_data(self, {"token": "99926009", "last_traded_price": 5000000})
            self.on_close(self)
            feed._stop_requested = True

        def close_connection(self):
            pass

    fake_module = types.SimpleNamespace(SmartWebSocketV2=FakeWS)
    sleep_calls = {"n": 0}

    def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 1:
            client.jwt_token = "jwt"
            client.feed_token = "feed"
        elif sleep_calls["n"] > 5:
            # Safety net: if the loop somehow didn't terminate via connect()
            # above, force it to rather than hang the test suite.
            feed._stop_requested = True

    with patch.dict(sys.modules, {"SmartApi.smartWebSocketV2": fake_module}), \
         patch("app.live_feed.time.sleep", side_effect=fake_sleep), \
         patch("app.live_feed.check_market_hours", return_value=None):  # market open
        feed._run()

    assert attempts["n"] == 1
    assert sleep_calls["n"] == 1  # only the initial "waiting for auth" sleep
    entry = store.get("BANKNIFTY")
    assert entry["price"] == 50000.0
    assert entry["is_live"] is False  # on_close fired before connect() returned


def test_run_skips_connection_attempt_when_market_closed():
    # 17 Aug 2026: this thread has its own market-hours gate now (previously
    # it dialed Angel's WS endpoint every _RECONNECT_DELAY_SECONDS all night
    # and every weekend). Market closed for the whole loop -- must never
    # reach SmartWebSocketV2 at all, and must sleep the longer closed-market
    # interval, not the open-market reconnect delay.
    store = LiveFeedStore()
    client = FakeSmartAPIClient()
    feed = IndexFeed(client, store, [BANKNIFTY])

    fake_module = types.SimpleNamespace(SmartWebSocketV2=lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("must not attempt to connect while market is closed")
    ))
    sleep_calls: list[float] = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            feed._stop_requested = True

    with patch.dict(sys.modules, {"SmartApi.smartWebSocketV2": fake_module}), \
         patch("app.live_feed.time.sleep", side_effect=fake_sleep), \
         patch("app.live_feed.check_market_hours", return_value="a Saturday (market closed)"):
        feed._run()

    assert sleep_calls == [300.0, 300.0]
    assert store.get("BANKNIFTY") is None


class FakeScalpAggregator:
    """8 Sep 2026: IndexFeed and app.quick_scalp_feed's own WebSocket
    connection were merged (see IndexFeed's module docstring) -- these
    tests exercise the dispatch/subscription side of that merge without
    depending on app.quick_scalp_feed's own real ScalpBarAggregator
    internals, which are tested separately in test_quick_scalp_feed.py."""

    def __init__(self, futures_tokens: list[str] | None = None) -> None:
        self.futures_tokens = futures_tokens or []
        self.futures_token_to_symbol = {tok: "NIFTY" for tok in self.futures_tokens}
        self.spot_ticks: list[tuple[str, float, int]] = []
        self.futures_ticks: list[tuple[str, float | None, int]] = []

    def resolve_futures_tokens(self) -> list[str]:
        return self.futures_tokens

    def on_spot_tick(self, symbol: str, price: float, minute_bucket: int) -> None:
        self.spot_ticks.append((symbol, price, minute_bucket))

    def on_futures_tick(self, symbol: str, cumulative_volume, minute_bucket: int) -> None:
        self.futures_ticks.append((symbol, cumulative_volume, minute_bucket))


def test_handle_data_dispatches_spot_ticks_to_both_store_and_aggregator():
    store = LiveFeedStore()
    aggregator = FakeScalpAggregator()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY], scalp_aggregator=aggregator)

    feed._handle_data(None, {"token": "99926009", "last_traded_price": 5000000})

    assert store.get("BANKNIFTY")["price"] == 50000.0
    assert aggregator.spot_ticks == [("BANKNIFTY", 50000.0, aggregator.spot_ticks[0][2])]


def test_handle_data_routes_futures_ticks_only_to_the_aggregator():
    store = LiveFeedStore()
    aggregator = FakeScalpAggregator(futures_tokens=["555"])
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY], scalp_aggregator=aggregator)

    feed._handle_data(None, {"token": "555", "last_traded_price": 5000000, "volume_trade_for_the_day": 42})

    assert aggregator.futures_ticks == [("NIFTY", 42, aggregator.futures_ticks[0][2])]
    assert store.get("BANKNIFTY") is None  # never touched by a futures tick


def test_handle_data_with_no_aggregator_behaves_exactly_as_before():
    # Every existing dashboard-only deployment path (scalp_aggregator=None)
    # must be completely unaffected by the merge.
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])
    feed._handle_data(None, {"token": "99926009", "last_traded_price": 5000000})
    assert store.get("BANKNIFTY")["price"] == 50000.0


def test_handle_open_subscribes_both_spot_and_futures_when_aggregator_present():
    store = LiveFeedStore()
    aggregator = FakeScalpAggregator(futures_tokens=["555"])
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY], scalp_aggregator=aggregator)
    feed._futures_tokens = ["555"]

    subscriptions = []

    class FakeWS:
        def subscribe(self, correlation_id, mode, token_list):
            subscriptions.append((correlation_id, mode, token_list))

    feed._ws = FakeWS()
    feed._handle_open(None)

    assert len(subscriptions) == 2
    modes = {mode for _, mode, _ in subscriptions}
    assert modes == {1, 2}
    futures_sub = next(s for s in subscriptions if s[1] == 2)
    assert futures_sub[2] == [{"exchangeType": 2, "tokens": ["555"]}]


def test_handle_open_subscribes_only_spot_without_an_aggregator():
    store = LiveFeedStore()
    feed = IndexFeed(FakeSmartAPIClient(), store, [BANKNIFTY])

    subscriptions = []

    class FakeWS:
        def subscribe(self, correlation_id, mode, token_list):
            subscriptions.append((correlation_id, mode, token_list))

    feed._ws = FakeWS()
    feed._handle_open(None)

    assert len(subscriptions) == 1


def test_run_resolves_futures_tokens_fresh_each_connection_attempt():
    store = LiveFeedStore()
    aggregator = FakeScalpAggregator(futures_tokens=["555"])
    client = FakeSmartAPIClient()
    feed = IndexFeed(client, store, [BANKNIFTY], scalp_aggregator=aggregator)

    resolve_calls = {"n": 0}
    real_resolve = aggregator.resolve_futures_tokens

    def _counting_resolve():
        resolve_calls["n"] += 1
        return real_resolve()

    aggregator.resolve_futures_tokens = _counting_resolve

    class FakeWS:
        def __init__(self, **kwargs):
            self.on_open = None
            self.on_data = None
            self.on_error = None
            self.on_close = None

        def subscribe(self, *_args, **_kwargs):
            pass

        def connect(self):
            self.on_close(self)
            feed._stop_requested = True

        def close_connection(self):
            pass

    fake_module = types.SimpleNamespace(SmartWebSocketV2=FakeWS)

    with patch.dict(sys.modules, {"SmartApi.smartWebSocketV2": fake_module}), \
         patch("app.live_feed.time.sleep"), \
         patch("app.live_feed.check_market_hours", return_value=None):
        feed._run()

    assert resolve_calls["n"] == 1
    assert feed._futures_tokens == ["555"]


def test_run_resumes_connecting_once_market_reopens():
    store = LiveFeedStore()
    client = FakeSmartAPIClient()
    feed = IndexFeed(client, store, [BANKNIFTY])

    attempts = {"n": 0}

    class FakeWS:
        def __init__(self, **kwargs):
            self.on_open = None
            self.on_data = None
            self.on_error = None
            self.on_close = None

        def connect(self):
            attempts["n"] += 1
            self.on_close(self)
            feed._stop_requested = True

        def close_connection(self):
            pass

    fake_module = types.SimpleNamespace(SmartWebSocketV2=FakeWS)
    # Closed on the first check, open on the second -- the loop must notice
    # the transition and proceed to actually connect.
    market_hours_results = iter(["a Saturday (market closed)", None])

    with patch.dict(sys.modules, {"SmartApi.smartWebSocketV2": fake_module}), \
         patch("app.live_feed.time.sleep"), \
         patch("app.live_feed.check_market_hours", side_effect=lambda _now: next(market_hours_results)):
        feed._run()

    assert attempts["n"] == 1


# ---------------------------------------------------------------------------
# resolve_spot_for_exit_check (9 Sep 2026, Phase 2b of the "portal
# unresponsive during market hours" investigation)
# ---------------------------------------------------------------------------

class _FakeFeedStoreForResolve:
    def __init__(self, entry: dict | None) -> None:
        self.entry = entry

    def get(self, symbol: str):
        return self.entry


class _FakeSmartAPIForResolve:
    def __init__(self, spot: float = 24000.0, raises: bool = False) -> None:
        self.spot = spot
        self.raises = raises
        self.calls = 0

    def get_index_spot(self, index):
        self.calls += 1
        if self.raises:
            raise RuntimeError("broker error")
        return self.spot


def test_resolve_spot_uses_fresh_feed_entry_without_calling_smartapi():
    feed_store = _FakeFeedStoreForResolve({"price": 24500.0, "is_live": True})
    smartapi = _FakeSmartAPIForResolve()

    result = resolve_spot_for_exit_check(NIFTY, smartapi, feed_store)

    assert result == 24500.0
    assert smartapi.calls == 0


def test_resolve_spot_falls_back_to_rest_when_feed_entry_is_stale():
    feed_store = _FakeFeedStoreForResolve({"price": 24500.0, "is_live": False})
    smartapi = _FakeSmartAPIForResolve(spot=24600.0)

    result = resolve_spot_for_exit_check(NIFTY, smartapi, feed_store)

    assert result == 24600.0
    assert smartapi.calls == 1


def test_resolve_spot_falls_back_to_rest_when_feed_has_no_entry_yet():
    smartapi = _FakeSmartAPIForResolve(spot=24700.0)

    result = resolve_spot_for_exit_check(NIFTY, smartapi, _FakeFeedStoreForResolve(None))

    assert result == 24700.0
    assert smartapi.calls == 1


def test_resolve_spot_falls_back_to_rest_when_feed_store_is_none():
    smartapi = _FakeSmartAPIForResolve(spot=24800.0)

    result = resolve_spot_for_exit_check(NIFTY, smartapi, None)

    assert result == 24800.0
    assert smartapi.calls == 1


def test_resolve_spot_falls_back_to_the_stale_feed_price_when_rest_also_fails():
    feed_store = _FakeFeedStoreForResolve({"price": 24500.0, "is_live": False})
    smartapi = _FakeSmartAPIForResolve(raises=True)

    result = resolve_spot_for_exit_check(NIFTY, smartapi, feed_store)

    assert result == 24500.0


def test_resolve_spot_returns_none_when_nothing_is_available():
    smartapi = _FakeSmartAPIForResolve(raises=True)

    result = resolve_spot_for_exit_check(NIFTY, smartapi, _FakeFeedStoreForResolve(None))

    assert result is None
