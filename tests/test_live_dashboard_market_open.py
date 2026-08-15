from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.dashboard_routes as dashboard_routes_module
from app.dashboard_routes import _live_dashboard_data
from app.db_models import Base
from app.time_utils import IST


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


class _NullSmartAPI:
    def get_index_spot(self, index):
        raise AssertionError("should not be called -- no IndexConfig rows seeded")


def test_market_open_true_during_trading_hours_on_a_weekday(monkeypatch):
    monkeypatch.setattr(dashboard_routes_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))  # Thursday
    data = _live_dashboard_data(_make_session(), _NullSmartAPI(), live_feed_store=None)
    assert data["market_open"] is True


def test_market_open_false_on_a_weekend(monkeypatch):
    monkeypatch.setattr(dashboard_routes_module, "utc_now", lambda: _ist(2026, 8, 15, 12, 0))  # Saturday
    data = _live_dashboard_data(_make_session(), _NullSmartAPI(), live_feed_store=None)
    assert data["market_open"] is False


def test_market_open_false_on_an_nse_holiday(monkeypatch):
    monkeypatch.setattr(dashboard_routes_module, "utc_now", lambda: _ist(2026, 1, 26, 12, 0))  # Republic Day
    data = _live_dashboard_data(_make_session(), _NullSmartAPI(), live_feed_store=None)
    assert data["market_open"] is False


def test_market_open_false_outside_trading_hours_on_a_weekday(monkeypatch):
    monkeypatch.setattr(dashboard_routes_module, "utc_now", lambda: _ist(2026, 8, 13, 20, 0))  # Thursday evening
    data = _live_dashboard_data(_make_session(), _NullSmartAPI(), live_feed_store=None)
    assert data["market_open"] is False
