from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig
from app.platform import get_index_live_figures


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
