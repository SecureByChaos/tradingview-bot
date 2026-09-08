from __future__ import annotations

import sys
import types
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db_models import Base
from app.market_data import ONE_MINUTE, load_bars
from app.quick_scalp_feed import QuickScalpFeed, _minute_bucket_to_ts_ist


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


class FakeOptionFinder:
    def __init__(self, futures_by_symbol: dict | None = None) -> None:
        self.futures_by_symbol = futures_by_symbol or {}

    def find_current_futures_contract(self, index):
        if index.symbol not in self.futures_by_symbol:
            return None
        return self.futures_by_symbol[index.symbol]


class _ExplodingOptionFinder:
    def find_current_futures_contract(self, index):
        raise RuntimeError("instrument master unavailable")


NIFTY = FakeIndex("NIFTY", "NSE", "26000")


def _shared_session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return engine, lambda: Session(engine)


def _make_feed(session_factory=None, option_finder=None, on_bar_closed=None, indexes=None):
    return QuickScalpFeed(
        FakeSmartAPIClient(), option_finder or FakeOptionFinder(), session_factory or (lambda: Session()),
        on_bar_closed or (lambda symbol: None), indexes if indexes is not None else [NIFTY],
    )


# ---------------------------------------------------------------------------
# _minute_bucket_to_ts_ist
# ---------------------------------------------------------------------------

def test_minute_bucket_to_ts_ist_floors_to_the_minute():
    # 2026-09-08 05:30:00 UTC == 11:00:00 IST.
    from datetime import timezone

    dt_utc = datetime(2026, 9, 8, 5, 30, tzinfo=timezone.utc)
    bucket = int(dt_utc.timestamp() // 60)
    result = _minute_bucket_to_ts_ist(bucket)
    assert result.tzinfo is None
    assert (result.hour, result.minute, result.second) == (11, 0, 0)


# ---------------------------------------------------------------------------
# tick aggregation -> bar finalization
# ---------------------------------------------------------------------------

def test_spot_tick_starts_a_forming_bar_without_finalizing_anything():
    closed = []
    feed = _make_feed(on_bar_closed=lambda symbol: closed.append(symbol))
    feed._on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    assert "NIFTY" in feed._forming
    assert closed == []


def test_spot_tick_updates_high_low_close_within_the_same_minute():
    feed = _make_feed()
    feed._on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    feed._on_spot_tick("NIFTY", 24010.0, minute_bucket=1000)
    feed._on_spot_tick("NIFTY", 23990.0, minute_bucket=1000)
    feed._on_spot_tick("NIFTY", 24005.0, minute_bucket=1000)
    forming = feed._forming["NIFTY"]
    assert forming.open == 24000.0
    assert forming.high == 24010.0
    assert forming.low == 23990.0
    assert forming.close == 24005.0


def test_minute_rollover_finalizes_and_persists_the_completed_bar():
    engine, factory = _shared_session_factory()
    closed_symbols = []
    feed = _make_feed(session_factory=factory, on_bar_closed=lambda symbol: closed_symbols.append(symbol))

    feed._on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    feed._on_spot_tick("NIFTY", 24010.0, minute_bucket=1000)
    feed._on_spot_tick("NIFTY", 24005.0, minute_bucket=1001)  # rolls over

    assert closed_symbols == ["NIFTY"]
    with Session(engine) as db:
        bars = load_bars(db, "NIFTY", ONE_MINUTE)
        assert len(bars) == 1
        assert bars[0].open == 24000.0
        assert bars[0].high == 24010.0
        assert bars[0].close == 24010.0  # last tick BEFORE rollover
    # A new forming bar started at the new tick's price.
    assert feed._forming["NIFTY"].open == 24005.0


def test_minute_rollover_attaches_the_accumulated_futures_volume():
    engine, factory = _shared_session_factory()
    feed = _make_feed(session_factory=factory)

    # Two futures ticks land inside minute 1000 -- 10 units of real delta volume.
    feed._on_futures_tick("NIFTY", 500.0, minute_bucket=1000)  # first reading, no baseline yet
    feed._on_futures_tick("NIFTY", 510.0, minute_bucket=1000)  # delta 10
    feed._on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    feed._on_spot_tick("NIFTY", 24005.0, minute_bucket=1001)  # finalizes minute 1000

    with Session(engine) as db:
        bars = load_bars(db, "NIFTY", ONE_MINUTE)
        assert bars[0].volume == 10.0


def test_futures_volume_delta_clamped_at_zero_on_a_decrease():
    feed = _make_feed()
    feed._on_futures_tick("NIFTY", 500.0, minute_bucket=1000)
    feed._on_futures_tick("NIFTY", 480.0, minute_bucket=1000)  # a decrease -- session reset / stale reading
    assert feed._minute_volume.get("NIFTY", {}).get(1000, 0.0) == 0.0


def test_futures_tick_with_no_volume_field_is_a_no_op():
    feed = _make_feed()
    feed._on_futures_tick("NIFTY", None, minute_bucket=1000)
    assert feed._minute_volume.get("NIFTY", {}) == {}


def test_bar_without_any_futures_volume_persists_with_zero_volume():
    engine, factory = _shared_session_factory()
    feed = _make_feed(session_factory=factory)
    feed._on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    feed._on_spot_tick("NIFTY", 24005.0, minute_bucket=1001)

    with Session(engine) as db:
        bars = load_bars(db, "NIFTY", ONE_MINUTE)
        assert bars[0].volume == 0.0


def test_finalize_bar_swallows_persistence_failures():
    def _exploding_factory():
        raise RuntimeError("db unavailable")

    feed = _make_feed(session_factory=_exploding_factory)
    from app.market_data import Bar
    feed._finalize_bar("NIFTY", Bar(ts_ist=datetime(2026, 9, 8, 11, 0), open=1, high=1, low=1, close=1))  # must not raise


def test_finalize_bar_swallows_callback_failures():
    engine, factory = _shared_session_factory()

    def _exploding_callback(symbol):
        raise RuntimeError("entry check blew up")

    feed = _make_feed(session_factory=factory, on_bar_closed=_exploding_callback)
    from app.market_data import Bar
    feed._finalize_bar("NIFTY", Bar(ts_ist=datetime(2026, 9, 8, 11, 0), open=1, high=1, low=1, close=1))  # must not raise
    with Session(engine) as db:
        assert len(load_bars(db, "NIFTY", ONE_MINUTE)) == 1  # the bar itself was still persisted


# ---------------------------------------------------------------------------
# _handle_data routing
# ---------------------------------------------------------------------------

def test_handle_data_routes_spot_and_futures_ticks_separately():
    feed = _make_feed()
    feed._spot_token_to_symbol = {"26000": "NIFTY"}
    feed._futures_token_to_symbol = {"999": "NIFTY"}

    feed._handle_data(None, {"token": "26000", "last_traded_price": 2400000})
    assert "NIFTY" in feed._forming

    feed._handle_data(None, {"token": "999", "last_traded_price": 2400000, "volume_trade_for_the_day": 100})
    assert feed._last_futures_cum_volume.get("NIFTY") == 100.0


def test_handle_data_ignores_unknown_token():
    feed = _make_feed()
    feed._spot_token_to_symbol = {"26000": "NIFTY"}
    feed._handle_data(None, {"token": "99999999", "last_traded_price": 12345})
    assert feed._forming == {}


def test_handle_data_tolerates_malformed_message():
    feed = _make_feed()
    feed._handle_data(None, {})
    feed._handle_data(None, {"token": "26000"})  # missing price


# ---------------------------------------------------------------------------
# _resolve_tokens
# ---------------------------------------------------------------------------

def test_resolve_tokens_builds_both_spot_and_futures_maps():
    option_finder = FakeOptionFinder({"NIFTY": {"exchange": "NFO", "tradingsymbol": "NIFTY28SEP26FUT", "symboltoken": "555"}})
    feed = _make_feed(option_finder=option_finder)
    spot_tokens, futures_tokens = feed._resolve_tokens()
    assert spot_tokens == ["26000"]
    assert futures_tokens == ["555"]
    assert feed._futures_token_to_symbol == {"555": "NIFTY"}


def test_resolve_tokens_degrades_gracefully_when_futures_lookup_fails():
    feed = _make_feed(option_finder=_ExplodingOptionFinder())
    spot_tokens, futures_tokens = feed._resolve_tokens()
    assert spot_tokens == ["26000"]
    assert futures_tokens == []


def test_resolve_tokens_excludes_an_index_with_no_futures_contract():
    feed = _make_feed(option_finder=FakeOptionFinder({}))
    _, futures_tokens = feed._resolve_tokens()
    assert futures_tokens == []


# ---------------------------------------------------------------------------
# start/stop
# ---------------------------------------------------------------------------

def test_start_does_nothing_with_no_configured_indexes():
    feed = _make_feed(indexes=[])
    feed.start()
    assert feed._thread is None


def test_stop_before_start_does_not_raise():
    feed = _make_feed()
    feed.stop()


# ---------------------------------------------------------------------------
# _run outer loop -- mirrors app.live_feed's own test shape for IndexFeed
# ---------------------------------------------------------------------------

def test_run_skips_connection_attempt_when_market_closed():
    feed = _make_feed()
    fake_module = types.SimpleNamespace(SmartWebSocketV2=lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("must not attempt to connect while market is closed")
    ))
    sleep_calls: list[float] = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            feed._stop_requested = True

    with patch.dict(sys.modules, {"SmartApi.smartWebSocketV2": fake_module}), \
         patch("app.quick_scalp_feed.time.sleep", side_effect=fake_sleep), \
         patch("app.quick_scalp_feed.check_market_hours", return_value="a Saturday (market closed)"):
        feed._run()

    assert sleep_calls == [300.0, 300.0]


