from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import OptionContract, Signal
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST, to_ist, utc_now
from app.ai.autonomous import (
    ORIGIN,
    _build_entry_prompt,
    _build_exit_prompt,
    _has_open_autonomous_trade,
    _parse_entry_response,
    _parse_exit_response,
    check_autonomous_entry,
    check_autonomous_exits,
    open_autonomous_trade,
    run_autonomous_checks,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True)


def _add_trade(db, *, trade_id, index_symbol="BANKNIFTY", origin=ORIGIN, status=TradeStatus.OPEN,
                current_premium=100.0, entry_price=100.0, stoploss=65.0, target=150.0) -> None:
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Autonomous AI - Bank Nifty", signal="BUY_CE",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, current_premium=current_premium, stoploss=stoploss, target=target,
        entry_time=utc_now(), origin=origin, status=status,
        result=TradeResult.OPEN if status == TradeStatus.OPEN else TradeResult.WIN,
        mode=TradingMode.PAPER,
    ))
    db.commit()


class FakeSmartAPI:
    def __init__(self, price: float | None = 100.0, spot: float = 57000.0) -> None:
        self.price = price
        self.spot = spot

    def get_ltp(self, *_args, **_kwargs) -> float | None:
        return self.price

    def get_index_spot(self, _index) -> float:
        return self.spot

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("Autonomous AI must never place a real order")


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
        raise AssertionError("Autonomous AI trades must never notify Telegram")


def _make_contract(dte_days: int = 8) -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=dte_days)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol=f"BANKNIFTY{expiry}57000CE", symboltoken="123", strike=57000,
        expiry=expiry, option_type="CE", lot_size=35,
    )


def _make_trade_manager(smartapi=None) -> MultiStrategyTradeManager:
    return MultiStrategyTradeManager(None, smartapi or FakeSmartAPI(), FakeOptionFinder(None), FakeTelegram())


class _Settings:
    def __init__(self, provider="openai", enabled=True, mode="LIVE"):
        self.provider = provider
        self.model = "gpt-x"
        self.api_key = "key"
        self.base_url = ""
        self.timeout_seconds = 10
        self.enabled = enabled
        self.mode = mode


# ---------------------------------------------------------------------------
# _parse_entry_response / _parse_exit_response
# ---------------------------------------------------------------------------

def test_parse_entry_response_valid_buy_ce():
    d = _parse_entry_response('{"decision": "BUY_CE", "confidence": 0.7, "reasoning": "price rising"}')
    assert d.action == "BUY_CE"
    assert d.confidence == 0.7


def test_parse_entry_response_rejects_unknown_decision():
    d = _parse_entry_response('{"decision": "MAYBE"}')
    assert d.action == "ERROR"


def test_parse_entry_response_handles_none_text():
    d = _parse_entry_response(None)
    assert d.action == "ERROR"


def test_parse_entry_response_unwraps_markdown_fence():
    d = _parse_entry_response('```json\n{"decision": "NONE", "reasoning": "nothing clear"}\n```')
    assert d.action == "NONE"


def test_parse_exit_response_valid_exit():
    d = _parse_exit_response('{"decision": "EXIT", "confidence": 0.9, "reasoning": "target-ish gain"}')
    assert d.action == "EXIT"


def test_parse_exit_response_valid_hold():
    d = _parse_exit_response('{"decision": "HOLD", "reasoning": "still developing"}')
    assert d.action == "HOLD"


def test_parse_exit_response_rejects_unknown_decision():
    d = _parse_exit_response('{"decision": "SELL"}')
    assert d.action == "ERROR"


# ---------------------------------------------------------------------------
# _build_entry_prompt / _build_exit_prompt
# ---------------------------------------------------------------------------

def test_build_entry_prompt_has_no_indicator_language():
    row = {"display_name": "Bank Nifty", "price": 57000.0, "change_abs": 120.5, "change_percent": 0.21,
           "day_low": 56800.0, "day_high": 57100.0}
    now_ist = to_ist(utc_now())
    prompt = _build_entry_prompt(row, now_ist, (15, 15))
    for forbidden in ("ADX", "EMA", "RSI", "Supertrend", "CPR", "regime"):
        assert forbidden not in prompt
    assert "57000.0" in prompt


def test_build_entry_prompt_omits_missing_change_and_range():
    row = {"display_name": "Nifty", "price": 24000.0, "change_abs": None, "change_percent": None,
           "day_low": None, "day_high": None}
    now_ist = to_ist(utc_now())
    prompt = _build_entry_prompt(row, now_ist, (15, 15))
    assert "Change vs previous close" not in prompt
    assert "Today's range" not in prompt


