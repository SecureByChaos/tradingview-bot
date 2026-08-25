from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.platform as platform_module
from app.db_models import Base, Candle, IndexConfig, IndexPriceTick
from app.market_data import ONE_MINUTE
from app.platform import get_index_live_figures
from app.time_utils import IST, utc_now


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


# ---------------------------------------------------------------------------
# 17 Aug 2026: falls back to the last-ever-recorded IndexPriceTick, instead of
# "Unavailable", when the feed hasn't produced a value for this process yet --
# routine right after a restart while the market's closed, since the live
# feed's own market-hours gate (app/live_feed.py, same date) means it no
# longer even attempts to reconnect in that state.
# ---------------------------------------------------------------------------

def test_falls_back_to_last_known_tick_when_feed_has_no_entry_yet():
    db = _make_session()
    _seed_index(db)
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57250.5, recorded_at=utc_now() - timedelta(days=1)))
    db.commit()
    smartapi = FakeSmartAPI()
    feed_store = FakeFeedStore({})  # feed running, no tick for this index yet

    figures = get_index_live_figures(db, smartapi, feed_store)

    assert smartapi.calls == 0  # still never falls back to a fresh SmartAPI call
    assert figures[0]["price"] == 57250.5
    assert figures[0]["is_live"] is False
    assert "error" not in figures[0]


def test_last_known_tick_fallback_picks_the_most_recent_one():
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57000.0, recorded_at=now - timedelta(days=3)))
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57500.0, recorded_at=now - timedelta(days=1)))
    db.commit()
    feed_store = FakeFeedStore({})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    assert figures[0]["price"] == 57500.0


def test_last_known_tick_fallback_is_not_re_recorded_as_a_fresh_tick(monkeypatch):
    # Even in the rare case this fallback fires DURING trading hours (feed
    # hasn't produced its first tick of the session yet), the fallback price
    # must not be written as a new tick -- that would inject a possibly-days-
    # old value into today's tick history and corrupt the change/day-range
    # math computed from it for the rest of the day.
    monkeypatch.setattr(platform_module, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))  # Thursday, trading hours
    db = _make_session()
    _seed_index(db)
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57250.5, recorded_at=_ist(2026, 8, 11, 10, 0)))
    db.commit()
    feed_store = FakeFeedStore({})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    assert figures[0]["price"] == 57250.5
    ticks = list(db.scalars(select(IndexPriceTick).where(IndexPriceTick.index_symbol == "BANKNIFTY")))
    assert len(ticks) == 1  # still just the seeded one -- no new row added


def test_no_fallback_available_still_shows_unavailable():
    # Genuinely nothing has ever been recorded for this index -- there is no
    # "last known price" to fall back to, so this must stay "Unavailable"
    # rather than fabricating a value.
    db = _make_session()
    _seed_index(db)
    feed_store = FakeFeedStore({})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    assert figures[0]["price"] is None
    assert "error" in figures[0]


# ---------------------------------------------------------------------------
# 21 Aug 2026: change_abs/change_percent computed against the previous trading
# day's last recorded tick, not today's first tick -- reported mismatch
# against TradingView (which shows change vs. previous close) traced to this,
# since "change since today's open" and "change since previous close" differ
# by whatever the index gapped overnight.
# ---------------------------------------------------------------------------


def test_change_computed_against_previous_day_last_tick_not_todays_first():
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    # Previous day: opened 57000, closed 57200 (the LAST tick that day).
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57000.0, recorded_at=now - timedelta(days=1, hours=6)))
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57200.0, recorded_at=now - timedelta(days=1, hours=1)))
    # Today's first tick (an overnight gap-up from yesterday's close).
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57400.0, recorded_at=now))
    db.commit()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57450.0, "is_live": True, "age_seconds": 0.1}})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    # Against yesterday's LAST tick (57200), not today's first tick (57400).
    assert figures[0]["change_abs"] == 250.0
    assert figures[0]["change_percent"] == round(250.0 / 57200.0 * 100, 2)


def test_change_falls_back_to_todays_first_tick_when_no_prior_day_tick_exists():
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57000.0, recorded_at=now))
    db.commit()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57100.0, "is_live": True, "age_seconds": 0.1}})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    assert figures[0]["change_abs"] == 100.0


