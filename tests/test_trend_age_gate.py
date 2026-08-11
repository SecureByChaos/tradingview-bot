from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.originator import _Decision, _MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK, _open_trade
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


def _make_context(same_direction_entries_today: dict[str, int] | None = None) -> MarketContext:
    return MarketContext(
        index_symbol="BANKNIFTY",
        as_of=to_ist(utc_now()).replace(tzinfo=None),
        spot=50000.0,
        levels=Levels(),
        cpr=None,
        adx=28.0,
        plus_di=None,
        minus_di=None,
        atr_value=100.0,
        atr_percent=0.2,
        rsi_value=60.0,
        ema9=None,
        ema21=None,
        ema50=None,
        supertrend_5m=1,
        supertrend_15m=1,
        supertrend_5m_value=None,
        supertrend_15m_value=None,
        htf_ema20=None,
        htf_ema50=None,
        distance_from_ema21_atr=None,
        day_range_atr_multiple=None,
        same_direction_entries_today=same_direction_entries_today or {},
    )


def _make_decision(action: str = "BUY_PE") -> _Decision:
    return _Decision(action=action, confidence=0.7, sl_percent=10.0, target_percent=20.0, reasoning="test")


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


def test_blocks_at_threshold_before_touching_option_finder():
    db = _make_session()
    index = _make_index()
    context = _make_context({"BUY_PE": _MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK})
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is None
    assert option_finder.calls == 0  # gate short-circuits before any contract resolution
    assert smartapi.ltp_calls == 0


def test_blocks_above_threshold_too():
    db = _make_session()
    index = _make_index()
    context = _make_context({"BUY_PE": _MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK + 3})
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is None


def test_allows_below_threshold():
    db = _make_session()
    index = _make_index()
    context = _make_context({"BUY_PE": _MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK - 1})
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is not None
    assert option_finder.calls == 1


def test_allows_first_entry_of_the_day():
    db = _make_session()
    index = _make_index()
    context = _make_context({})  # nothing recorded yet today
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is not None


def test_gate_is_per_direction_not_per_index():
    # Heavy CE exposure must not block a PE entry -- the gate reads the
    # decision's own direction, not the index's exposure as a whole.
    db = _make_session()
    index = _make_index()
    context = _make_context({"BUY_CE": 10, "BUY_PE": 0})
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=context)

    assert result is not None


def test_no_market_context_does_not_crash_and_does_not_gate():
    # _open_trade's market_context parameter defaults to None; the gate must
    # degrade safely rather than raising when it's absent.
    db = _make_session()
    index = _make_index()
    decision = _make_decision("BUY_PE")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    result = _open_trade(db, index, "claude", decision, smartapi, option_finder, market_context=None)

    assert result is not None
