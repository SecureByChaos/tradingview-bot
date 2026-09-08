from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db_models import Base
from app.market_data import ONE_MINUTE, Bar, load_bars
from app.quick_scalp_feed import ScalpBarAggregator, _minute_bucket_to_ts_ist


class FakeIndex:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


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


NIFTY = FakeIndex("NIFTY")


def _shared_session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return engine, lambda: Session(engine)


def _make_aggregator(session_factory=None, option_finder=None, on_bar_closed=None, indexes=None):
    return ScalpBarAggregator(
        option_finder or FakeOptionFinder(), session_factory or (lambda: Session()),
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
    agg = _make_aggregator(on_bar_closed=lambda symbol: closed.append(symbol))
    agg.on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    assert "NIFTY" in agg._forming
    assert closed == []


def test_spot_tick_updates_high_low_close_within_the_same_minute():
    agg = _make_aggregator()
    agg.on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    agg.on_spot_tick("NIFTY", 24010.0, minute_bucket=1000)
    agg.on_spot_tick("NIFTY", 23990.0, minute_bucket=1000)
    agg.on_spot_tick("NIFTY", 24005.0, minute_bucket=1000)
    forming = agg._forming["NIFTY"]
    assert forming.open == 24000.0
    assert forming.high == 24010.0
    assert forming.low == 23990.0
    assert forming.close == 24005.0


def test_minute_rollover_finalizes_and_persists_the_completed_bar():
    engine, factory = _shared_session_factory()
    closed_symbols = []
    agg = _make_aggregator(session_factory=factory, on_bar_closed=lambda symbol: closed_symbols.append(symbol))

    agg.on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    agg.on_spot_tick("NIFTY", 24010.0, minute_bucket=1000)
    agg.on_spot_tick("NIFTY", 24005.0, minute_bucket=1001)  # rolls over

    assert closed_symbols == ["NIFTY"]
    with Session(engine) as db:
        bars = load_bars(db, "NIFTY", ONE_MINUTE)
        assert len(bars) == 1
        assert bars[0].open == 24000.0
        assert bars[0].high == 24010.0
        assert bars[0].close == 24010.0  # last tick BEFORE rollover
    # A new forming bar started at the new tick's price.
    assert agg._forming["NIFTY"].open == 24005.0


def test_minute_rollover_attaches_the_accumulated_futures_volume():
    engine, factory = _shared_session_factory()
    agg = _make_aggregator(session_factory=factory)

    # Two futures ticks land inside minute 1000 -- 10 units of real delta volume.
    agg.on_futures_tick("NIFTY", 500.0, minute_bucket=1000)  # first reading, no baseline yet
    agg.on_futures_tick("NIFTY", 510.0, minute_bucket=1000)  # delta 10
    agg.on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    agg.on_spot_tick("NIFTY", 24005.0, minute_bucket=1001)  # finalizes minute 1000

    with Session(engine) as db:
        bars = load_bars(db, "NIFTY", ONE_MINUTE)
        assert bars[0].volume == 10.0


def test_futures_volume_delta_clamped_at_zero_on_a_decrease():
    agg = _make_aggregator()
    agg.on_futures_tick("NIFTY", 500.0, minute_bucket=1000)
    agg.on_futures_tick("NIFTY", 480.0, minute_bucket=1000)  # a decrease -- session reset / stale reading
    assert agg._minute_volume.get("NIFTY", {}).get(1000, 0.0) == 0.0


def test_futures_tick_with_no_volume_field_is_a_no_op():
    agg = _make_aggregator()
    agg.on_futures_tick("NIFTY", None, minute_bucket=1000)
    assert agg._minute_volume.get("NIFTY", {}) == {}


def test_bar_without_any_futures_volume_persists_with_zero_volume():
    engine, factory = _shared_session_factory()
    agg = _make_aggregator(session_factory=factory)
    agg.on_spot_tick("NIFTY", 24000.0, minute_bucket=1000)
    agg.on_spot_tick("NIFTY", 24005.0, minute_bucket=1001)

    with Session(engine) as db:
        bars = load_bars(db, "NIFTY", ONE_MINUTE)
        assert bars[0].volume == 0.0


def test_finalize_bar_swallows_persistence_failures():
    def _exploding_factory():
        raise RuntimeError("db unavailable")

    agg = _make_aggregator(session_factory=_exploding_factory)
    agg._finalize_bar("NIFTY", Bar(ts_ist=datetime(2026, 9, 8, 11, 0), open=1, high=1, low=1, close=1))  # must not raise


def test_finalize_bar_swallows_callback_failures():
    engine, factory = _shared_session_factory()

    def _exploding_callback(symbol):
        raise RuntimeError("entry check blew up")

    agg = _make_aggregator(session_factory=factory, on_bar_closed=_exploding_callback)
    agg._finalize_bar("NIFTY", Bar(ts_ist=datetime(2026, 9, 8, 11, 0), open=1, high=1, low=1, close=1))  # must not raise
    with Session(engine) as db:
        assert len(load_bars(db, "NIFTY", ONE_MINUTE)) == 1  # the bar itself was still persisted


# ---------------------------------------------------------------------------
# resolve_futures_tokens
# ---------------------------------------------------------------------------

def test_resolve_futures_tokens_builds_the_map():
    option_finder = FakeOptionFinder({"NIFTY": {"exchange": "NFO", "tradingsymbol": "NIFTY28SEP26FUT", "symboltoken": "555"}})
    agg = _make_aggregator(option_finder=option_finder)
    tokens = agg.resolve_futures_tokens()
    assert tokens == ["555"]
    assert agg.futures_token_to_symbol == {"555": "NIFTY"}


def test_resolve_futures_tokens_degrades_gracefully_when_lookup_fails():
    agg = _make_aggregator(option_finder=_ExplodingOptionFinder())
    tokens = agg.resolve_futures_tokens()
    assert tokens == []


def test_resolve_futures_tokens_excludes_an_index_with_no_futures_contract():
    agg = _make_aggregator(option_finder=FakeOptionFinder({}))
    tokens = agg.resolve_futures_tokens()
    assert tokens == []
