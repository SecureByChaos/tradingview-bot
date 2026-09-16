"""latest_bar_age_seconds and FUTURES_CANDLE_SUFFIX -- added 16 Sep 2026 so
every REST-polling candle consumer (app.ai.originator, app.ai.autonomous,
app.validated_signal) can skip its own REST refresh when the live
WebSocket-fed history (app.quick_scalp_feed.ScalpBarAggregator) is already
fresh enough, instead of hitting SmartAPI's shared quote throttle every
cycle regardless. See CLAUDE.md's "pull everything from a single source"
entry for the throttle-contention problem this exists to relieve.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base
from app.market_data import (
    FUTURES_CANDLE_SUFFIX,
    ONE_MINUTE,
    Bar,
    latest_bar_age_seconds,
    store_bars,
)
from app.time_utils import IST


def _make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def test_futures_candle_suffix_is_the_established_fut_string():
    # Every consumer (Autonomous AI's VWAP, Validated Signal's volume gate,
    # and now the live WebSocket feed's own futures-bar persistence) must
    # agree on this exact literal for the shared-key mechanism to work --
    # pinned here so a future edit to this one constant can't silently
    # drift without a test noticing.
    assert FUTURES_CANDLE_SUFFIX == "_FUT"


def test_latest_bar_age_seconds_returns_none_when_nothing_stored():
    db = _make_session()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    assert latest_bar_age_seconds(db, "NIFTY", ONE_MINUTE, now_ist) is None


def test_latest_bar_age_seconds_computes_real_elapsed_time():
    db = _make_session()
    store_bars(db, "NIFTY", ONE_MINUTE, [Bar(ts_ist=datetime(2026, 9, 16, 9, 58), open=1, high=1, low=1, close=1)])
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)  # 2 minutes later
    age = latest_bar_age_seconds(db, "NIFTY", ONE_MINUTE, now_ist)
    assert age == 120.0


def test_latest_bar_age_seconds_accepts_a_naive_now_too():
    db = _make_session()
    store_bars(db, "NIFTY", ONE_MINUTE, [Bar(ts_ist=datetime(2026, 9, 16, 9, 59), open=1, high=1, low=1, close=1)])
    now_naive = datetime(2026, 9, 16, 10, 0)
    assert latest_bar_age_seconds(db, "NIFTY", ONE_MINUTE, now_naive) == 60.0


def test_latest_bar_age_seconds_picks_the_most_recent_bar_not_the_first():
    db = _make_session()
    store_bars(
        db, "NIFTY", ONE_MINUTE,
        [
            Bar(ts_ist=datetime(2026, 9, 16, 9, 30), open=1, high=1, low=1, close=1),
            Bar(ts_ist=datetime(2026, 9, 16, 9, 59), open=1, high=1, low=1, close=1),
        ],
    )
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    assert latest_bar_age_seconds(db, "NIFTY", ONE_MINUTE, now_ist) == 60.0


def test_latest_bar_age_seconds_is_scoped_to_index_symbol_and_interval():
    db = _make_session()
    store_bars(db, "NIFTY", ONE_MINUTE, [Bar(ts_ist=datetime(2026, 9, 16, 9, 59), open=1, high=1, low=1, close=1)])
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    assert latest_bar_age_seconds(db, "BANKNIFTY", ONE_MINUTE, now_ist) is None
    assert latest_bar_age_seconds(db, "NIFTY", "FIVE_MINUTE", now_ist) is None
