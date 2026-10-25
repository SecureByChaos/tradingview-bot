from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.ai.agentic as agentic_module
from app.ai.agentic import (
    _build_entry_prompt,
    _build_exit_prompt,
    _call_openai_live,
    _has_open_agentic_trade,
    _parse_entry_response,
    _parse_exit_response,
    check_agentic_entry,
    check_agentic_exits,
    open_agentic_trade,
    run_agentic_entry_exit_checks,
)
from app.db_models import AgenticAISettings, Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus
from app.models import ExitReason, OptionContract
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST, to_ist, utc_now


def _make_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index(symbol: str = "NIFTY", enabled: bool = True) -> IndexConfig:
    return IndexConfig(symbol=symbol, display_name="Nifty 50", enabled=enabled, lot_size=75, strike_interval=50)


def _make_contract(dte_days: int = 8, lot_size: int = 75) -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=dte_days)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol=f"NIFTY{expiry}22500CE", symboltoken="1", strike=22500,
        expiry=expiry, option_type="CE", lot_size=lot_size,
    )


class FakeSmartAPI:
    def __init__(self, price: float | None = 100.0) -> None:
        self.price = price
        self.calls = 0

    def get_ltp(self, *_args, **_kwargs):
        self.calls += 1
        return self.price


class FakeOptionFinder:
    def __init__(self, contract: OptionContract | None) -> None:
        self.contract = contract
        self.calls = 0

    def find_atm_contract(self, signal, index, offset, min_dte=None):
        self.calls += 1
        if self.contract is None:
            raise ValueError("no contract available")
        return self.contract


class FakeTelegram:
    def send(self, *_args, **_kwargs) -> None:
        raise AssertionError("Agentic AI trades must never notify Telegram")


def _make_trade_manager(smartapi=None) -> MultiStrategyTradeManager:
    return MultiStrategyTradeManager(None, smartapi or FakeSmartAPI(), FakeOptionFinder(None), FakeTelegram())