def test_day_low_high_still_computed_from_todays_ticks_only():
    # Confirms the previous-day reference change didn't accidentally pull
    # yesterday's prices into today's day-range figures.
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=50000.0, recorded_at=now - timedelta(days=1)))
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57000.0, recorded_at=now - timedelta(hours=2)))
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57300.0, recorded_at=now - timedelta(hours=1)))
    db.commit()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57100.0, "is_live": True, "age_seconds": 0.1}})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    assert figures[0]["day_low"] == 57000.0
    assert figures[0]["day_high"] == 57300.0


# ---------------------------------------------------------------------------
# 25 Aug 2026: prefer the CAS-corrected candle close over the previous-day-
# tick approximation. Real production data confirmed the tick-based
# reference could be recorded at 15:25 IST -- before the auction settles --
# which is exactly the class of gap capture_closing_auction() was already
# built to fix for AI Origination's own previous-close reads. This wires the
# same corrected source into the Live market panel instead of maintaining a
# second, less accurate mechanism.
# ---------------------------------------------------------------------------


def _candle_ts(days_ago: int = 1, hour: int = 15, minute: int = 29) -> datetime:
    """A naive IST timestamp, matching Candle.ts_ist's own storage convention
    (see its docstring: 'IST-naive minute timestamp')."""
    base = datetime.now(IST) - timedelta(days=days_ago)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)


def test_prefers_candle_close_over_previous_day_tick_when_both_exist():
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    # A stale tick recorded well before the real settlement (the confirmed
    # production shape: 15:25 IST, before the ~15:29-15:35 CAS close).
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57419.45, recorded_at=now - timedelta(days=1, hours=1)))
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57450.0, recorded_at=now))
    # The corrected CAS close, as capture_closing_auction() would have
    # written it.
    db.add(Candle(index_symbol="BANKNIFTY", interval=ONE_MINUTE, ts_ist=_candle_ts(), open=57500.0, high=57550.0, low=57500.0, close=57525.95))
    db.commit()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57450.0, "is_live": True, "age_seconds": 0.1}})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    assert figures[0]["change_abs"] == round(57450.0 - 57525.95, 2)


def test_candle_reference_uses_most_recent_previous_day_candle():
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57450.0, recorded_at=now))
    db.add(Candle(index_symbol="BANKNIFTY", interval=ONE_MINUTE, ts_ist=_candle_ts(hour=15, minute=13), open=57600.0, high=57600.0, low=57600.0, close=57600.0))
    db.add(Candle(index_symbol="BANKNIFTY", interval=ONE_MINUTE, ts_ist=_candle_ts(hour=15, minute=29), open=57525.95, high=57525.95, low=57525.95, close=57525.95))
    db.commit()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57450.0, "is_live": True, "age_seconds": 0.1}})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    assert figures[0]["change_abs"] == round(57450.0 - 57525.95, 2)


def test_candle_reference_ignores_non_one_minute_interval_rows():
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57200.0, recorded_at=now - timedelta(days=1)))
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57450.0, recorded_at=now))
    # A FIVE_MINUTE candle must not be picked up by the ONE_MINUTE-filtered
    # query -- only the fine-grained series capture_closing_auction() writes.
    db.add(Candle(index_symbol="BANKNIFTY", interval="FIVE_MINUTE", ts_ist=_candle_ts(), open=57999.0, high=57999.0, low=57999.0, close=57999.0))
    db.commit()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57450.0, "is_live": True, "age_seconds": 0.1}})

    figures = get_index_live_figures(db, FakeSmartAPI(), feed_store)

    # Falls back to the IndexPriceTick reference, not the FIVE_MINUTE candle.
    assert figures[0]["change_abs"] == round(57450.0 - 57200.0, 2)


def test_prevclose_log_line_reports_which_source_was_used(caplog):
    db = _make_session()
    _seed_index(db)
    now = utc_now()
    db.add(IndexPriceTick(index_symbol="BANKNIFTY", price=57450.0, recorded_at=now))
    db.add(Candle(index_symbol="BANKNIFTY", interval=ONE_MINUTE, ts_ist=_candle_ts(), open=57525.95, high=57525.95, low=57525.95, close=57525.95))
    db.commit()
    feed_store = FakeFeedStore({"BANKNIFTY": {"price": 57450.0, "is_live": True, "age_seconds": 0.1}})

    with caplog.at_level("INFO"):
        get_index_live_figures(db, FakeSmartAPI(), feed_store)

    [line] = [r.message for r in caplog.records if "[PREVCLOSE]" in r.message]
    assert "candle" in line
    assert "57525.95" in line
