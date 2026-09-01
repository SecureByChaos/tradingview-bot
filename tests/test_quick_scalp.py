from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_data import Bar
from app.models import OptionContract, Signal
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST, to_ist, utc_now
from app.quick_scalp import (
    ORIGIN,
    _has_open_quick_scalp_trade,
    check_quick_scalp_entry,
    check_quick_scalp_exits,
    open_quick_scalp_trade,
    quick_scalp_action,
    run_quick_scalp_checks,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True)


def _add_trade(db, *, trade_id, index_symbol="BANKNIFTY", origin=ORIGIN, status=TradeStatus.OPEN,
                current_premium=100.0, entry_time=None) -> None:
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Quick Scalp - Bank Nifty", signal="BUY_CE",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=100.0, current_premium=current_premium, stoploss=97.0, target=105.0,
        entry_time=entry_time or utc_now(), origin=origin, status=status,
        result=TradeResult.OPEN if status == TradeStatus.OPEN else TradeResult.WIN,
        mode=TradingMode.PAPER,
    ))
    db.commit()


def _bars(n: int) -> list[Bar]:
    now = utc_now()
    return [Bar(ts_ist=now, open=100.0, high=100.0, low=100.0, close=100.0) for _ in range(n)]


class FakeSmartAPI:
    def __init__(self, price: float | None = 100.0) -> None:
        self.price = price

    def get_ltp(self, *_args, **_kwargs) -> float | None:
        return self.price

    def get_candles(self, *_args, **_kwargs):
        return []

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("Quick Scalp must never place a real order")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract | None) -> None:
        self.contract = contract
        self.calls = 0

    def find_atm_contract(self, signal: Signal, index: IndexConfig, offset: int, min_dte: int | None = None) -> OptionContract:
        self.calls += 1
        if self.contract is None:
            raise ValueError("no contract available")
        return self.contract


class FakeTelegram:
    def send(self, *_args, **_kwargs) -> None:
        raise AssertionError("Quick Scalp trades must never notify Telegram")


def _make_contract(dte_days: int = 8) -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=dte_days)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol=f"BANKNIFTY{expiry}57000CE", symboltoken="123", strike=57000,
        expiry=expiry, option_type="CE", lot_size=35,
    )


def _make_trade_manager() -> MultiStrategyTradeManager:
    return MultiStrategyTradeManager(None, FakeSmartAPI(), FakeOptionFinder(None), FakeTelegram())


# ---------------------------------------------------------------------------
# quick_scalp_action
# ---------------------------------------------------------------------------

def _patch_indicators(monkeypatch, module, *, fast, slow, rsi_values):
    def _fake_ema(values, period):
        if period == module._EMA_FAST:
            return fast
        if period == module._EMA_SLOW:
            return slow
        raise AssertionError(f"unexpected EMA period {period}")

    monkeypatch.setattr(module, "ema", _fake_ema)
    monkeypatch.setattr(module, "rsi", lambda bars, period: rsi_values)


def test_crossover_up_with_rsi_confirmation_gives_buy_ce(monkeypatch):
    import app.quick_scalp as module
    _patch_indicators(monkeypatch, module, fast=[10.0, 10.5], slow=[10.2, 10.2], rsi_values=[50.0, 60.0])
    assert quick_scalp_action(_bars(2)) == "BUY_CE"


def test_crossover_up_without_rsi_confirmation_declines(monkeypatch):
    import app.quick_scalp as module
    _patch_indicators(monkeypatch, module, fast=[10.0, 10.5], slow=[10.2, 10.2], rsi_values=[50.0, 50.0])
    assert quick_scalp_action(_bars(2)) is None


def test_crossover_down_with_rsi_confirmation_gives_buy_pe(monkeypatch):
    import app.quick_scalp as module
    _patch_indicators(monkeypatch, module, fast=[10.2, 9.8], slow=[10.0, 10.0], rsi_values=[50.0, 40.0])
    assert quick_scalp_action(_bars(2)) == "BUY_PE"


def test_crossover_down_without_rsi_confirmation_declines(monkeypatch):
    import app.quick_scalp as module
    _patch_indicators(monkeypatch, module, fast=[10.2, 9.8], slow=[10.0, 10.0], rsi_values=[50.0, 50.0])
    assert quick_scalp_action(_bars(2)) is None


