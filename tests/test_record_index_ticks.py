from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.platform as platform_module
from app.db_models import Base, IndexConfig, IndexPriceTick
from app.platform import record_index_ticks
from app.time_utils import IST, utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


class FakeSmartAPI:
    def __init__(self, spot: float = 12345.0) -> None:
        self.spot = spot
        self.calls: list[str] = []

    def get_index_spot(self, index) -> float:
        self.calls.append(index.symbol)
        return self.spot


class _ExplodingSmartAPI:
    def get_index_spot(self, index):
        raise AssertionError("must not fetch spot when feed_store already has a value")


class FakeFeedStore:
    def __init__(self, entries: dict[str, dict] | None = None) -> None:
        self.entries = entries or {}

    def get(self, symbol: str):
        return self.entries.get(symbol)


def _seed_index(db: Session, symbol: str = "BANKNIFTY") -> None:
    db.add(IndexConfig(symbol=symbol, display_name=symbol, enabled=True, spot_exchange="NSE", spot_token="1"))
    db.commit()


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_skips_entirely_when_market_is_closed(monkeypatch):
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 15, 12, 0))  # Saturday
    db = _make_session()
    _seed_index(db)

    record_index_ticks(_ExplodingSmartAPI(), FakeFeedStore({"BANKNIFTY": {"price": 50000.0, "is_live": True}}), db=db)

    ticks = list(db.scalars(select(IndexPriceTick)))
    assert ticks == []


def test_skips_when_no_indexes_are_enabled(monkeypatch):
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))  # Thursday, trading hours
    db = _make_session()

    record_index_ticks(_ExplodingSmartAPI(), FakeFeedStore(), db=db)

    ticks = list(db.scalars(select(IndexPriceTick)))
    assert ticks == []


def test_prefers_feed_store_price_without_calling_smartapi(monkeypatch):
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))
    db = _make_session()
    _seed_index(db)
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 50123.45, "is_live": True}})

    record_index_ticks(_ExplodingSmartAPI(), feed_store, db=db)

    ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    assert len(ticks) == 1
    assert ticks[0].price == 50123.45


def test_falls_back_to_smartapi_when_feed_store_is_none(monkeypatch):
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))
    db = _make_session()
    _seed_index(db)
    smartapi = FakeSmartAPI(spot=57777.0)

    record_index_ticks(smartapi, None, db=db)

    assert smartapi.calls == ["BANKNIFTY"]
    ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    assert len(ticks) == 1
    assert ticks[0].price == 57777.0


def test_skips_an_index_with_no_feed_entry_yet_rather_than_calling_smartapi(monkeypatch):
    # feed_store is wired (not None) but has no reading for this index yet --
    # same fail-soft skip get_index_live_figures's own fallback path uses,
    # not a reason to fall back to a fresh REST call.
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))
    db = _make_session()
    _seed_index(db)

    record_index_ticks(_ExplodingSmartAPI(), FakeFeedStore({}), db=db)

    ticks = list(db.scalars(select(IndexPriceTick)))
    assert ticks == []


def test_smartapi_failure_on_one_index_does_not_block_others(monkeypatch):
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))
    db = _make_session()
    _seed_index(db, "BANKNIFTY")
    _seed_index(db, "NIFTY")

    class _PartialFailureSmartAPI:
        def get_index_spot(self, index):
            if index.symbol == "BANKNIFTY":
                raise RuntimeError("broker error")
            return 24000.0

    record_index_ticks(_PartialFailureSmartAPI(), None, db=db)

    bn_ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    nifty_ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "NIFTY")))
    assert bn_ticks == []
    assert len(nifty_ticks) == 1
    assert nifty_ticks[0].price == 24000.0


def test_respects_the_existing_throttle_on_a_second_call(monkeypatch):
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))
    db = _make_session()
    _seed_index(db)
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 50000.0, "is_live": True}})

    record_index_ticks(_ExplodingSmartAPI(), feed_store, db=db)
    feed_store.entries["BANKNIFTY"]["price"] = 50001.0
    record_index_ticks(_ExplodingSmartAPI(), feed_store, db=db)

    ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    assert len(ticks) == 1  # the second call landed inside the 25s throttle window


def test_does_not_hold_the_db_session_across_the_price_fetch(monkeypatch):
    """The whole point of this job: unlike the old inline dashboard-poll
    write, the session used to look up enabled indexes is fully closed
    before any price is fetched. Simulated here with an owns_session=True
    call (db=None) and a SessionLocal stand-in whose sessions record their
    own close() calls."""
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    seed = Session(engine)
    _seed_index(seed)
    seed.close()

    closed_flags: list[list[bool]] = []

    def _session_factory():
        session = Session(engine)
        flag = [False]
        closed_flags.append(flag)
        original_close = session.close

        def _tracking_close():
            flag[0] = True
            original_close()

        session.close = _tracking_close
        return session

    monkeypatch.setattr(platform_module, "SessionLocal", _session_factory)

    fetch_time_first_session_closed: list[bool] = []

    class _TrackingSmartAPI:
        def get_index_spot(self, index):
            # At fetch time, the FIRST session (used only to list enabled
            # indexes) must already be closed.
            fetch_time_first_session_closed.append(closed_flags[0][0])
            return 12345.0

    record_index_ticks(_TrackingSmartAPI(), None, db=None)

    assert len(closed_flags) == 2  # one short session to read, one to write
    assert fetch_time_first_session_closed == [True]
