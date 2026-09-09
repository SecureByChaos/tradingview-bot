from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import app.dashboard_routes as dashboard_routes_module
from app.dashboard_routes import _live_dashboard_data
from app.db_models import Base, IndexConfig, IndexPriceTick
from app.time_utils import IST


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class _NullSmartAPI:
    def get_index_spot(self, index):
        raise AssertionError("must not be called -- the feed store already has a fresh price")


class FakeFeedStore:
    def __init__(self, entries: dict[str, dict] | None = None) -> None:
        self.entries = entries or {}

    def get(self, symbol: str):
        return self.entries.get(symbol)


def _make_engine_and_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return engine, Session(engine)


def _seed_index(db: Session) -> None:
    db.add(
        IndexConfig(
            symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True,
            spot_exchange="NSE", spot_symbol="Nifty Bank", spot_token="99926009",
        )
    )
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57000.0))
    db.commit()


def _writes_seen_during(engine, callback) -> list[str]:
    """9 Sep 2026, Phase 2 item 4 of the "portal unresponsive during market
    hours" investigation: listens at the real SQL-statement level (not just
    diffing row counts before/after) so this catches a write attempt even
    if it would have been a no-op or gotten rolled back -- the actual
    requirement is that the request handler never ISSUES a write, not just
    that the database ends up unchanged."""
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        callback()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    return [s for s in statements if s.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


def test_live_dashboard_data_issues_no_writes_during_trading_hours(monkeypatch):
    # The specific moment the old code DID write (is_trading_now=True) --
    # see get_index_live_figures' own history. Must now be a pure read.
    monkeypatch.setattr(dashboard_routes_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))  # Thursday, trading hours
    engine, db = _make_engine_and_session()
    _seed_index(db)
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57050.0, "is_live": True, "age_seconds": 0.5}})

    writes = _writes_seen_during(engine, lambda: _live_dashboard_data(db, _NullSmartAPI(), feed_store))

    assert writes == []


def test_live_dashboard_data_issues_no_writes_outside_trading_hours(monkeypatch):
    monkeypatch.setattr(dashboard_routes_module, "utc_now", lambda: _ist(2026, 8, 15, 12, 0))  # Saturday
    engine, db = _make_engine_and_session()
    _seed_index(db)
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57050.0, "is_live": True, "age_seconds": 0.5}})

    writes = _writes_seen_during(engine, lambda: _live_dashboard_data(db, _NullSmartAPI(), feed_store))

    assert writes == []


def test_live_dashboard_data_issues_no_writes_with_no_indexes_configured(monkeypatch):
    monkeypatch.setattr(dashboard_routes_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))
    engine, db = _make_engine_and_session()

    writes = _writes_seen_during(engine, lambda: _live_dashboard_data(db, _NullSmartAPI(), live_feed_store=None))

    assert writes == []
