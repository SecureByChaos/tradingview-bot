from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.platform as platform_module
from app.db_models import Base, IndexConfig, IndexPriceTick
from app.platform import get_index_live_figures
from app.time_utils import IST


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


class FakeSmartAPI:
    def __init__(self) -> None:
        self.calls = 0

    def get_index_spot(self, index) -> float:
        self.calls += 1
        return 12345.0


class FakeFeedStore:
    def __init__(self, entries: dict[str, dict] | None = None) -> None:
        self.entries = entries or {}

    def get(self, symbol: str):
        return self.entries.get(symbol)


def _seed_index(db: Session) -> None:
    db.add(
        IndexConfig(
            symbol="BANKNIFTY",
            display_name="Bank Nifty",
            enabled=True,
            spot_exchange="NSE",
            spot_symbol="Nifty Bank",
            spot_token="99926009",
        )
    )
    db.commit()


def test_uses_feed_store_price_without_calling_smartapi():
    db = _make_session()
    _seed_index(db)
    smartapi = FakeSmartAPI()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 50000.0, "is_live": True, "age_seconds": 0.2}})

    figures = get_index_live_figures(db, smartapi, feed_store)

    assert smartapi.calls == 0
    assert figures[0]["price"] == 50000.0
    assert figures[0]["is_live"] is True


def test_falls_back_to_smartapi_when_no_feed_store_given():
    db = _make_session()
    _seed_index(db)
    smartapi = FakeSmartAPI()

    figures = get_index_live_figures(db, smartapi, feed_store=None)

    assert smartapi.calls == 1
    assert figures[0]["price"] == 12345.0


def test_smartapi_exception_detail_is_not_returned_to_caller():
    """Regression test for CodeQL's "information exposure through an
    exception" finding on PR #9: str(exc) used to flow straight into this
    dict, which /api/live-dashboard returns verbatim as JSON."""

    class RaisingSmartAPI:
        def get_index_spot(self, index):
            raise RuntimeError("internal detail: raw broker response body, not for API clients")

    db = _make_session()
    _seed_index(db)

    figures = get_index_live_figures(db, RaisingSmartAPI(), feed_store=None)

    assert figures[0]["price"] is None
    assert "internal detail" not in figures[0]["error"]
    assert figures[0]["error"] == "Live price temporarily unavailable"


def test_does_not_call_smartapi_when_feed_store_present_but_has_no_entry_yet():
    db = _make_session()
    _seed_index(db)
    smartapi = FakeSmartAPI()
    feed_store = FakeFeedStore({})  # feed running, but no tick for this index yet

    figures = get_index_live_figures(db, smartapi, feed_store)

    assert smartapi.calls == 0
    assert figures[0]["price"] is None
    assert "error" in figures[0]


def test_stale_feed_entry_still_used_with_is_live_false():
    db = _make_session()
    _seed_index(db)
    smartapi = FakeSmartAPI()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 49000.0, "is_live": False, "age_seconds": 45.0}})

    figures = get_index_live_figures(db, smartapi, feed_store)

    assert smartapi.calls == 0  # never falls back to a fresh call on staleness
    assert figures[0]["price"] == 49000.0
    assert figures[0]["is_live"] is False


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_records_a_tick_during_trading_hours_on_a_weekday(monkeypatch):
    # 14 Aug 2026: this is the path dashboard polling drives, independent of
    # originator.py's own (already market-hours-gated) tick recording.
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))  # Thursday, trading hours
    db = _make_session()
    _seed_index(db)
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 50000.0, "is_live": True, "age_seconds": 0.2}})

    get_index_live_figures(db, FakeSmartAPI(), feed_store)

    ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    assert len(ticks) == 1


def test_does_not_record_a_tick_on_a_weekend(monkeypatch):
    # The confirmed 14 Aug report: dashboard polling kept writing a new
    # IndexPriceTick with the same frozen price every ~25s, purely because a
    # browser tab was open, even on a day the market never opened. The
    # figure itself must still render (last known price, via the feed) --
    # only the redundant write should stop.
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 15, 12, 0))  # Saturday
    db = _make_session()
    _seed_index(db)
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 50000.0, "is_live": False, "age_seconds": 999.0}})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    assert len(ticks) == 0
    assert figures[0]["price"] == 50000.0  # still shown, just not re-recorded


def test_does_not_record_a_tick_outside_trading_hours_on_a_weekday(monkeypatch):
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 20, 0))  # Thursday evening
    db = _make_session()
    _seed_index(db)
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 50000.0, "is_live": False, "age_seconds": 999.0}})

    get_index_live_figures(db, FakeSmartAPI(), feed_store)

    ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    assert len(ticks) == 0
