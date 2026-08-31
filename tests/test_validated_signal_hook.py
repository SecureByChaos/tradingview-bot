from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai import originator
from app.ai.originator import run_origination_checks
from app.ai.repository import create_settings
from app.db_models import AIOriginationLog, Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_context import CPR, Levels, MarketContext
from app.time_utils import IST, utc_now


def _ist(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_context(**overrides) -> MarketContext:
    fields = dict(
        index_symbol="NIFTY", as_of=utc_now(), spot=24303.45,
        levels=Levels(),
        cpr=CPR(pivot=24301.5, top=24352.4, bottom=24250.6, width_percent=0.42, classification="NARROW"),
        adx=28.4, plus_di=25.3, minus_di=15.1, atr_value=118.35, atr_percent=0.49,
        rsi_value=55.2, ema9=24291.7, ema21=24281.35, ema50=24261.9,
        supertrend_5m=1, supertrend_15m=1, supertrend_5m_value=24271.6, supertrend_15m_value=24261.4,
        htf_ema20=24271.8, htf_ema50=24251.3, distance_from_ema21_atr=0.53, day_range_atr_multiple=1.02,
        regime="TREND", setups={},
    )
    fields.update(overrides)
    return MarketContext(**fields)


class FakeSmartAPI:
    def __init__(self, price: float = 24300.0) -> None:
        self.price = price

    def get_index_spot(self, _index) -> float:
        return self.price

    def get_ltp(self, *_args, **_kwargs) -> float:
        return 100.0


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="NIFTY", display_name="Nifty 50", enabled=True, ai_origination_live_trade=False)


def test_hook_opens_a_validated_signal_trade_when_the_setup_and_window_match(monkeypatch):
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", secondary_enabled=False)
    db.commit()

    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 13, 12, 0))  # inside 11:00-14:00 IST
    monkeypatch.setattr(
        originator, "_load_market_context",
        lambda *a, **k: (_make_context(setups={"EMA_STACK_UP": True}), False),
    )
    monkeypatch.setattr(
        originator, "_call_provider",
        lambda *a, **k: originator._Decision(action="NONE", confidence=0.4, sl_percent=None, target_percent=None, reasoning="quiet"),
    )

    class _StubOptionFinder:
        def find_atm_contract(self, signal, index, offset, min_dte=None):
            from datetime import timedelta

            from app.models import OptionContract
            from app.time_utils import to_ist

            expiry = (to_ist(utc_now()).date() + timedelta(days=10)).strftime("%d%b%Y").upper()
            return OptionContract(
                tradingsymbol="NIFTY10OCT2624300CE", symboltoken="1", strike=24300,
                expiry=expiry, option_type="CE", lot_size=75,
            )

    run_origination_checks(FakeSmartAPI(), _StubOptionFinder(), db=db)

    trades = db.query(StrategyTrade).filter(StrategyTrade.origin == "VALIDATED_SIGNAL").all()
    assert len(trades) == 1
    assert trades[0].signal == "BUY_CE"
    assert trades[0].mode == TradingMode.PAPER


def test_hook_failure_does_not_break_ai_origination_own_decision_loop(monkeypatch):
    # A broken Validated Signal check must never take AI Origination's own
    # per-provider loop down with it -- the hook is wrapped in its own
    # try/except specifically for this.
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", secondary_enabled=False)
    db.commit()

    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 13, 12, 0))
    monkeypatch.setattr(originator, "_load_market_context", lambda *a, **k: (_make_context(), False))
    monkeypatch.setattr(
        originator, "check_validated_signal",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        originator, "_call_provider",
        lambda *a, **k: originator._Decision(action="NONE", confidence=0.4, sl_percent=None, target_percent=None, reasoning="quiet"),
    )

    class _ExplodingOptionFinder:
        def find_atm_contract(self, *args, **kwargs):
            raise AssertionError("must not be reached")

    run_origination_checks(FakeSmartAPI(), _ExplodingOptionFinder(), db=db)

    rows = db.query(AIOriginationLog).filter(AIOriginationLog.index_name == "NIFTY").all()
    assert len(rows) == 1
    assert rows[0].decision == "NONE"


def test_hook_does_not_fire_when_no_setup_matches(monkeypatch):
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", secondary_enabled=False)
    db.commit()

    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 13, 12, 0))
    monkeypatch.setattr(originator, "_load_market_context", lambda *a, **k: (_make_context(), False))
    monkeypatch.setattr(
        originator, "_call_provider",
        lambda *a, **k: originator._Decision(action="NONE", confidence=0.4, sl_percent=None, target_percent=None, reasoning="quiet"),
    )

    class _ExplodingOptionFinder:
        def find_atm_contract(self, *args, **kwargs):
            raise AssertionError("must not be reached -- no matching setup")

    run_origination_checks(FakeSmartAPI(), _ExplodingOptionFinder(), db=db)

    trades = db.query(StrategyTrade).filter(StrategyTrade.origin == "VALIDATED_SIGNAL").all()
    assert trades == []
