from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.originator import (
    _Decision,
    _chop_gate_enabled,
    _chop_gate_min_efficiency_ratio,
    _open_trade,
)
from app.ai.repository import create_settings
from app.db_models import Base, IndexConfig
from app.market_context import Levels, MarketContext
from app.models import OptionContract, Signal
from app.time_utils import to_ist, utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty", ai_origination_live_trade=False)


def _make_decision(action: str = "BUY_PE") -> _Decision:
    return _Decision(action=action, confidence=0.7, sl_percent=10.0, target_percent=20.0, reasoning="test")


def _make_context(chop_efficiency_ratio: float | None) -> MarketContext:
    return MarketContext(
        index_symbol="BANKNIFTY", as_of=utc_now(), spot=57000.0,
        levels=Levels(),
        cpr=None,
        adx=26.5, plus_di=None, minus_di=None, atr_value=None, atr_percent=None,
        rsi_value=None, ema9=None, ema21=None, ema50=None,
        supertrend_5m=None, supertrend_15m=None, supertrend_5m_value=None, supertrend_15m_value=None,
        htf_ema20=None, htf_ema50=None, distance_from_ema21_atr=None, day_range_atr_multiple=None,
        chop_efficiency_ratio=chop_efficiency_ratio,
    )


class _FakeSettings:
    live_trading = False


class FakeSmartAPI:
    def __init__(self, price: float = 100.0) -> None:
        self.price = price
        self.settings = _FakeSettings()
        self.ltp_calls = 0

    def get_ltp(self, *_args, **_kwargs) -> float:
        self.ltp_calls += 1
        return self.price

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("should never place a real order in these tests")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract) -> None:
        self.contract = contract
        self.calls = 0

    def find_atm_contract(self, signal: Signal, index: IndexConfig, offset: int, min_dte: int | None = None) -> OptionContract:
        self.calls += 1
        return self.contract


def _make_contract() -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=20)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol="BANKNIFTY25AUG2650000PE",
        symboltoken="123",
        strike=50000,
        expiry=expiry,
        option_type="PE",
        lot_size=35,
    )


# ---------------------------------------------------------------------------
# _chop_gate_enabled / _chop_gate_min_efficiency_ratio
# ---------------------------------------------------------------------------

def test_chop_gate_enabled_falls_back_to_false_without_settings_row():
    db = _make_session()
    assert _chop_gate_enabled(db) is False


def test_chop_gate_enabled_reads_admin_configured_value():
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_enabled=True)
    assert _chop_gate_enabled(db) is True


def test_chop_gate_min_efficiency_ratio_falls_back_to_point_three_without_settings_row():
    db = _make_session()
    assert _chop_gate_min_efficiency_ratio(db) == 0.3


def test_chop_gate_min_efficiency_ratio_reads_admin_configured_value():
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_min_efficiency_ratio=0.5)
    assert _chop_gate_min_efficiency_ratio(db) == 0.5


# ---------------------------------------------------------------------------
# _open_trade integration
# ---------------------------------------------------------------------------

def test_open_trade_disabled_by_default_ignores_a_choppy_reading():
    # No AISettings row at all -- the gate must default to OFF and a choppy
    # market must not block anything until an admin opts in.
    db = _make_session()
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())
    context = _make_context(chop_efficiency_ratio=0.05)  # deeply choppy

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is not None
    assert option_finder.calls == 1


def test_open_trade_enabled_blocks_below_the_floor():
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_enabled=True)
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())
    context = _make_context(chop_efficiency_ratio=0.20)  # below the default 0.3 floor

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is None
    assert option_finder.calls == 0  # gate short-circuits before contract resolution
    assert smartapi.ltp_calls == 0


def test_open_trade_enabled_allows_at_or_above_the_floor():
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_enabled=True)
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())
    context = _make_context(chop_efficiency_ratio=0.55)  # clean

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is not None
    assert option_finder.calls == 1


def test_open_trade_boundary_exactly_at_floor_is_not_blocked():
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_enabled=True)
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())
    context = _make_context(chop_efficiency_ratio=0.3)  # exactly the default floor

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is not None


def test_open_trade_enabled_but_no_market_context_fails_open():
    # A missing reading is "unknown", never treated as "choppy" -- see the
    # gate's own comment in app/ai/originator.py.
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_enabled=True)
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=None)

    assert result is not None


def test_open_trade_enabled_but_chop_reading_missing_fails_open():
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_enabled=True)
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())
    context = _make_context(chop_efficiency_ratio=None)

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is not None


def test_open_trade_enabled_honors_admin_configured_floor():
    # A reading of 0.40 clears the default 0.3 floor but not an
    # admin-configured 0.5 floor.
    db = _make_session()
    create_settings(db, id=1, ai_origination_chop_gate_enabled=True, ai_origination_chop_gate_min_efficiency_ratio=0.5)
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())
    context = _make_context(chop_efficiency_ratio=0.40)

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is None