def test_build_exit_prompt_includes_backstop_as_informational():
    db = _make_session()
    trade = StrategyTrade(
        trade_id="t1", strategy_name="x", signal="BUY_CE", index_symbol="BANKNIFTY",
        tradingsymbol="X", symboltoken="1", strike=57000, expiry="28AUG2026", option_type="CE",
        quantity=35, entry_price=100.0, current_premium=110.0, stoploss=65.0, target=150.0,
        entry_time=utc_now(), origin=ORIGIN, status=TradeStatus.OPEN, pnl_percent=10.0,
        mode=TradingMode.PAPER,
    )
    prompt = _build_exit_prompt(trade, to_ist(utc_now()))
    assert "informational" in prompt
    assert "65.0" in prompt and "150.0" in prompt


# ---------------------------------------------------------------------------
# _has_open_autonomous_trade
# ---------------------------------------------------------------------------

def test_no_open_trade_when_table_empty():
    db = _make_session()
    assert _has_open_autonomous_trade(db, "BANKNIFTY") is False


def test_true_when_an_autonomous_trade_is_open():
    db = _make_session()
    _add_trade(db, trade_id="t1")
    assert _has_open_autonomous_trade(db, "BANKNIFTY") is True


def test_false_when_open_trade_belongs_to_a_different_origin():
    db = _make_session()
    _add_trade(db, trade_id="t1", origin="AI_ORIGIN_OPENAI")
    assert _has_open_autonomous_trade(db, "BANKNIFTY") is False


def test_false_when_the_trade_is_already_closed():
    db = _make_session()
    _add_trade(db, trade_id="t1", status=TradeStatus.CLOSED)
    assert _has_open_autonomous_trade(db, "BANKNIFTY") is False


# ---------------------------------------------------------------------------
# open_autonomous_trade
# ---------------------------------------------------------------------------

def test_open_autonomous_trade_opens_a_paper_fixed_trade_with_backstop_stop_target():
    db = _make_session()
    index = _make_index()
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_autonomous_trade(db, index, "BUY_CE", "clear upward move", smartapi, option_finder)

    assert trade is not None
    assert trade.origin == ORIGIN
    assert trade.mode == TradingMode.PAPER
    assert trade.sl_mode == "FIXED"
    assert trade.status == TradeStatus.OPEN
    # No fitted coefficients in this sandbox -> symmetric_premium_percent is
    # a no-op, so the nominal 35%/50% backstop applies directly.
    assert trade.stoploss == round(100.0 * (1 - 0.35), 2)
    assert trade.target == round(100.0 * (1 + 0.50), 2)
    assert trade.ai_reasoning == "clear upward move"


def test_open_autonomous_trade_declines_when_dte_floor_not_met(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "days_to_expiry", lambda expiry, as_of: 2)

    db = _make_session()
    index = _make_index()
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_autonomous_trade(db, index, "BUY_CE", "reason", smartapi, option_finder)

    assert trade is None


def test_open_autonomous_trade_handles_contract_resolution_failure():
    db = _make_session()
    index = _make_index()
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(None)

    trade = open_autonomous_trade(db, index, "BUY_CE", "reason", smartapi, option_finder)

    assert trade is None


def test_open_autonomous_trade_handles_missing_ltp():
    db = _make_session()
    index = _make_index()
    smartapi = FakeSmartAPI(price=None)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_autonomous_trade(db, index, "BUY_CE", "reason", smartapi, option_finder)

    assert trade is None


def test_open_autonomous_trade_never_places_a_real_order():
    db = _make_session()
    index = _make_index()
    smartapi = FakeSmartAPI(price=100.0)
    option_finder = FakeOptionFinder(_make_contract())

    trade = open_autonomous_trade(db, index, "BUY_CE", "reason", smartapi, option_finder)

    assert trade is not None
    assert trade.mode == TradingMode.PAPER


# ---------------------------------------------------------------------------
# check_autonomous_entry
# ---------------------------------------------------------------------------

