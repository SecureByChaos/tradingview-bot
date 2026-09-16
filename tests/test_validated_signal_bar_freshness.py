"""_load_index_features/_futures_volume_by_5min switched from REST-fetched
FIVE_MINUTE candles to ONE_MINUTE + resample() -- 16 Sep 2026, see CLAUDE.md's
"pull everything from a single source" entry.

Before this, FIVE_MINUTE was a key nothing but this module's own REST calls
ever wrote to, so a REST failure (rate limit, timeout) had no live fallback
at all and the module halted new signals almost every time it hit one --
confirmed from real production logs on 15 Sep 2026. ONE_MINUTE is the key
app.quick_scalp_feed.ScalpBarAggregator already writes to continuously off
the shared WebSocket feed, so a REST failure now falls back to genuinely
fresh data, and the REST call is skipped entirely (not just tolerated on
failure) when that history is already fresh enough.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig
from app.market_data import FIVE_MINUTE, ONE_MINUTE, Bar, load_bars, store_bars
from app.time_utils import IST
from app.validated_signal import _futures_volume_by_5min, _load_index_features


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index(symbol: str = "BANKNIFTY") -> IndexConfig:
    return IndexConfig(symbol=symbol, display_name=symbol, spot_token="1", enabled=True)


class _ExplodingSmartAPI:
    def get_candles(self, *_args, **_kwargs):
        raise AssertionError("REST get_candles should have been skipped -- WebSocket-fed history was fresh")


class _RecordingSmartAPI:
    def __init__(self) -> None:
        self.calls = 0

    def get_candles(self, *_args, **_kwargs):
        self.calls += 1
        return []


class _FakeOptionFinder:
    def __init__(self, futures_contract=None) -> None:
        self._futures_contract = futures_contract

    def find_current_futures_contract(self, index):
        return self._futures_contract


_CONTRACT = {"exchange": "NFO", "tradingsymbol": "BANKNIFTY28SEP26FUT", "symboltoken": "999"}


def _one_min_bars(n: int, start: datetime, base: float = 57000.0) -> list[Bar]:
    bars = []
    price = base
    for i in range(n):
        price += 1.0 if i % 2 == 0 else -0.4
        bars.append(Bar(ts_ist=start + timedelta(minutes=i), open=price - 1, high=price + 1, low=price - 2, close=price))
    return bars


# ---------------------------------------------------------------------------
# _load_index_features -- spot side
# ---------------------------------------------------------------------------

def test_load_index_features_skips_rest_when_one_minute_history_is_fresh():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    bars = _one_min_bars(200, datetime(2026, 9, 16, 6, 0))
    bars.append(Bar(ts_ist=datetime(2026, 9, 16, 9, 59, 30), open=57500.0, high=57510.0, low=57490.0, close=57505.0))
    store_bars(db, "BANKNIFTY", ONE_MINUTE, bars)

    session_bars, volumes, pdh, pdl, refresh_failed = _load_index_features(
        db, index, _ExplodingSmartAPI(), _FakeOptionFinder(), now_ist,
    )
    assert refresh_failed is False


def test_load_index_features_still_refreshes_when_one_minute_history_is_stale():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    store_bars(db, "BANKNIFTY", ONE_MINUTE, [
        Bar(ts_ist=datetime(2026, 9, 16, 9, 0), open=57000.0, high=57010.0, low=56990.0, close=57005.0),
    ])
    smartapi = _RecordingSmartAPI()

    _load_index_features(db, index, smartapi, _FakeOptionFinder(), now_ist)
    assert smartapi.calls == 1


def test_load_index_features_drops_the_incomplete_trailing_five_min_bucket():
    # A real REST FIVE_MINUTE call (the module's pre-16-Sep behaviour) would
    # never return a partial bar -- resample()'s own trailing bucket must be
    # dropped so the box/ORB/trigger logic keeps seeing only fully CLOSED
    # 5-min candles.
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 17, tzinfo=IST)  # mid-way through a 5-min bucket
    bars = _one_min_bars(60, datetime(2026, 9, 16, 9, 15))  # up to 10:14, complete buckets only
    bars.append(Bar(ts_ist=datetime(2026, 9, 16, 10, 16), open=57500.0, high=57510.0, low=57490.0, close=57505.0))
    store_bars(db, "BANKNIFTY", ONE_MINUTE, bars)

    session_bars, *_ = _load_index_features(db, index, _ExplodingSmartAPI(), _FakeOptionFinder(), now_ist)
    # The 10:15-10:20 bucket is still forming (only the 10:16 minute exists)
    # and must not appear.
    assert all(b.ts_ist != datetime(2026, 9, 16, 10, 15) for b in session_bars)


# ---------------------------------------------------------------------------
# _futures_volume_by_5min -- futures volume side
# ---------------------------------------------------------------------------

def test_futures_volume_skips_rest_when_one_minute_history_is_fresh():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    store_bars(db, "BANKNIFTY_FUT", ONE_MINUTE, [
        Bar(ts_ist=datetime(2026, 9, 16, 9, 59), open=57000.0, high=57010.0, low=56990.0, close=57005.0, volume=500.0),
    ])
    result = _futures_volume_by_5min(db, index, _FakeOptionFinder(_CONTRACT), _ExplodingSmartAPI(), now_ist)
    assert result  # did not raise, and a real reading came back


def test_futures_volume_still_refreshes_when_one_minute_history_is_stale():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    store_bars(db, "BANKNIFTY_FUT", ONE_MINUTE, [
        Bar(ts_ist=datetime(2026, 9, 16, 9, 0), open=57000.0, high=57010.0, low=56990.0, close=57005.0, volume=500.0),
    ])
    smartapi = _RecordingSmartAPI()

    _futures_volume_by_5min(db, index, _FakeOptionFinder(_CONTRACT), smartapi, now_ist)
    assert smartapi.calls == 1


def test_futures_volume_resamples_one_minute_bars_into_five_minute_buckets():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 9, 25, tzinfo=IST)
    store_bars(db, "BANKNIFTY_FUT", ONE_MINUTE, [
        Bar(ts_ist=datetime(2026, 9, 16, 9, 15) + timedelta(minutes=i), open=1, high=1, low=1, close=1, volume=10.0)
        for i in range(5)
    ])
    result = _futures_volume_by_5min(db, index, _FakeOptionFinder(_CONTRACT), _ExplodingSmartAPI(), now_ist)
    assert result.get(datetime(2026, 9, 16, 9, 15)) == 50.0  # 5 one-min bars x 10 volume, summed


def test_futures_volume_empty_without_a_futures_contract():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    result = _futures_volume_by_5min(db, index, _FakeOptionFinder(None), _ExplodingSmartAPI(), now_ist)
    assert result == {}