def _ist_now(y=2026, m=10, d=25, hh=11, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _identity_rescale(nominal, index_symbol, option_type, dte):
    return nominal, True


def _make_settings(**overrides) -> AgenticAISettings:
    """AgenticAISettings(id=1) alone, never added/committed to a session,
    leaves every column at its raw Python None -- SQLAlchemy's default=
    only applies at flush/INSERT time, not at plain construction. Every
    field check_risk_gates()/compute_position_size()/open_agentic_trade()
    actually read is given an explicit value here so these tests work
    whether or not the returned object is ever committed."""
    base = dict(
        id=1, enabled=True, api_key="sk-real", base_url="", live_model="gpt-6-luna",
        live_reasoning_effort="medium", qa_model="", qa_reasoning_effort="max", timeout_seconds=20,
        account_capital_rupees=100000.0, max_risk_percent_per_trade=1.0, max_daily_loss_percent=3.0,
        max_concurrent_positions=1, max_capital_deployed_percent=20.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    )
    base.update(overrides)
    return AgenticAISettings(**base)


def _seed_trade(db, trade_id, origin, status=TradeStatus.OPEN, current_premium=110.0, index_symbol="NIFTY"):
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name=f"{origin} - Nifty 50", signal="BUY_CE",
        index_symbol=index_symbol, option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=130.0,
        stoploss=85.0, entry_time=utc_now(), origin=origin, status=status,
        result=TradeResult.OPEN if status == TradeStatus.OPEN else TradeResult.WIN,
        current_premium=current_premium, highest_price=current_premium, lowest_price=100.0,
        pnl_percent=10.0, sl_mode="FIXED",
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_entry_response_valid_buy():
    result = _parse_entry_response('{"decision": "BUY_CE", "confidence": 0.7, "reasoning": "clean breakout"}')
    assert result.action == "BUY_CE"
    assert result.confidence == 0.7
    assert result.reasoning == "clean breakout"


def test_parse_entry_response_valid_none():
    result = _parse_entry_response('{"decision": "none", "confidence": 0.2, "reasoning": "unclear"}')
    assert result.action == "NONE"


def test_parse_entry_response_unwraps_markdown_fence():
    # CLAUDE.md's own documented gotcha -- LLMs wrap JSON in fences despite
    # being told not to; extract_json_object must handle this.
    text = '```json\n{"decision": "BUY_PE", "confidence": 0.6, "reasoning": "x"}\n```'
    result = _parse_entry_response(text)
    assert result.action == "BUY_PE"


def test_parse_entry_response_rejects_unknown_decision():
    result = _parse_entry_response('{"decision": "MAYBE", "confidence": 0.5, "reasoning": "x"}')
    assert result.action == "ERROR"


def test_parse_entry_response_no_text_is_error():
    assert _parse_entry_response(None).action == "ERROR"
    assert _parse_entry_response("").action == "ERROR"


def test_parse_exit_response_valid():
    result = _parse_exit_response('{"decision": "EXIT", "confidence": 0.8, "reasoning": "thesis broke"}')
    assert result.action == "EXIT"
    assert result.reasoning == "thesis broke"


def test_parse_exit_response_hold():
    result = _parse_exit_response('{"decision": "hold", "confidence": 0.6, "reasoning": "still fine"}')
    assert result.action == "HOLD"


def test_parse_exit_response_rejects_unknown_decision():
    result = _parse_exit_response('{"decision": "MAYBE"}')
    assert result.action == "ERROR"


# ---------------------------------------------------------------------------
# _call_openai_live -- only the pure, no-network early-return path (matches
# this project's existing testing depth for the identical _call_openai_qa
# wrapper -- its HTTP-calling branch is exercised indirectly in every
# check_agentic_entry/exits test below via a monkeypatched _call_openai_live,
# not by mocking the HTTP layer itself).
# ---------------------------------------------------------------------------


def test_call_openai_live_unconfigured_without_api_key():
    settings = AgenticAISettings(id=1, live_model="gpt-6-luna", api_key="")
    result = _call_openai_live(settings, "sys", "user")
    assert result.content is None
    assert "not configured" in result.error


def test_call_openai_live_unconfigured_without_model():
    settings = AgenticAISettings(id=1, live_model="", api_key="sk-real")
    result = _call_openai_live(settings, "sys", "user")
    assert result.content is None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def test_build_entry_prompt_includes_real_figures():
    figures = {"price": 22550.5, "change_abs": 45.5, "change_percent": 0.2, "day_low": 22480, "day_high": 22600}
    prompt = _build_entry_prompt("Nifty 50", figures)
    assert "22550.5" in prompt
    assert "+45.50" in prompt
    assert "22480" in prompt and "22600" in prompt


def test_build_entry_prompt_handles_missing_change_and_range():
    figures = {"price": 100.0, "change_abs": None, "change_percent": None, "day_low": None, "day_high": None}
    prompt = _build_entry_prompt("Nifty 50", figures)
    assert "unavailable" in prompt


def test_build_exit_prompt_includes_position_numbers():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    prompt = _build_exit_prompt(trade, to_ist(utc_now()))
    assert "110.0" in prompt
    assert "10.0" in prompt


# ---------------------------------------------------------------------------
# _has_open_agentic_trade
# ---------------------------------------------------------------------------


def test_has_open_agentic_trade_true():
    db = _make_db()
    _seed_trade(db, "t1", "AGENTIC_AI")
    assert _has_open_agentic_trade(db, "NIFTY") is True


def test_has_open_agentic_trade_false_when_closed():
    db = _make_db()
    _seed_trade(db, "t1", "AGENTIC_AI", status=TradeStatus.CLOSED)
    assert _has_open_agentic_trade(db, "NIFTY") is False


def test_has_open_agentic_trade_scoped_to_this_origin_only():
    db = _make_db()
    _seed_trade(db, "t1", "AUTONOMOUS_AI")
    assert _has_open_agentic_trade(db, "NIFTY") is False


# ---------------------------------------------------------------------------
# open_agentic_trade
# ---------------------------------------------------------------------------


def test_open_agentic_trade_opens_with_correct_fields(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    # Risk budget Rs 10,000 (10% of 100,000), per-unit risk 15 (100-85),
    # max_units = 666, lot size 75 -> floor(666/75) = 8 lots -> quantity 600.
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    )
    contract = _make_contract(lot_size=75)
    option_finder = FakeOptionFinder(contract)
    smartapi = FakeSmartAPI(price=100.0)

    trade = open_agentic_trade(db, index, "BUY_CE", "clean setup", settings, smartapi, option_finder)

    assert trade is not None
    assert trade.origin == "AGENTIC_AI"
    assert trade.mode == "PAPER"
    assert trade.sl_mode == "FIXED"
    assert trade.entry_price == 100.0
    assert trade.stoploss == 85.0  # 100 * (1 - 15/100)
    assert trade.target == 130.0  # 100 * (1 + 30/100)
    assert trade.quantity == 600
    assert trade.ai_action == "BUY_CE"
    assert trade.ai_reasoning == "clean setup"


def test_open_agentic_trade_declines_when_risk_budget_too_small_for_one_lot(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    # Same math as the comment above: 1% of Rs 100,000 = Rs 1,000 risk
    # budget, stop distance 15 (100 * 15%), lot size 75 -> 0 lots.
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_risk_percent_per_trade=1.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    )
    contract = _make_contract(lot_size=75)
    option_finder = FakeOptionFinder(contract)
    smartapi = FakeSmartAPI(price=100.0)

    trade = open_agentic_trade(db, index, "BUY_CE", "clean setup", settings, smartapi, option_finder)

    assert trade is None
    assert db.query(StrategyTrade).count() == 0


def test_open_agentic_trade_sizes_a_real_lot_when_budget_allows(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    # Risk budget Rs 10,000 (10% of 100,000), stop distance 15, lot size 75
    # -> max_units = 666 -> floor(666/75) = 8 lots -> quantity 600.
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    )
    contract = _make_contract(lot_size=75)
    option_finder = FakeOptionFinder(contract)
    smartapi = FakeSmartAPI(price=100.0)

    trade = open_agentic_trade(db, index, "BUY_CE", "clean setup", settings, smartapi, option_finder)

    assert trade is not None
    assert trade.quantity == 600
    assert trade.investment_amount == 60000.0


def test_open_agentic_trade_declines_when_contract_resolution_fails():
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    option_finder = FakeOptionFinder(None)
    smartapi = FakeSmartAPI()
    trade = open_agentic_trade(db, index, "BUY_CE", "x", settings, smartapi, option_finder)
    assert trade is None


def test_open_agentic_trade_declines_when_ltp_is_empty(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    contract = _make_contract()
    option_finder = FakeOptionFinder(contract)
    smartapi = FakeSmartAPI(price=None)
    trade = open_agentic_trade(db, index, "BUY_CE", "x", settings, smartapi, option_finder)
    assert trade is None


# ---------------------------------------------------------------------------
# check_agentic_entry -- gating order and the model never being asked when
# an earlier gate already blocks.
# ---------------------------------------------------------------------------


def _exploding_call(*_args, **_kwargs):
    raise AssertionError("the model must not be called when an earlier gate already blocks")


def test_check_agentic_entry_skips_when_position_already_open(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    index = _make_index()
    _seed_trade(db, "open1", "AGENTIC_AI")
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
    )
    assert result is None


def test_check_agentic_entry_skips_when_risk_gate_blocks(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    index = _make_index()
    settings = _make_settings(max_concurrent_positions=1)
    # A position already open elsewhere trips the global concurrency gate.
    _seed_trade(db, "open1", "AGENTIC_AI", index_symbol="BANKNIFTY")
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
    )
    assert result is None


def test_check_agentic_entry_skips_when_no_live_price(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": None}},
    )
    assert result is None


def test_check_agentic_entry_declines_on_model_none(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"NONE","confidence":0.3,"reasoning":"unclear"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
    )
    assert result is None


def test_check_agentic_entry_opens_a_trade_on_buy_decision(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"BUY_CE","confidence":0.75,"reasoning":"clear move"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0)
    option_finder = FakeOptionFinder(_make_contract(lot_size=75))
    smartapi = FakeSmartAPI(price=100.0)

    trade = check_agentic_entry(
        db, index, _ist_now(), settings, smartapi, option_finder, {"NIFTY": {"price": 100.0}},
    )

    assert trade is not None
    assert trade.ai_action == "BUY_CE"
    assert trade.ai_reasoning == "clear move"


def test_check_agentic_entry_handles_provider_error_without_raising(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult(None, "network error"),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
    )
    assert result is None


def test_check_agentic_entry_handles_unparseable_response_without_raising(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult("not json at all", None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
    )
    assert result is None


# ---------------------------------------------------------------------------
# check_agentic_exits
# ---------------------------------------------------------------------------


def test_check_agentic_exits_squares_off_past_cutoff_without_calling_model(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    monkeypatch.setattr(agentic_module, "utc_now", lambda: _ist_now(hh=15, mm=20).astimezone(IST))
    monkeypatch.setattr(agentic_module, "to_ist", lambda dt: _ist_now(hh=15, mm=20))

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.TIME_EXIT.value


def test_check_agentic_exits_skips_a_trade_with_no_current_premium(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    trade = StrategyTrade(
        trade_id="t1", strategy_name="Agentic AI - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=130.0,
        stoploss=85.0, entry_time=utc_now(), origin="AGENTIC_AI", status=TradeStatus.OPEN,
        result=TradeResult.OPEN, current_premium=None, sl_mode="FIXED",
    )
    db.add(trade)
    db.commit()
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


def test_check_agentic_exits_closes_on_model_exit_decision(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"EXIT","confidence":0.8,"reasoning":"thesis broke"}', None),
    )
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AI_DISCRETION_EXIT.value


def test_check_agentic_exits_leaves_trade_open_on_hold(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"HOLD","confidence":0.6,"reasoning":"still fine"}', None),
    )
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


def test_check_agentic_exits_other_origins_are_never_touched(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AUTONOMOUS_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


def test_check_agentic_exits_provider_error_leaves_trade_untouched_without_raising(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult(None, "network error"),
    )
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# run_agentic_entry_exit_checks -- the scheduler-facing entry point
# ---------------------------------------------------------------------------


def test_run_checks_skipped_without_smartapi_option_finder_or_trade_manager():
    # Must not raise even with nothing wired -- same no-op-in-this-context
    # convention app.ai.autonomous.run_autonomous_checks already uses.
    run_agentic_entry_exit_checks()


def test_run_checks_skipped_when_market_closed(monkeypatch):
    monkeypatch.setattr(agentic_module, "check_market_hours", lambda _now: "Signal received outside NSE trading hours")
    db = _make_db()
    run_agentic_entry_exit_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), None, db)
    # Nothing should have been attempted at all -- no settings row even read.
    assert db.query(StrategyTrade).count() == 0


def test_run_checks_skipped_when_not_enabled(monkeypatch):
    monkeypatch.setattr(agentic_module, "check_market_hours", lambda _now: None)
    db = _make_db()
    db.add(AgenticAISettings(id=1, enabled=False, live_model="gpt-6-luna", api_key="sk-real"))
    db.commit()
    run_agentic_entry_exit_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), None, db)
    assert db.query(StrategyTrade).count() == 0


def test_run_checks_skipped_when_live_model_or_api_key_missing(monkeypatch):
    monkeypatch.setattr(agentic_module, "check_market_hours", lambda _now: None)
    db = _make_db()
    db.add(AgenticAISettings(id=1, enabled=True, live_model="", api_key=""))
    db.commit()
    run_agentic_entry_exit_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), None, db)
    assert db.query(StrategyTrade).count() == 0