def test_check_entry_skips_when_position_already_open(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    _add_trade(db, trade_id="t1")
    option_finder = FakeOptionFinder(_make_contract())

    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model")))

    result = check_autonomous_entry(
        db, index, {"price": 57000.0, "display_name": "Bank Nifty", "change_abs": None, "change_percent": None,
                     "day_low": None, "day_high": None},
        to_ist(utc_now()), (15, 15), _Settings(), FakeSmartAPI(), option_finder,
    )
    assert result is None
    assert option_finder.calls == 0


def test_check_entry_skips_when_no_live_price(monkeypatch):
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model")))

    result = check_autonomous_entry(db, index, None, to_ist(utc_now()), (15, 15), _Settings(), FakeSmartAPI(), option_finder)
    assert result is None


def test_check_entry_opens_a_trade_on_buy_decision(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())

    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "BUY_PE", "confidence": 0.6, "reasoning": "drifting down"}', None, 12.0))

    result = check_autonomous_entry(
        db, index, {"price": 57000.0, "display_name": "Bank Nifty", "change_abs": -50.0, "change_percent": -0.09,
                     "day_low": 56900.0, "day_high": 57200.0},
        to_ist(utc_now()), (15, 15), _Settings(), FakeSmartAPI(price=100.0), option_finder,
    )
    assert result is not None
    assert result.signal == "BUY_PE"
    assert result.ai_reasoning == "drifting down"


def test_check_entry_none_decision_opens_nothing(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())

    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "NONE", "reasoning": "nothing clear"}', None, 12.0))

    result = check_autonomous_entry(
        db, index, {"price": 57000.0, "display_name": "Bank Nifty", "change_abs": None, "change_percent": None,
                     "day_low": None, "day_high": None},
        to_ist(utc_now()), (15, 15), _Settings(), FakeSmartAPI(price=100.0), option_finder,
    )
    assert result is None
    assert option_finder.calls == 0


def test_check_entry_provider_error_opens_nothing(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())

    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: module._RawCall(None, "HTTP 500", None))

    result = check_autonomous_entry(
        db, index, {"price": 57000.0, "display_name": "Bank Nifty", "change_abs": None, "change_percent": None,
                     "day_low": None, "day_high": None},
        to_ist(utc_now()), (15, 15), _Settings(), FakeSmartAPI(price=100.0), option_finder,
    )
    assert result is None
    assert option_finder.calls == 0


# ---------------------------------------------------------------------------
# check_autonomous_exits
# ---------------------------------------------------------------------------

def _before_cutoff(monkeypatch, module) -> None:
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))


def test_check_exits_closes_trade_on_exit_decision(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=140.0)
    trade_manager = _make_trade_manager()

    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "EXIT", "confidence": 0.8, "reasoning": "good gain, taking it"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "AI_DISCRETION_EXIT"
    assert trade.exit_price == 140.0


def test_check_exits_leaves_trade_open_on_hold_decision(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()

    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_leaves_trade_open_when_provider_errors(monkeypatch):
    # The safe default on a failed exit call is to do nothing -- the
    # mechanical backstop stop/target protects the position, not a forced
    # exit on a transient API failure.
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()

    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: module._RawCall(None, "timeout", None))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_never_touches_telegram_or_strategy_stats():
    # FakeTelegram.send raises if ever called -- close_trade's own
    # is_ai_alternative branch (origin != "SIGNAL") must skip it entirely
    # for this origin, the same isolation every non-SIGNAL origin gets.
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=200.0)
    trade_manager = _make_trade_manager()

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    trade_manager.close_trade(db, trade, 200.0, __import__("app.models", fromlist=["ExitReason"]).ExitReason.AI_DISCRETION_EXIT)

    assert trade.status == TradeStatus.CLOSED


def test_check_exits_isolated_from_other_origins(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    _add_trade(db, trade_id="t-signal", origin="SIGNAL", current_premium=200.0)
    trade_manager = _make_trade_manager()

    def _exploding(*a, **k):
        raise AssertionError("must never call the model for a non-Autonomous-AI trade")

    monkeypatch.setattr(module, "_call_provider", _exploding)
    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t-signal").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_squares_off_unconditionally_at_cutoff_with_no_model_call(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 15, 0, tzinfo=IST))
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()

    def _exploding(*a, **k):
        raise AssertionError("must not call the model once past the cutoff -- it's a hard square-off")

    monkeypatch.setattr(module, "_call_provider", _exploding)

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "TIME_EXIT"
    assert trade.exit_price == 105.0


def test_check_exits_squares_off_after_cutoff_too_not_only_exactly_at_it(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 15, 7, tzinfo=IST))
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call")))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "TIME_EXIT"


