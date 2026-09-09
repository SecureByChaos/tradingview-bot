from __future__ import annotations

import logging

from app.config import Settings
from app.smartapi_client import SmartAPIClient, _DEFAULT_SPOT_FEED_FRESHNESS_SECONDS


def _make_client(**kwargs) -> SmartAPIClient:
    return SmartAPIClient(
        Settings(smartapi_api_key="x", smartapi_client_id="x", smartapi_pin="x", smartapi_totp_secret="x"),
        **kwargs,
    )


class FakeIndex:
    def __init__(self, symbol: str = "NIFTY", spot_token: str = "1") -> None:
        self.symbol = symbol
        self.spot_token = spot_token
        self.spot_exchange = "NSE"
        self.spot_symbol = "Nifty 50"


class FakeFeedStore:
    def __init__(self, entry: dict | None) -> None:
        self.entry = entry
        self.get_calls = 0

    def get(self, symbol: str):
        self.get_calls += 1
        return self.entry


def test_default_freshness_constant_is_3_seconds():
    assert _DEFAULT_SPOT_FEED_FRESHNESS_SECONDS == 3.0


def test_feed_store_none_falls_straight_through_to_rest(monkeypatch):
    client = _make_client()
    assert client.feed_store is None
    called = {"n": 0}

    def fake_get_ltp(exchange, tradingsymbol, symboltoken):
        called["n"] += 1
        return 24000.0

    monkeypatch.setattr(client, "get_ltp", fake_get_ltp)

    result = client.get_index_spot(FakeIndex())

    assert result == 24000.0
    assert called["n"] == 1


def test_uses_a_fresh_feed_entry_without_calling_rest(monkeypatch):
    client = _make_client()
    client.feed_store = FakeFeedStore({"price": 24555.5, "age_seconds": 1.0})

    def _exploding_get_ltp(*args, **kwargs):
        raise AssertionError("must not fall back to REST when the feed reading is fresh")

    monkeypatch.setattr(client, "get_ltp", _exploding_get_ltp)

    result = client.get_index_spot(FakeIndex())

    assert result == 24555.5


def test_falls_back_to_rest_when_feed_entry_is_older_than_the_freshness_window(monkeypatch):
    client = _make_client()
    client.feed_store = FakeFeedStore({"price": 24555.5, "age_seconds": 3.5})  # 3.5s >= default 3.0s window
    called = {"n": 0}

    def fake_get_ltp(exchange, tradingsymbol, symboltoken):
        called["n"] += 1
        return 24600.0

    monkeypatch.setattr(client, "get_ltp", fake_get_ltp)

    result = client.get_index_spot(FakeIndex())

    assert result == 24600.0
    assert called["n"] == 1


def test_falls_back_to_rest_when_feed_has_no_entry_yet(monkeypatch):
    client = _make_client()
    client.feed_store = FakeFeedStore(None)
    monkeypatch.setattr(client, "get_ltp", lambda *a, **k: 24700.0)

    result = client.get_index_spot(FakeIndex())

    assert result == 24700.0


def test_freshness_window_is_configurable(monkeypatch):
    # A wider configured window accepts an entry the default 3s window
    # would have rejected.
    client = _make_client(spot_feed_freshness_seconds=10.0)
    client.feed_store = FakeFeedStore({"price": 24555.5, "age_seconds": 8.0})

    def _exploding_get_ltp(*args, **kwargs):
        raise AssertionError("must not fall back to REST -- 8s is fresh under a 10s window")

    monkeypatch.setattr(client, "get_ltp", _exploding_get_ltp)

    result = client.get_index_spot(FakeIndex())

    assert result == 24555.5


def test_boundary_age_exactly_at_the_freshness_window_is_not_fresh(monkeypatch):
    # Strict `<`, matching every other freshness/threshold convention in
    # this codebase (e.g. the chop-gate floor).
    client = _make_client()
    client.feed_store = FakeFeedStore({"price": 24555.5, "age_seconds": 3.0})
    monkeypatch.setattr(client, "get_ltp", lambda *a, **k: 24600.0)

    result = client.get_index_spot(FakeIndex())

    assert result == 24600.0


def test_logs_at_debug_which_source_was_used(monkeypatch, caplog):
    client = _make_client()
    client.feed_store = FakeFeedStore({"price": 24555.5, "age_seconds": 1.0})

    with caplog.at_level(logging.DEBUG):
        client.get_index_spot(FakeIndex())

    assert any("[SPOT]" in r.message and "LiveFeedStore" in r.message for r in caplog.records)


def test_logs_at_debug_when_falling_back_to_rest(monkeypatch, caplog):
    client = _make_client()
    monkeypatch.setattr(client, "get_ltp", lambda *a, **k: 24000.0)

    with caplog.at_level(logging.DEBUG):
        client.get_index_spot(FakeIndex())

    assert any("[SPOT]" in r.message and "REST" in r.message for r in caplog.records)


def test_missing_spot_token_still_raises_before_touching_the_feed():
    client = _make_client()
    client.feed_store = FakeFeedStore({"price": 1.0, "age_seconds": 0.0})
    import pytest
    from app.smartapi_client import SmartAPIError

    with pytest.raises(SmartAPIError):
        client.get_index_spot(FakeIndex(spot_token=""))
