from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_context import Levels, MarketContext
from app.models import OptionContract, Signal
from app.time_utils import to_ist, utc_now
from app.validated_signal import (
    ORIGIN,
    _has_open_validated_trade,
    check_validated_signal,
    open_validated_trade,
    validated_action,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty")


def _context(**setups: bool) -> MarketContext:
    return MarketContext(
        index_symbol="BANKNIFTY", as_of=utc_now(), spot=57000.0,
        levels=Levels(),
        cpr=None,
        adx=None, plus_di=None, minus_di=None, atr_value=None, atr_percent=None,
        rsi_value=None, ema9=None, ema21=None, ema50=None,
        supertrend_5m=None, supertrend_15m=None, supertrend_5m_value=None, supertrend_15m_value=None,
        htf_ema20=None, htf_ema50=None, distance_from_ema21_atr=None, day_range_atr_multiple=None,
        setups=dict(setups),
    )


class FakeSmartAPI:
    def __init__(self, price: float | None = 100.0) -> None:
        self.price = price
        self.ltp_calls = 0

    def get_ltp(self, *_args, **_kwargs) -> float | None:
        self.ltp_calls += 1
        return self.price

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("Validated Signal must never place a real order")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract | None) -> None:
        self.contract = contract
        self.calls = 0

    def find_atm_contract(self, signal: Signal, index: IndexConfig, offset: int, min_dte: int | None = None) -> OptionContract:
        self.calls += 1
        if self.contract is None:
            raise ValueError("no contract available")
        return self.contract


def _make_contract(dte_days: int = 8, option_type: str = "PE") -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=dte_days)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol=f"BANKNIFTY{expiry}57000{option_type}",
        symboltoken="123",
        strike=57000,
        expiry=expiry,
        option_type=option_type,
        lot_size=35,
    )


def _add_trade(db, *, trade_id, index_symbol="BANKNIFTY", origin=ORIGIN, status=TradeStatus.OPEN) -> None:
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Validated Signal - Bank Nifty", signal="BUY_CE",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=100.0, stoploss=88.0, target=120.0, entry_time=utc_now(),
        origin=origin, status=status, result=TradeResult.OPEN if status == TradeStatus.OPEN else TradeResult.WIN,
        mode=TradingMode.PAPER,
    ))
    db.commit()


# ---------------------------------------------------------------------------
# validated_action
# ---------------------------------------------------------------------------

def test_returns_none_without_market_context():
    assert validated_action(None, datetime(2026, 8, 28, 12, 0)) is None


def test_returns_none_outside_window():
    ctx = _context(EMA_STACK_UP=True)
    assert validated_action(ctx, datetime(2026, 8, 28, 10, 59)) is None
    assert validated_action(ctx, datetime(2026, 8, 28, 14, 0)) is None


def test_window_boundaries_inclusive_start_exclusive_end():
    ctx = _context(PDH_BREAK=True)
    assert validated_action(ctx, datetime(2026, 8, 28, 11, 0)) == "BUY_CE"
    assert validated_action(ctx, datetime(2026, 8, 28, 13, 59)) == "BUY_CE"
    assert validated_action(ctx, datetime(2026, 8, 28, 14, 0)) is None


def test_up_setup_gives_buy_ce():
    ctx = _context(EMA_STACK_UP=True, EMA_STACK_DOWN=False)
    assert validated_action(ctx, datetime(2026, 8, 28, 12, 0)) == "BUY_CE"


def test_down_setup_gives_buy_pe():
    ctx = _context(ORB_BREAK_DOWN=True)
    assert validated_action(ctx, datetime(2026, 8, 28, 12, 0)) == "BUY_PE"


def test_pdl_break_has_no_suffix_but_means_down():
    ctx = _context(PDL_BREAK=True)
    assert validated_action(ctx, datetime(2026, 8, 28, 12, 0)) == "BUY_PE"


def test_no_matching_setup_declines():
    ctx = _context(RANGE_REGIME=True, EXTENDED_FROM_MEAN=True)
    assert validated_action(ctx, datetime(2026, 8, 28, 12, 0)) is None


def test_both_directions_active_is_ambiguous_and_declines():
    ctx = _context(EMA_STACK_UP=True, ORB_BREAK_DOWN=True)
    assert validated_action(ctx, datetime(2026, 8, 28, 12, 0)) is None


# ---------------------------------------------------------------------------
# _has_open_validated_trade
# ---------------------------------------------------------------------------

def test_no_open_trade_when_table_empty():
    db = _make_session()
    assert _has_open_validated_trade(db, "BANKNIFTY") is False


def test_true_when_a_validated_signal_trade_is_open():
    db = _make_session()
    _add_trade(db, trade_id="t1")
    assert _has_open_validated_trade(db, "BANKNIFTY") is True


def test_false_when_open_trade_belongs_to_a_different_origin():
    db = _make_session()
    _add_trade(db, trade_id="t1", origin="AI_ORIGIN_OPENAI")
    assert _has_open_validated_trade(db, "BANKNIFTY") is False