def test_check_exits_does_not_square_off_before_cutoff(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 14, 59, tzinfo=IST))
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_autonomous_exits -- AUTONOMOUS_STALL_EXIT (3 Sep 2026)
# ---------------------------------------------------------------------------
#
# "Now" is frozen at 2026-08-31 12:00 IST (06:30 UTC). entry_time is stored
# with a real UTC tzinfo -- SQLite strips tzinfo on the round-trip through
# check_autonomous_exits' own query, and to_ist() then correctly treats the
# naive result as UTC and adds +5:30 -- same documented gotcha (and same
# fix) as every other duration-sensitive test in this project.

def _add_trade_at(db, *, trade_id, entry_time_utc, pnl_percent, current_premium=100.0, entry_price=100.0) -> None:
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Autonomous AI - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, current_premium=current_premium, pnl_percent=pnl_percent,
        stoploss=65.0, target=150.0, entry_time=entry_time_utc, origin=ORIGIN,
        status=TradeStatus.OPEN, result=TradeResult.OPEN, mode=TradingMode.PAPER,
    ))
    db.commit()


def test_check_exits_closes_a_stalled_trade_with_no_model_call(monkeypatch):
    import app.ai.autonomous as module
    from datetime import UTC
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    # 90 minutes before the frozen "now" -- past the 60-minute stall window.
    _add_trade_at(db, trade_id="t1", entry_time_utc=datetime(2026, 8, 31, 5, 0, tzinfo=UTC), pnl_percent=1.5)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call for a stalled trade")))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "AUTONOMOUS_STALL_EXIT"


def test_check_exits_does_not_stall_before_the_window_elapses(monkeypatch):
    import app.ai.autonomous as module
    from datetime import UTC
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    # 30 minutes before "now" -- inside the 60-minute stall window, so this
    # must still reach the model's own HOLD/EXIT judgment as normal.
    _add_trade_at(db, trade_id="t1", entry_time_utc=datetime(2026, 8, 31, 6, 0, tzinfo=UTC), pnl_percent=1.5)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_does_not_stall_a_trade_that_has_moved(monkeypatch):
    import app.ai.autonomous as module
    from datetime import UTC
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    # 90 minutes elapsed (past the window) but +8% P&L is outside the +-5%
    # stall band -- a real, moving trade must still reach the model.
    _add_trade_at(db, trade_id="t1", entry_time_utc=datetime(2026, 8, 31, 5, 0, tzinfo=UTC), pnl_percent=8.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# run_autonomous_checks (end-to-end wiring)
# ---------------------------------------------------------------------------

def test_run_autonomous_checks_skips_without_dependencies(caplog):
    with caplog.at_level("INFO"):
        run_autonomous_checks(None, None, None)
    assert "Skipped" in caplog.text


def test_run_autonomous_checks_skips_outside_market_hours(monkeypatch):
    import app.ai.autonomous as module
    from datetime import datetime
    from app.time_utils import IST

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 20, 0, tzinfo=IST))  # night
    db = _make_session()

    def _exploding(*a, **k):
        raise AssertionError("must not reach settings lookup outside market hours")

    monkeypatch.setattr(module, "get_settings", _exploding)

    run_autonomous_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), db=db)


def test_run_autonomous_checks_blocks_new_entries_at_the_dedicated_3pm_cutoff(monkeypatch):
    # Deliberately EARLIER than the shared Settings > General square-off
    # time (15:15 default) other strategies use -- _TRADING_END is a
    # dedicated, decoupled constant for this strategy alone (see its own
    # comment in app/ai/autonomous.py).
    import app.ai.autonomous as module
    from app.ai.repository import create_settings

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 15, 0, tzinfo=IST))
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai")
    db.commit()

    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("no new entries at/after the 3pm cutoff")))

    run_autonomous_checks(FakeSmartAPI(), option_finder, _make_trade_manager(), db=db)

    assert option_finder.calls == 0


def test_run_autonomous_checks_still_enters_before_the_3pm_cutoff(monkeypatch):
    import app.ai.autonomous as module
    from app.ai.repository import create_settings

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai")
    db.commit()

    option_finder = FakeOptionFinder(_make_contract())
    calls = []
    monkeypatch.setattr(
        module, "_call_provider",
        lambda *a, **k: calls.append(1) or module._RawCall('{"decision": "NONE", "reasoning": "nothing clear"}', None, 5.0),
    )

    run_autonomous_checks(FakeSmartAPI(price=57000.0), option_finder, _make_trade_manager(), db=db)

    # The model WAS actually asked (declined) -- confirms 12:00 IST is
    # correctly inside the trading window, not just "no trade opened" which
    # would also be true if the cycle were skipped entirely.
    assert len(calls) == 1
