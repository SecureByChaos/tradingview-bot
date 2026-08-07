from __future__ import annotations

import sys
import types
from unittest.mock import patch

from app.live_feed import IndexFeed, LiveFeedStore, _PAISE_PER_RUPEE, _STALE_AFTER_SECONDS


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
         patch("app.live_feed.time.sleep", side_effect=fake_sleep):
        feed._run()

    assert attempts["n"] == 1
    assert sleep_calls["n"] == 1  # only the initial "waiting for auth" sleep
    entry = store.get("BANKNIFTY")
    assert entry["price"] == 50000.0
    assert entry["is_live"] is False  # on_close fired before connect() returned
