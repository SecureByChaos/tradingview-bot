"""_load_market_context skips its own REST candle refresh entirely when the
live WebSocket-fed 1-minute history (app.quick_scalp_feed.ScalpBarAggregator,
fed by app.live_feed.IndexFeed's single connection) is already fresher than
BAR_FRESHNESS_SECONDS -- 16 Sep 2026, see CLAUDE.md's "pull everything from a
single source" entry. Before this, a REST failure had a good fallback (this
same ONE_MINUTE key), but nothing stopped the REST call from being attempted
every single 5-minute cycle regardless, which is what was actually generating
AI Origination's own share of the shared quote-throttle contention.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.originator import _load_market_context
from app.db_models import Base, IndexConfig
from app.market_data import ONE_MINUTE, Bar, store_bars
from app.time_utils import IST


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(
        symbol="NIFTY", display_name="Nifty 50", enabled=True,
        exchange_segment="NFO", instrument_name="NIFTY",
        spot_exchange="NSE", spot_symbol="NIFTY 50", spot_token="99926000",
    )


class _ExplodingSmartAPI:
    def get_candles(self, *_args, **_kwargs):
        raise AssertionError("REST get_candles should have been skipped -- WebSocket-fed history was fresh")


def _trending_bars(n: int, start: datetime, base: float = 24000.0) -> list[Bar]:
    bars = []
    price = base
    for i in range(n):
        price += 1.2 if i % 3 != 0 else -0.3
        bars.append(Bar(ts_ist=start + timedelta(minutes=i), open=price - 1, high=price + 1, low=price - 2, close=price))
    return bars


def test_load_market_context_skips_rest_when_websocket_fed_history_is_fresh():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    bars = _trending_bars(200, datetime(2026, 9, 16, 6, 0))
    # Overwrite freshness with one more bar 30s old -- well under the
    # 150s BAR_FRESHNESS_SECONDS floor.
    bars.append(Bar(ts_ist=datetime(2026, 9, 16, 9, 59, 30), open=24500.0, high=24510.0, low=24490.0, close=24505.0))
    store_bars(db, "NIFTY", ONE_MINUTE, bars)

    context, data_stale = _load_market_context(db, index, 24505.0, now_ist, _ExplodingSmartAPI())

    assert context is not None
    assert data_stale is False  # never entered the except branch, since REST was never attempted


def test_load_market_context_still_refreshes_when_websocket_fed_history_is_stale():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    # Only a 30-minutes-old bar stored -- e.g. a disconnected WebSocket feed
    # -- must still fall through to a real REST attempt.
    store_bars(db, "NIFTY", ONE_MINUTE, [
        Bar(ts_ist=datetime(2026, 9, 16, 9, 30), open=24000.0, high=24010.0, low=23990.0, close=24005.0),
    ])
    calls = []

    class _RecordingSmartAPI:
        def get_candles(self, *_args, **_kwargs):
            calls.append(1)
            return []

    _load_market_context(db, index, 24505.0, now_ist, _RecordingSmartAPI())
    assert calls == [1]  # REST was actually attempted, not skipped


def test_load_market_context_refreshes_when_nothing_stored_at_all():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    calls = []

    class _RecordingSmartAPI:
        def get_candles(self, *_args, **_kwargs):
            calls.append(1)
            return []

    context, _ = _load_market_context(db, index, 24505.0, now_ist, _RecordingSmartAPI())
    assert calls == [1]
    assert context is None  # no bars at all, even after the (empty) refresh


def test_load_market_context_returns_none_not_a_crash_when_bars_are_too_sparse_for_a_context(monkeypatch):
    # Regression: build_market_context can return None even when bars_1m is
    # non-empty (not enough history for ADX/EMA/Supertrend to warm up).
    # Previously this crashed with AttributeError on
    # `context.same_direction_entries_today = ...` instead of failing
    # closed like the "no bars at all" case just above.
    import app.ai.originator as module

    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    store_bars(db, "NIFTY", ONE_MINUTE, [
        Bar(ts_ist=datetime(2026, 9, 16, 9, 30), open=24000.0, high=24010.0, low=23990.0, close=24005.0),
    ])
    monkeypatch.setattr(module, "build_market_context", lambda **_kwargs: None)

    class _EmptyRestSmartAPI:
        def get_candles(self, *_args, **_kwargs):
            return []

    context, data_stale = _load_market_context(db, index, 24505.0, now_ist, _EmptyRestSmartAPI())
    assert context is None
    assert data_stale is False
