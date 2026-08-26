from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai import originator
from app.ai.originator import run_origination_checks
from app.ai.repository import create_settings
from app.db_models import AIOriginationLog, Base, IndexConfig, StrategyTrade, TradeStatus, TradingMode
from app.market_context import CPR, Levels, MarketContext
from app.time_utils import IST, utc_now


def _ist(y, m, d, hh=11, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_context(**overrides) -> MarketContext:
    # Every figure below deliberately avoids formatting to a trailing "X0.00"
    # -- _prompt_has_defect flags the literal substring "0.00 pts" anywhere in
    # the rendered prompt (its backstop for a fabricated zero-distance line),
    # and a plain round number like 120.00 or 20.00 pts collides with that
    # substring check by coincidence, not because anything is actually wrong.
    fields = dict(
        index_symbol="NIFTY", as_of=utc_now(), spot=24303.45,
        levels=Levels(
            opening_range_high=24351.2, opening_range_low=24252.85, opening_range_complete=True,
            previous_day_high=24402.6, previous_day_low=24201.35, previous_day_close=24281.9,
            day_open=24292.4, day_high=24361.75, day_low=24241.15,
        ),
        cpr=CPR(pivot=24301.5, top=24352.4, bottom=24250.6, width_percent=0.42, classification="NARROW"),
        adx=28.4, plus_di=25.3, minus_di=15.1, atr_value=118.35, atr_percent=0.49,
        rsi_value=55.2, ema9=24291.7, ema21=24281.35, ema50=24261.9,
        supertrend_5m=1, supertrend_15m=1, supertrend_5m_value=24271.6, supertrend_15m_value=24261.4,
        htf_ema20=24271.8, htf_ema50=24251.3, distance_from_ema21_atr=0.53, day_range_atr_multiple=1.02,
        drift_15m=0.12, drift_45m=0.24, drift_180m=0.31, drift_since_open=0.42,
        regime="TREND", trend_duration_bars=10, trend_duration_pct_of_session=40.0, move_extent_atr=1.23,
    )
    fields.update(overrides)
    return MarketContext(**fields)


class FakeSmartAPI:
    def __init__(self, price: float = 24300.0) -> None:
        self.price = price

    def get_index_spot(self, _index) -> float:
        return self.price


class _ExplodingOptionFinder:
    def find_atm_contract(self, *args, **kwargs):
        raise AssertionError("option_finder must never be touched -- every provider slot is occupied")


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="NIFTY", display_name="Nifty 50", enabled=True, ai_origination_live_trade=False)


def _open_trade(**overrides) -> StrategyTrade:
    fields = dict(
        trade_id="t-open",
        strategy_name="AI_ORIGIN", signal="BUY_PE", index_symbol="NIFTY",
        tradingsymbol="X", symboltoken="1", strike=24300, expiry="28AUG2026",
        option_type="PE", quantity=75, entry_price=100.0, stoploss=90.0, target=120.0,
        entry_time=utc_now(), origin="AI_ORIGIN_OPENAI", status=TradeStatus.OPEN,
        mode=TradingMode.PAPER,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_run_origination_checks_keeps_logging_context_when_the_only_providers_slot_is_occupied(monkeypatch):
    # 26 Aug 2026: reproduces the reported Nifty 50 stale-panel bug. A
    # single-provider AI Settings config (secondary disabled) with an open
    # trade occupying that provider's only slot on this index used to skip
    # the whole index -- no price fetch, no _load_market_context, no
    # AIOriginationLog row -- so get_market_conditions() kept serving the
    # last row from before the trade opened, indefinitely, for as long as it
    # stayed open. Context must now still be built and logged every cycle.
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", secondary_enabled=False)
    db.add(_open_trade())
    db.commit()

    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))  # Thursday, trading hours
    monkeypatch.setattr(originator, "_load_market_context", lambda *a, **k: (_make_context(), False))

    def _exploding_call_provider(*args, **kwargs):
        raise AssertionError("no provider has a free slot -- the model must never be called this cycle")

    monkeypatch.setattr(originator, "_call_provider", _exploding_call_provider)

    run_origination_checks(FakeSmartAPI(), _ExplodingOptionFinder(), db=db)

    rows = db.query(AIOriginationLog).filter(AIOriginationLog.index_name == "NIFTY").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.decision == "SLOT_OCCUPIED"
    assert row.regime == "TREND"
    assert row.adx == 28.4
    assert row.confidence is None
    assert not row.reasoning  # excludes this row from every reasoning-filtered backtest script
    assert row.trade_id is None


def test_run_origination_checks_still_calls_the_provider_when_its_slot_is_free(monkeypatch):
    # Control case: no open trade at all -- the provider loop must run
    # normally and record a real decision, not the context-only marker.
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", secondary_enabled=False)
    db.commit()

    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))
    monkeypatch.setattr(originator, "_load_market_context", lambda *a, **k: (_make_context(), False))
    monkeypatch.setattr(
        originator, "_call_provider",
        lambda *a, **k: originator._Decision(action="NONE", confidence=0.4, sl_percent=None, target_percent=None, reasoning="quiet"),
    )

    run_origination_checks(FakeSmartAPI(), _ExplodingOptionFinder(), db=db)

    rows = db.query(AIOriginationLog).filter(AIOriginationLog.index_name == "NIFTY").all()
    assert len(rows) == 1
    assert rows[0].decision == "NONE"
    assert rows[0].provider == "openai"