def test_run_waits_for_auth_then_subscribes_both_spot_and_futures():
    option_finder = FakeOptionFinder({"NIFTY": {"exchange": "NFO", "tradingsymbol": "NIFTY28SEP26FUT", "symboltoken": "555"}})
    client = FakeSmartAPIClient(jwt_token=None, feed_token=None)
    feed = QuickScalpFeed(client, option_finder, lambda: Session(create_engine("sqlite://")), lambda s: None, [NIFTY])

    subscriptions = []

    class FakeWS:
        def __init__(self, **kwargs):
            self.on_open = None
            self.on_data = None
            self.on_error = None
            self.on_close = None

        def subscribe(self, correlation_id, mode, token_list):
            subscriptions.append((correlation_id, mode, token_list))

        def connect(self):
            self.on_open(self)
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
            feed._stop_requested = True

    with patch.dict(sys.modules, {"SmartApi.smartWebSocketV2": fake_module}), \
         patch("app.quick_scalp_feed.time.sleep", side_effect=fake_sleep), \
         patch("app.quick_scalp_feed.check_market_hours", return_value=None):
        feed._run()

    assert len(subscriptions) == 2
    modes = {mode for _, mode, _ in subscriptions}
    assert modes == {1, 2}  # LTP for spot, QUOTE for futures
    spot_sub = next(s for s in subscriptions if s[1] == 1)
    futures_sub = next(s for s in subscriptions if s[1] == 2)
    assert spot_sub[2] == [{"exchangeType": 1, "tokens": ["26000"]}]
    assert futures_sub[2] == [{"exchangeType": 2, "tokens": ["555"]}]