def test_false_when_the_validated_signal_trade_is_already_closed():
    db = _make_session()
    _add_trade(db, trade_id="t1", status=TradeStatus.CLOSED)
    assert _has_open_validated_trade(db, "BANKNIFTY") is False


def test_false_when_open_trade_is_on_a_different_index():
    db = _make_session()
    _add_trade(db, trade_id="t1", index_symbol="NIFTY")
    assert _has_open_validated_trade(db, "BANKNIFTY") is False


# ---------------------------------------------------------------------------
# open_validated_trade
# ---------------------------------------------------------------------------

def test_open_validated_trade_opens_a_paper_fixed_trade_with_correct_stop_target():
    db = _make_session()
    index = _make_index()
    ctx = _context(EMA_STACK_UP=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(option_type="CE"))

    trade = open_validated_trade(db, index, "BUY_CE", ctx, smartapi, option_finder)

    assert trade is not None
    assert trade.origin == ORIGIN
    assert trade.mode == TradingMode.PAPER
    assert trade.sl_mode == "FIXED"
    assert trade.status == TradeStatus.OPEN
    # No fitted coefficients in this sandbox -> symmetric_premium_percent is
    # a no-op, so the nominal 12%/20% apply directly.
    assert trade.stoploss == round(100.0 * (1 - 0.12), 2)
    assert trade.target == round(100.0 * (1 + 0.20), 2)
    assert "EMA_STACK_UP" in trade.ai_reasoning


def test_open_validated_trade_matched_setups_only_include_direction_matched_ones():
    db = _make_session()
    index = _make_index()
    ctx = _context(EMA_STACK_UP=True, ORB_BREAK_DOWN=True, TREND_REGIME=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(option_type="CE"))

    trade = open_validated_trade(db, index, "BUY_CE", ctx, smartapi, option_finder)

    assert trade is not None
    assert "EMA_STACK_UP" in trade.ai_reasoning
    assert "ORB_BREAK_DOWN" not in trade.ai_reasoning
    assert "TREND_REGIME" not in trade.ai_reasoning


def test_open_validated_trade_declines_when_no_contract_far_enough_out(monkeypatch):
    import app.validated_signal as module
    monkeypatch.setattr(module, "days_to_expiry", lambda expiry, as_of: 2)

    db = _make_session()
    index = _make_index()
    ctx = _context(EMA_STACK_UP=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_validated_trade(db, index, "BUY_CE", ctx, smartapi, option_finder)

    assert trade is None


def test_open_validated_trade_handles_contract_resolution_failure():
    db = _make_session()
    index = _make_index()
    ctx = _context(EMA_STACK_UP=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(None)

    trade = open_validated_trade(db, index, "BUY_CE", ctx, smartapi, option_finder)

    assert trade is None


def test_open_validated_trade_handles_missing_ltp():
    db = _make_session()
    index = _make_index()
    ctx = _context(EMA_STACK_UP=True)
    smartapi = FakeSmartAPI(price=None)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_validated_trade(db, index, "BUY_CE", ctx, smartapi, option_finder)

    assert trade is None


def test_open_validated_trade_never_places_a_real_order():
    # FakeSmartAPI.place_market_order raises if ever called -- this module
    # must have no live-order path at all, regardless of index config.
    db = _make_session()
    index = _make_index()
    ctx = _context(EMA_STACK_UP=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(option_type="CE"))

    trade = open_validated_trade(db, index, "BUY_CE", ctx, smartapi, option_finder)

    assert trade is not None
    assert trade.mode == TradingMode.PAPER


# ---------------------------------------------------------------------------
# check_validated_signal
# ---------------------------------------------------------------------------

def test_check_skips_entirely_when_a_position_is_already_open():
    db = _make_session()
    index = _make_index()
    _add_trade(db, trade_id="t1")
    ctx = _context(EMA_STACK_UP=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(option_type="CE"))

    result = check_validated_signal(db, index, ctx, datetime(2026, 8, 28, 12, 0), smartapi, option_finder)

    assert result is None
    assert option_finder.calls == 0  # never even tries to resolve a contract


def test_check_skips_when_no_validated_action():
    db = _make_session()
    index = _make_index()
    ctx = _context(RANGE_REGIME=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    result = check_validated_signal(db, index, ctx, datetime(2026, 8, 28, 12, 0), smartapi, option_finder)

    assert result is None
    assert option_finder.calls == 0


def test_check_opens_a_trade_end_to_end():
    db = _make_session()
    index = _make_index()
    ctx = _context(ST_ALIGNED_UP=True)
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract(option_type="CE"))

    result = check_validated_signal(db, index, ctx, datetime(2026, 8, 28, 12, 0), smartapi, option_finder)

    assert result is not None
    assert result.signal == "BUY_CE"
    assert result.origin == ORIGIN