def test_no_crossover_declines(monkeypatch):
    import app.quick_scalp as module
    _patch_indicators(monkeypatch, module, fast=[11.0, 11.2], slow=[10.0, 10.0], rsi_values=[60.0, 62.0])
    assert quick_scalp_action(_bars(2)) is None


def test_cold_indicators_decline(monkeypatch):
    import app.quick_scalp as module
    _patch_indicators(monkeypatch, module, fast=[None, 10.5], slow=[None, 10.2], rsi_values=[None, 60.0])
    assert quick_scalp_action(_bars(2)) is None


def test_fewer_than_two_bars_declines():
    assert quick_scalp_action(_bars(1)) is None
    assert quick_scalp_action([]) is None


# ---------------------------------------------------------------------------
# _has_open_quick_scalp_trade
# ---------------------------------------------------------------------------

def test_no_open_trade_when_table_empty():
    db = _make_session()
    assert _has_open_quick_scalp_trade(db, "BANKNIFTY") is False


def test_true_when_a_quick_scalp_trade_is_open():
    db = _make_session()
    _add_trade(db, trade_id="t1")
    assert _has_open_quick_scalp_trade(db, "BANKNIFTY") is True


def test_false_when_open_trade_belongs_to_a_different_origin():
    db = _make_session()
    _add_trade(db, trade_id="t1", origin="AI_ORIGIN_OPENAI")
    assert _has_open_quick_scalp_trade(db, "BANKNIFTY") is False


def test_false_when_the_trade_is_already_closed():
    db = _make_session()
    _add_trade(db, trade_id="t1", status=TradeStatus.CLOSED)
    assert _has_open_quick_scalp_trade(db, "BANKNIFTY") is False


# ---------------------------------------------------------------------------
# open_quick_scalp_trade
# ---------------------------------------------------------------------------

def test_open_quick_scalp_trade_opens_a_paper_fixed_trade_with_correct_stop_target():
    db = _make_session()
    index = _make_index()
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_quick_scalp_trade(db, index, "BUY_CE", smartapi, option_finder)

    assert trade is not None
    assert trade.origin == ORIGIN
    assert trade.mode == TradingMode.PAPER
    assert trade.sl_mode == "FIXED"
    assert trade.status == TradeStatus.OPEN
    # No fitted coefficients in this sandbox -> symmetric_premium_percent is
    # a no-op, so the nominal 3%/5% apply directly.
    assert trade.stoploss == round(100.0 * (1 - 0.03), 2)
    assert trade.target == round(100.0 * (1 + 0.05), 2)


def test_open_quick_scalp_trade_declines_when_dte_floor_not_met(monkeypatch):
    import app.quick_scalp as module
    monkeypatch.setattr(module, "days_to_expiry", lambda expiry, as_of: 2)

    db = _make_session()
    index = _make_index()
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_quick_scalp_trade(db, index, "BUY_CE", smartapi, option_finder)

    assert trade is None


def test_open_quick_scalp_trade_handles_contract_resolution_failure():
    db = _make_session()
    index = _make_index()
    trade = open_quick_scalp_trade(db, index, "BUY_CE", FakeSmartAPI(price=100.0), FakeOptionFinder(None))
    assert trade is None


def test_open_quick_scalp_trade_handles_missing_ltp():
    db = _make_session()
    index = _make_index()
    trade = open_quick_scalp_trade(db, index, "BUY_CE", FakeSmartAPI(price=None), FakeOptionFinder(_make_contract()))
    assert trade is None


def test_open_quick_scalp_trade_never_places_a_real_order():
    db = _make_session()
    index = _make_index()
    trade = open_quick_scalp_trade(db, index, "BUY_CE", FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()))
    assert trade is not None
    assert trade.mode == TradingMode.PAPER


# ---------------------------------------------------------------------------
# check_quick_scalp_exits
# ---------------------------------------------------------------------------

def test_check_exits_squares_off_at_max_hold(monkeypatch):
    # entry_time and the monkeypatched "now" are both expressed in real UTC
    # -- SQLite doesn't round-trip tzinfo (see CLAUDE.md's own documented
    # gotcha), so entry_time comes back naive and to_ist() reinterprets it
    # as UTC; mixing an IST-tzinfo "now" with a naive-UTC-on-readback
    # entry_time would silently compute the wrong duration.
    import app.quick_scalp as module
    now = datetime(2026, 8, 31, 6, 30, tzinfo=UTC)
    monkeypatch.setattr(module, "utc_now", lambda: now)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=101.0, entry_time=now - timedelta(minutes=16))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "MAX_HOLD_EXIT"
    assert trade.exit_price == 101.0


def test_check_exits_does_not_square_off_before_max_hold(monkeypatch):
    import app.quick_scalp as module
    now = datetime(2026, 8, 31, 6, 30, tzinfo=UTC)
    monkeypatch.setattr(module, "utc_now", lambda: now)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=101.0, entry_time=now - timedelta(minutes=10))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_isolated_from_other_origins(monkeypatch):
    import app.quick_scalp as module
    now = datetime(2026, 8, 31, 6, 30, tzinfo=UTC)
    monkeypatch.setattr(module, "utc_now", lambda: now)
    db = _make_session()
    _add_trade(db, trade_id="t-signal", origin="SIGNAL", current_premium=101.0, entry_time=now - timedelta(minutes=30))
    trade_manager = _make_trade_manager()

    check_quick_scalp_exits(db, trade_manager)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t-signal").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_never_touches_telegram_or_strategy_stats():
    # FakeTelegram.send raises if ever called -- close_trade's own
    # is_ai_alternative branch (origin != "SIGNAL") must skip it entirely.
    from app.models import ExitReason

    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()
    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()

    trade_manager.close_trade(db, trade, 105.0, ExitReason.MAX_HOLD_EXIT)

    assert trade.status == TradeStatus.CLOSED


# ---------------------------------------------------------------------------
# check_quick_scalp_entry
# ---------------------------------------------------------------------------

def test_check_entry_skips_when_position_already_open(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    _add_trade(db, trade_id="t1")
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "quick_scalp_action", lambda bars: (_ for _ in ()).throw(AssertionError("must not check signal")))

    result = check_quick_scalp_entry(db, index, _bars(30), FakeSmartAPI(price=100.0), option_finder)

    assert result is None
    assert option_finder.calls == 0


def test_check_entry_opens_a_trade_on_a_signal(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "quick_scalp_action", lambda bars: "BUY_PE")

    result = check_quick_scalp_entry(db, index, _bars(30), FakeSmartAPI(price=100.0), option_finder)

    assert result is not None
    assert result.signal == "BUY_PE"
    assert result.origin == ORIGIN


def test_check_entry_no_signal_opens_nothing(monkeypatch):
    import app.quick_scalp as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "quick_scalp_action", lambda bars: None)

    result = check_quick_scalp_entry(db, index, _bars(30), FakeSmartAPI(price=100.0), option_finder)

    assert result is None
    assert option_finder.calls == 0


# ---------------------------------------------------------------------------
# run_quick_scalp_checks (end-to-end wiring)
# ---------------------------------------------------------------------------

def test_run_quick_scalp_checks_skips_without_dependencies(caplog):
    with caplog.at_level("INFO"):
        run_quick_scalp_checks(None, None, None)
    assert "Skipped" in caplog.text


def test_run_quick_scalp_checks_skips_outside_market_hours(monkeypatch):
    import app.quick_scalp as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 20, 0, tzinfo=IST))  # night
    db = _make_session()

    def _exploding(*a, **k):
        raise AssertionError("must not reach entry/exit checks outside market hours")

    monkeypatch.setattr(module, "check_quick_scalp_exits", _exploding)

    run_quick_scalp_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), db=db)


def test_run_quick_scalp_checks_opens_a_trade_end_to_end(monkeypatch):
    import app.quick_scalp as module

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))  # Monday, trading hours
    db = _make_session()
    db.add(_make_index())
    db.commit()

    monkeypatch.setattr(module, "_refresh_bars", lambda *a, **k: _bars(30))
    monkeypatch.setattr(module, "quick_scalp_action", lambda bars: "BUY_CE")
    option_finder = FakeOptionFinder(_make_contract())

    run_quick_scalp_checks(FakeSmartAPI(price=100.0), option_finder, _make_trade_manager(), db=db)

    trades = db.query(StrategyTrade).filter(StrategyTrade.origin == ORIGIN).all()
    assert len(trades) == 1
    assert trades[0].signal == "BUY_CE"
