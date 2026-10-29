from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.ai.agentic as agentic_module
from app.ai.agentic import (
    SYSTEM_PROMPT_EXIT,
    SYSTEM_PROMPT_NEWS,
    SYSTEM_PROMPT_ORCHESTRATOR,
    SYSTEM_PROMPT_TECHNICAL,
    _AgentVerdict,
    _build_exit_prompt,
    _build_news_prompt,
    _build_orchestrator_prompt,
    _build_technical_prompt,
    _call_openai_live,
    _chop_label,
    _compute_technical_features,
    _has_open_agentic_trade,
    _parse_agent_verdict,
    _parse_entry_response,
    _parse_exit_response,
    _peak_pnl_percent,
    _trend_regime_label,
    check_agentic_entry,
    check_agentic_exits,
    get_news_verdict,
    get_technical_verdict,
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


def _make_index(symbol: str = "NIFTY", enabled: bool = True, spot_token: str = "99926000") -> IndexConfig:
    return IndexConfig(
        symbol=symbol, display_name="Nifty 50", enabled=enabled, lot_size=75, strike_interval=50,
        spot_token=spot_token, spot_exchange="NSE",
    )


def _make_contract(dte_days: int = 8, lot_size: int = 75) -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=dte_days)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol=f"NIFTY{expiry}22500CE", symboltoken="1", strike=22500,
        expiry=expiry, option_type="CE", lot_size=lot_size,
    )


class FakeSmartAPI:
    def __init__(self, price: float | None = 100.0, candle_rows: list | None = None) -> None:
        self.price = price
        self.calls = 0
        self._candle_rows = candle_rows if candle_rows is not None else []

    def get_ltp(self, *_args, **_kwargs):
        self.calls += 1
        return self.price

    def get_candles(self, *_args, **_kwargs):
        return self._candle_rows


def _trending_1m_rows(n: int, start_ist: datetime, base: float = 22500.0) -> list:
    """n consecutive 1-minute SmartAPI-shaped rows with a clean, consistent
    upward drift -- enough for ADX/EMA to warm up, same shape
    tests/test_autonomous_ai.py's own _trending_1m_rows already uses for
    the identical purpose (app.market_context.build_market_context needs
    real bar history, not a bare price)."""
    rows = []
    price = base
    for i in range(n):
        price += 1.5 if i % 3 != 0 else -0.3
        ts = start_ist + timedelta(minutes=i)
        rows.append([ts.strftime("%Y-%m-%dT%H:%M:%S+05:30"), price - 1, price + 1, price - 2, price, 0])
    return rows


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


def _seed_trade(db, trade_id, origin, status=TradeStatus.OPEN, current_premium=110.0, index_symbol="NIFTY", highest_price=None):
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name=f"{origin} - Nifty 50", signal="BUY_CE",
        index_symbol=index_symbol, option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=130.0,
        stoploss=85.0, entry_time=utc_now(), origin=origin, status=status,
        result=TradeResult.OPEN if status == TradeStatus.OPEN else TradeResult.WIN,
        current_premium=current_premium, highest_price=highest_price if highest_price is not None else current_premium,
        lowest_price=100.0, pnl_percent=10.0, sl_mode="FIXED",
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def _exploding_call(*_args, **_kwargs):
    raise AssertionError("the model must not be called when an earlier gate already blocks")


def _dispatch_by_system_prompt(responses: dict) -> "callable":
    """A _call_openai_live stand-in for run_agentic_entry_exit_checks' own
    end-to-end tests, where all three agents (Technical, News, Orchestrator)
    go through the same function -- dispatches a canned response by which
    SYSTEM_PROMPT_* constant was actually passed, so one monkeypatch can
    stand in for three distinct agents in the same test."""
    def _call(settings, system_prompt, user_prompt):
        for prompt_const, content in responses.items():
            if system_prompt == prompt_const:
                return agentic_module._LiveCallResult(content, None)
        raise AssertionError(f"no canned response registered for this system prompt: {system_prompt[:60]!r}...")
    return _call


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


def test_parse_agent_verdict_valid_bullish():
    result = _parse_agent_verdict('{"lean": "BULLISH", "confidence": 0.65, "reasoning": "ADX trending, EMA bullish"}')
    assert result.lean == "BULLISH"
    assert result.confidence == 0.65
    assert result.reasoning == "ADX trending, EMA bullish"


def test_parse_agent_verdict_valid_neutral_lowercase():
    result = _parse_agent_verdict('{"lean": "neutral", "confidence": 0.2, "reasoning": "chop"}')
    assert result.lean == "NEUTRAL"


def test_parse_agent_verdict_unwraps_markdown_fence():
    text = '```json\n{"lean": "BEARISH", "confidence": 0.5, "reasoning": "x"}\n```'
    assert _parse_agent_verdict(text).lean == "BEARISH"


def test_parse_agent_verdict_rejects_unknown_lean():
    assert _parse_agent_verdict('{"lean": "BUY_CE", "confidence": 0.5, "reasoning": "x"}').lean == "ERROR"


def test_parse_agent_verdict_no_text_is_error():
    assert _parse_agent_verdict(None).lean == "ERROR"
    assert _parse_agent_verdict("").lean == "ERROR"


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
# Trend regime / chop label helpers
# ---------------------------------------------------------------------------


def test_trend_regime_label_bullish():
    assert _trend_regime_label(100.0, 95.0) == "BULLISH"


def test_trend_regime_label_bearish():
    assert _trend_regime_label(95.0, 100.0) == "BEARISH"


def test_trend_regime_label_neutral_on_exact_tie():
    assert _trend_regime_label(100.0, 100.0) == "NEUTRAL"


def test_trend_regime_label_unknown_when_missing():
    assert _trend_regime_label(None, 100.0) == "UNKNOWN"
    assert _trend_regime_label(100.0, None) == "UNKNOWN"


def test_chop_label_bands():
    assert _chop_label(None) == "UNKNOWN"
    assert _chop_label(0.1) == "CHOPPY"
    assert _chop_label(0.29) == "CHOPPY"
    assert _chop_label(0.3) == "MIXED"
    assert _chop_label(0.49) == "MIXED"
    assert _chop_label(0.5) == "CLEAN"
    assert _chop_label(0.9) == "CLEAN"


# ---------------------------------------------------------------------------
# _compute_technical_features -- this agent's own candle-fetch orchestration
# around the shared app.market_context.build_market_context().
# ---------------------------------------------------------------------------


def test_compute_technical_features_none_without_spot_token():
    db = _make_db()
    index = _make_index(spot_token="")
    result = _compute_technical_features(db, index, 22500.0, to_ist(utc_now()), FakeSmartAPI())
    assert result is None


def test_compute_technical_features_none_when_no_candle_history_at_all():
    db = _make_db()
    index = _make_index()
    smartapi = FakeSmartAPI(candle_rows=[])
    result = _compute_technical_features(db, index, 22500.0, to_ist(utc_now()), smartapi)
    assert result is None


def test_compute_technical_features_real_computation_from_a_trending_series():
    db = _make_db()
    index = _make_index()
    smartapi = FakeSmartAPI(candle_rows=_trending_1m_rows(200, datetime(2026, 10, 18, 6, 0)))
    now_ist = datetime(2026, 10, 25, 11, 0, tzinfo=IST)

    features = _compute_technical_features(db, index, 22700.0, now_ist, smartapi)

    assert features is not None
    # A clean, consistent upward drift must read as a bullish EMA regime.
    assert features.trend_regime == "BULLISH"
    assert features.ema9 is not None and features.ema21 is not None
    assert features.ema9 > features.ema21


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def test_build_technical_prompt_includes_real_readings():
    features = agentic_module._TechnicalFeatures(
        ema9=22600.0, ema21=22500.0, trend_regime="BULLISH", adx=27.3,
        chop_efficiency_ratio=0.62, chop_label="CLEAN", recent_price_change_percent=0.45,
        pdh=22750.0, pdl=22400.0, dist_to_pdh=50.0, dist_to_pdl=300.0,
    )
    prompt = _build_technical_prompt("Nifty 50", features)
    assert "BULLISH" in prompt
    assert "27.3" in prompt
    assert "CLEAN" in prompt
    assert "+0.45%" in prompt


def test_build_technical_prompt_handles_unavailable_readings():
    features = agentic_module._TechnicalFeatures(
        ema9=None, ema21=None, trend_regime="UNKNOWN", adx=None,
        chop_efficiency_ratio=None, chop_label="UNKNOWN", recent_price_change_percent=None,
        pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
    )
    prompt = _build_technical_prompt("Nifty 50", features)
    assert "unavailable" in prompt


def test_build_news_prompt_includes_headlines():
    prompt = _build_news_prompt('- "RBI holds rates steady" (ET Markets)')
    assert "RBI holds rates steady" in prompt


def test_build_orchestrator_prompt_includes_real_figures():
    figures = {"price": 22550.5, "change_abs": 45.5, "change_percent": 0.2, "day_low": 22480, "day_high": 22600}
    prompt = _build_orchestrator_prompt("Nifty 50", figures, None, None)
    assert "22550.5" in prompt
    assert "+45.50" in prompt
    assert "22480" in prompt and "22600" in prompt


def test_build_orchestrator_prompt_handles_missing_change_and_range():
    figures = {"price": 100.0, "change_abs": None, "change_percent": None, "day_low": None, "day_high": None}
    prompt = _build_orchestrator_prompt("Nifty 50", figures, None, None)
    assert "unavailable" in prompt


def test_build_orchestrator_prompt_shows_both_verdicts():
    figures = {"price": 100.0, "change_abs": 1.0, "change_percent": 1.0, "day_low": 99, "day_high": 101}
    technical = _AgentVerdict("BULLISH", 0.7, "ADX trending, EMA bullish")
    news = _AgentVerdict("NEUTRAL", None, "No recent headlines available this cycle.")
    prompt = _build_orchestrator_prompt("Nifty 50", figures, technical, news)
    assert "Technical & Flow Agent: BULLISH" in prompt
    assert "ADX trending, EMA bullish" in prompt
    assert "News & Sentiment Agent: NEUTRAL" in prompt


def test_build_orchestrator_prompt_shows_unavailable_when_verdicts_missing():
    figures = {"price": 100.0, "change_abs": 1.0, "change_percent": 1.0, "day_low": 99, "day_high": 101}
    prompt = _build_orchestrator_prompt("Nifty 50", figures, None, None)
    assert "Technical & Flow Agent: unavailable" in prompt
    assert "News & Sentiment Agent: unavailable" in prompt


def test_build_orchestrator_prompt_includes_raw_headlines_when_given():
    figures = {"price": 100.0, "change_abs": 1.0, "change_percent": 1.0, "day_low": 99, "day_high": 101}
    prompt = _build_orchestrator_prompt(
        "Nifty 50", figures, None, None, headlines_text='- "RBI holds rates steady" (ET Markets)',
    )
    assert "RBI holds rates steady" in prompt


def test_build_orchestrator_prompt_omits_headlines_section_when_empty():
    figures = {"price": 100.0, "change_abs": 1.0, "change_percent": 1.0, "day_low": 99, "day_high": 101}
    prompt = _build_orchestrator_prompt("Nifty 50", figures, None, None, headlines_text="")
    assert "Raw recent headlines" not in prompt


def test_build_exit_prompt_includes_position_numbers():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    prompt = _build_exit_prompt(trade, to_ist(utc_now()))
    assert "110.0" in prompt
    assert "10.0" in prompt


def test_build_exit_prompt_includes_headlines_when_given():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    prompt = _build_exit_prompt(trade, to_ist(utc_now()), headlines_text='- "Market dips on profit booking" (Moneycontrol)')
    assert "Market dips on profit booking" in prompt


def test_build_exit_prompt_omits_headlines_section_when_empty():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    prompt = _build_exit_prompt(trade, to_ist(utc_now()))
    assert "Raw recent headlines" not in prompt


def test_build_exit_prompt_shows_both_verdicts_when_given():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    technical = _AgentVerdict("BEARISH", 0.6, "chop clearing, EMA bearish")
    news = _AgentVerdict("BULLISH", 0.4, "mild positive headline")
    prompt = _build_exit_prompt(trade, to_ist(utc_now()), technical_verdict=technical, news_verdict=news)
    assert "Technical & Flow Agent: BEARISH" in prompt
    assert "News & Sentiment Agent: BULLISH" in prompt


def test_build_exit_prompt_shows_unavailable_when_verdicts_missing():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    prompt = _build_exit_prompt(trade, to_ist(utc_now()))
    assert "Technical & Flow Agent: unavailable" in prompt
    assert "News & Sentiment Agent: unavailable" in prompt


# ---------------------------------------------------------------------------
# _peak_pnl_percent -- 28 Oct 2026, a real position ran to +19% and was still
# open at +5% with the exit agent never having seen the peak at all.
# ---------------------------------------------------------------------------


def test_peak_pnl_percent_reflects_the_real_running_high():
    db = _make_db()
    # The real reported shape: entry 100, ran up to 119 (peak +19%), has
    # since fallen back to 105 (current +5%) -- peak and current must read
    # as two different numbers, not collapse to one.
    trade = _seed_trade(db, "t1", "AGENTIC_AI", current_premium=105.0, highest_price=119.0)
    assert _peak_pnl_percent(trade) == 19.0
    assert trade.pnl_percent == 10.0  # the fixture's own stored running P&L, untouched by this helper


def test_peak_pnl_percent_falls_back_to_running_pnl_without_a_stored_high():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI", current_premium=105.0)
    trade.highest_price = None
    db.commit()
    assert _peak_pnl_percent(trade) == trade.pnl_percent


def test_peak_pnl_percent_falls_back_without_a_real_entry_price():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade.entry_price = 0.0
    db.commit()
    assert _peak_pnl_percent(trade) == trade.pnl_percent


def test_build_exit_prompt_shows_peak_distinct_from_current_pnl():
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI", current_premium=105.0, highest_price=119.0)
    prompt = _build_exit_prompt(trade, to_ist(utc_now()))
    assert "Peak P&L (highest point this position has reached): 19.0%" in prompt
    assert "Running P&L: 10.0%" in prompt


def test_orchestrator_exit_prompt_weighs_peak_against_current_in_the_system_prompt():
    # Pins the new guidance's presence -- a soft, model-judgment framing,
    # never a hard formula (per explicit instruction: the model decides).
    assert "PEAK P&L" in SYSTEM_PROMPT_EXIT
    assert "given back a meaningful share of a real peak gain" in SYSTEM_PROMPT_EXIT
    assert "HOLD remains correct" in SYSTEM_PROMPT_EXIT


def test_exit_prompt_closes_the_restate_the_thesis_loophole():
    # 28 Oct 2026, same day, second real-evidence pass: a real position ran
    # to +43.42% peak MFE and the model's exit judgment -- re-asked ~20+
    # times, already shown its own peak every time -- held through nearly
    # all of it anyway. The original peak-P&L wording left "the Technical
    # read still supports the thesis" as an unconditional escape hatch;
    # this pins that the prompt now requires something CURRENT and
    # CONCRETE, not a restatement of why the trade was entered, once the
    # giveback is large.
    assert "not by itself a resolution" in SYSTEM_PROMPT_EXIT
    assert "repeating it is not new information" in SYSTEM_PROMPT_EXIT
    assert "lean toward EXIT instead" in SYSTEM_PROMPT_EXIT
    # The shallow-pullback carve-out must survive -- this tightens the large-
    # giveback case only, it doesn't turn every small wobble into a forced
    # justification.
    assert "needs none of\nthis scrutiny" in SYSTEM_PROMPT_EXIT or "needs none of this scrutiny" in SYSTEM_PROMPT_EXIT


# ---------------------------------------------------------------------------
# get_technical_verdict
# ---------------------------------------------------------------------------


def _make_features(**overrides) -> "agentic_module._TechnicalFeatures":
    base = dict(
        ema9=22600.0, ema21=22500.0, trend_regime="BULLISH", adx=27.0,
        chop_efficiency_ratio=0.55, chop_label="CLEAN", recent_price_change_percent=0.3,
        pdh=22750.0, pdl=22400.0, dist_to_pdh=50.0, dist_to_pdl=300.0,
    )
    base.update(overrides)
    return agentic_module._TechnicalFeatures(**base)


def test_get_technical_verdict_returns_real_lean(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda settings, system_prompt, user_prompt: agentic_module._LiveCallResult(
            '{"lean":"BULLISH","confidence":0.7,"reasoning":"clean uptrend"}', None,
        ),
    )
    verdict = get_technical_verdict(_make_settings(), "Nifty 50", _make_features())
    assert verdict.lean == "BULLISH"
    assert verdict.reasoning == "clean uptrend"


def test_get_technical_verdict_error_on_provider_failure(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult(None, "network error"),
    )
    verdict = get_technical_verdict(_make_settings(), "Nifty 50", _make_features())
    assert verdict.lean == "ERROR"


def test_get_technical_verdict_uses_the_technical_system_prompt(monkeypatch):
    captured = {}

    def _capture(settings, system_prompt, user_prompt):
        captured["system_prompt"] = system_prompt
        return agentic_module._LiveCallResult('{"lean":"NEUTRAL","confidence":0.2,"reasoning":"mixed"}', None)

    monkeypatch.setattr(agentic_module, "_call_openai_live", _capture)
    get_technical_verdict(_make_settings(), "Nifty 50", _make_features())
    assert captured["system_prompt"] == SYSTEM_PROMPT_TECHNICAL


# ---------------------------------------------------------------------------
# get_news_verdict
# ---------------------------------------------------------------------------


def test_get_news_verdict_neutral_without_calling_model_when_no_headlines():
    monkeypatch_target = agentic_module._call_openai_live
    try:
        agentic_module._call_openai_live = _exploding_call
        verdict = get_news_verdict(_make_settings(), "")
        assert verdict.lean == "NEUTRAL"
    finally:
        agentic_module._call_openai_live = monkeypatch_target


def test_get_news_verdict_returns_real_lean(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"lean":"BEARISH","confidence":0.6,"reasoning":"rate hike fears"}', None),
    )
    verdict = get_news_verdict(_make_settings(), '- "RBI signals rate hike" (ET Markets)')
    assert verdict.lean == "BEARISH"
    assert verdict.reasoning == "rate hike fears"


def test_get_news_verdict_degrades_to_neutral_on_provider_failure(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult(None, "network error"),
    )
    verdict = get_news_verdict(_make_settings(), '- "some headline" (Feed)')
    assert verdict.lean == "NEUTRAL"  # degrades, does NOT return ERROR -- news is supplementary


def test_get_news_verdict_degrades_to_neutral_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult("not json at all", None),
    )
    verdict = get_news_verdict(_make_settings(), '- "some headline" (Feed)')
    assert verdict.lean == "NEUTRAL"


def test_get_news_verdict_uses_the_news_system_prompt(monkeypatch):
    captured = {}

    def _capture(settings, system_prompt, user_prompt):
        captured["system_prompt"] = system_prompt
        return agentic_module._LiveCallResult('{"lean":"NEUTRAL","confidence":0.2,"reasoning":"routine"}', None)

    monkeypatch.setattr(agentic_module, "_call_openai_live", _capture)
    get_news_verdict(_make_settings(), '- "some headline" (Feed)')
    assert captured["system_prompt"] == SYSTEM_PROMPT_NEWS


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
    assert trade.ai_confidence is None  # no confidence passed -- defaults to None, never fabricated


# ---------------------------------------------------------------------------
# ai_confidence persistence -- 29 Oct 2026. The Orchestrator already
# computes a confidence score on every decision; it was never stored on
# the trade or read anywhere, so a real same-day incident ("why did it
# take this trade") could only ever be answered from reasoning text, never
# a number. This fixes the storage gap only -- no gate, no behavior
# change, confirmed by test_open_agentic_trade_opens_with_correct_fields
# above still passing unmodified with confidence simply omitted.
# ---------------------------------------------------------------------------


def test_open_agentic_trade_persists_the_passed_confidence(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    )
    contract = _make_contract(lot_size=75)
    option_finder = FakeOptionFinder(contract)
    smartapi = FakeSmartAPI(price=100.0)

    trade = open_agentic_trade(
        db, index, "BUY_CE", "clean setup", settings, smartapi, option_finder, confidence=0.82,
    )

    assert trade.ai_confidence == 0.82


def test_check_agentic_entry_threads_the_orchestrator_confidence_into_the_trade(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    settings = _make_settings(account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0)
    option_finder = FakeOptionFinder(_make_contract(lot_size=75))
    smartapi = FakeSmartAPI(price=100.0)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult(
            '{"decision": "BUY_CE", "confidence": 0.74, "reasoning": "clean"}', None,
        ),
    )

    trade = check_agentic_entry(
        db, index, _ist_now(), settings, smartapi, option_finder, {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "bullish"),
        news_verdict=_AgentVerdict("NEUTRAL", None, ""),
    )

    assert trade is not None
    assert trade.ai_confidence == 0.74


def test_open_agentic_trade_forces_minimum_lot_when_risk_budget_too_small(monkeypatch):
    # 27 Oct 2026, explicit instruction ("whatever it takes... paper trades
    # only"): a 0-lot risk calculation no longer declines the trade -- it
    # opens at the forced _MIN_LOTS_PAPER_FLOOR instead, flagged explicitly.
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

    assert trade is not None
    assert trade.quantity == 75  # 1 lot, the forced floor
    assert trade.mode == "PAPER"


def test_open_agentic_trade_logs_forced_minimum_lot_flag(monkeypatch):
    from app.db_models import LogEvent

    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_risk_percent_per_trade=1.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    )
    contract = _make_contract(lot_size=75)
    option_finder = FakeOptionFinder(contract)
    smartapi = FakeSmartAPI(price=100.0)

    open_agentic_trade(db, index, "BUY_CE", "clean setup", settings, smartapi, option_finder)

    opened_event = db.query(LogEvent).filter(LogEvent.message.like("%opened%")).one()
    assert '"forced_minimum_lot": true' in opened_event.payload


def test_open_agentic_trade_normal_sizing_is_never_flagged_as_forced(monkeypatch):
    from app.db_models import LogEvent

    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    db = _make_db()
    index = _make_index()
    # Real, non-zero sizing (10% risk budget) must NOT be flagged as forced.
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    )
    contract = _make_contract(lot_size=75)
    option_finder = FakeOptionFinder(contract)
    smartapi = FakeSmartAPI(price=100.0)

    trade = open_agentic_trade(db, index, "BUY_CE", "clean setup", settings, smartapi, option_finder)

    assert trade is not None
    assert trade.quantity == 600  # the real, unforced 8-lot sizing
    opened_event = db.query(LogEvent).filter(LogEvent.message.like("%opened%")).one()
    assert '"forced_minimum_lot": false' in opened_event.payload


def test_compute_position_size_itself_still_floors_to_zero(monkeypatch):
    # The paper-only floor lives only in open_agentic_trade -- the shared
    # compute_position_size() function (also used by the dashboard's own
    # what-if calculator) must stay an honest answer to "could this really
    # size at all", unaffected by this strategy-specific override.
    from app.ai.agentic import compute_position_size

    assert compute_position_size(100_000.0, 1.0, 100.0, 85.0, 75) == 0


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
# check_agentic_entry -- gating order, now also including the Technical
# Agent verdict gate, and the Orchestrator being the final call.
# ---------------------------------------------------------------------------


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


def test_check_agentic_entry_blocked_by_news_kill_switch(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
        is_severe=True,
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


def test_check_agentic_entry_skips_when_no_technical_verdict(monkeypatch):
    # Even with every other gate clear, no real Technical Agent verdict
    # for this cycle must fail closed -- the Orchestrator is never called
    # without this pipeline's primary, required signal.
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
        technical_verdict=None,
    )
    assert result is None


def test_check_agentic_entry_skips_when_technical_verdict_is_error(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("ERROR", None, "provider failed"),
    )
    assert result is None


def test_check_agentic_entry_reaches_orchestrator_with_a_real_technical_verdict(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"NONE","confidence":0.3,"reasoning":"unclear"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.6, "clean uptrend"),
    )
    assert result is None  # declined on NONE, not on an earlier gate -- the orchestrator WAS reached


def test_check_agentic_entry_declines_on_orchestrator_none(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"NONE","confidence":0.3,"reasoning":"unclear"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    result = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.6, "clean uptrend"),
    )
    assert result is None


def test_check_agentic_entry_opens_a_trade_on_orchestrator_buy_decision(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"BUY_CE","confidence":0.75,"reasoning":"both agents agree"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0)
    option_finder = FakeOptionFinder(_make_contract(lot_size=75))
    smartapi = FakeSmartAPI(price=100.0)

    trade = check_agentic_entry(
        db, index, _ist_now(), settings, smartapi, option_finder, {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "clean uptrend"),
        news_verdict=_AgentVerdict("BULLISH", 0.4, "mildly supportive headline"),
    )

    assert trade is not None
    assert trade.ai_action == "BUY_CE"
    assert trade.ai_reasoning == "both agents agree"


def test_check_agentic_entry_orchestrator_receives_both_verdicts(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    captured = {}

    def _capture(settings, system_prompt, user_prompt):
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return agentic_module._LiveCallResult('{"decision":"NONE","confidence":0.3,"reasoning":"x"}', None)

    monkeypatch.setattr(agentic_module, "_call_openai_live", _capture)
    db = _make_db()
    index = _make_index()
    settings = _make_settings()
    check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BEARISH", 0.5, "EMA bearish, ADX trending down"),
        news_verdict=_AgentVerdict("BEARISH", 0.3, "weak PMI print"),
    )
    assert captured["system_prompt"] == SYSTEM_PROMPT_ORCHESTRATOR
    assert "EMA bearish, ADX trending down" in captured["user_prompt"]
    assert "weak PMI print" in captured["user_prompt"]


def test_orchestrator_system_prompt_treats_news_as_an_indicator_not_a_gate():
    # 26 Oct 2026: the first version made any News disagreement a near-veto
    # over even a clear Technical case -- fixed so News is corroborating
    # weight read against its own confidence, never an automatic override.
    assert "Technical & Flow Agent's read as the primary driver" in SYSTEM_PROMPT_ORCHESTRATOR
    assert "not a second gate the trade must also clear" in SYSTEM_PROMPT_ORCHESTRATOR
    assert "A NEUTRAL news read is not disagreement" in SYSTEM_PROMPT_ORCHESTRATOR
    assert "do not let a single weak" in SYSTEM_PROMPT_ORCHESTRATOR
    assert "override a clear technical case on disagreement alone" in SYSTEM_PROMPT_ORCHESTRATOR
    # The old near-veto phrasing must actually be gone, not just supplemented.
    assert "prefer NONE unless one agent's case is exceptionally clear" not in SYSTEM_PROMPT_ORCHESTRATOR


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
        technical_verdict=_AgentVerdict("BULLISH", 0.6, "clean uptrend"),
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
        technical_verdict=_AgentVerdict("BULLISH", 0.6, "clean uptrend"),
    )
    assert result is None


# ---------------------------------------------------------------------------
# check_agentic_exits
# ---------------------------------------------------------------------------


def _freeze_clock(monkeypatch, hh: int, mm: int) -> None:
    """check_agentic_exits computes now_ist from the REAL wall clock
    (to_ist(utc_now())), and every test below passes a fixed (15, 15)
    cutoff -- without freezing the clock, a test written mid-morning starts
    failing on its own the moment real IST time crosses 15:15, the exact
    class of flake CLAUDE.md documents repeatedly elsewhere in this
    project. Every test that isn't deliberately exercising the past-cutoff
    branch itself must call this with a safe pre-cutoff time."""
    monkeypatch.setattr(agentic_module, "utc_now", lambda: _ist_now(hh=hh, mm=mm).astimezone(IST))
    monkeypatch.setattr(agentic_module, "to_ist", lambda dt: _ist_now(hh=hh, mm=mm))


def test_check_agentic_exits_squares_off_past_cutoff_without_calling_model(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    _freeze_clock(monkeypatch, 15, 20)

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.TIME_EXIT.value


def test_check_agentic_exits_skips_a_trade_with_no_current_premium(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    _freeze_clock(monkeypatch, 11, 0)
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
    _freeze_clock(monkeypatch, 11, 0)
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
    _freeze_clock(monkeypatch, 11, 0)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


def test_check_agentic_exits_other_origins_are_never_touched(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    _freeze_clock(monkeypatch, 11, 0)
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
    _freeze_clock(monkeypatch, 11, 0)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15))

    db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


def test_check_agentic_exits_news_kill_switch_closes_every_open_position(monkeypatch):
    monkeypatch.setattr(agentic_module, "_call_openai_live", _exploding_call)
    _freeze_clock(monkeypatch, 11, 0)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15), is_severe=True, severe_matches=["War declared"])

    db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.NEWS_KILL_SWITCH.value


def test_check_agentic_exits_no_kill_switch_when_not_severe(monkeypatch):
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"HOLD","confidence":0.6,"reasoning":"fine"}', None),
    )
    _freeze_clock(monkeypatch, 11, 0)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15), is_severe=False)

    db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


def test_check_agentic_exits_uses_the_matching_technical_verdict_for_each_trade(monkeypatch):
    captured = {}

    def _capture(settings, system_prompt, user_prompt):
        captured["user_prompt"] = user_prompt
        return agentic_module._LiveCallResult('{"decision":"HOLD","confidence":0.6,"reasoning":"fine"}', None)

    monkeypatch.setattr(agentic_module, "_call_openai_live", _capture)
    _freeze_clock(monkeypatch, 11, 0)
    db = _make_db()
    _seed_trade(db, "t1", "AGENTIC_AI", index_symbol="NIFTY")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(
        db, trade_manager, settings, (15, 15),
        technical_verdicts_by_index={"NIFTY": _AgentVerdict("BEARISH", 0.5, "EMA regime turned bearish")},
        news_verdict=_AgentVerdict("NEUTRAL", None, "no real signal"),
    )

    assert "EMA regime turned bearish" in captured["user_prompt"]


def test_check_agentic_exits_missing_technical_verdict_does_not_block_the_call(monkeypatch):
    # A thin/missing technical read for this trade's index must not stop
    # the exit call itself -- the backstop stop/target already protect it.
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"HOLD","confidence":0.6,"reasoning":"fine"}', None),
    )
    _freeze_clock(monkeypatch, 11, 0)
    db = _make_db()
    trade = _seed_trade(db, "t1", "AGENTIC_AI")
    trade_manager = _make_trade_manager()
    settings = _make_settings()

    check_agentic_exits(db, trade_manager, settings, (15, 15), technical_verdicts_by_index={})

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


def test_run_checks_news_kill_switch_closes_open_positions_end_to_end(monkeypatch):
    from app.ai.news import Headline

    monkeypatch.setattr(agentic_module, "check_market_hours", lambda _now: None)
    monkeypatch.setattr(
        agentic_module, "fetch_market_headlines",
        lambda: [Headline("War declared, markets panic", "Test Feed", None)],
    )
    db = _make_db()
    db.add(AgenticAISettings(id=1, enabled=True, live_model="gpt-6-luna", api_key="sk-real"))
    trade = _seed_trade(db, "open1", "AGENTIC_AI")
    db.commit()

    run_agentic_entry_exit_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), None, db)

    db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.NEWS_KILL_SWITCH.value


def test_run_checks_severe_news_skips_computing_technical_and_news_verdicts(monkeypatch):
    # A severe-event cycle force-closes everything and blocks every new
    # entry regardless -- neither verdict would ever be read, so neither
    # should be computed (an extra, wasted LLM/candle-fetch cost otherwise).
    from app.ai.news import Headline

    monkeypatch.setattr(agentic_module, "check_market_hours", lambda _now: None)
    monkeypatch.setattr(
        agentic_module, "fetch_market_headlines",
        lambda: [Headline("War declared, markets panic", "Test Feed", None)],
    )
    monkeypatch.setattr(agentic_module, "get_news_verdict", _exploding_call)
    monkeypatch.setattr(agentic_module, "_compute_technical_features", _exploding_call)
    db = _make_db()
    db.add(AgenticAISettings(id=1, enabled=True, live_model="gpt-6-luna", api_key="sk-real"))
    db.commit()

    run_agentic_entry_exit_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), None, db)
    # No AssertionError raised above means neither exploding stand-in fired.


def test_run_checks_opens_a_trade_through_the_full_three_agent_pipeline(monkeypatch):
    """End to end: a real Technical Agent call (fed from a genuinely
    trending synthetic candle series), a real News Agent call, and a real
    Orchestrator call that combines both into an opened trade -- the
    direct proof the rebuilt multi-agent wiring actually reaches
    open_agentic_trade, not just that each piece works in isolation."""
    monkeypatch.setattr(agentic_module, "check_market_hours", lambda _now: None)
    _freeze_clock(monkeypatch, 11, 0)
    monkeypatch.setattr(agentic_module, "fetch_market_headlines", lambda: [])
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "get_index_live_figures",
        lambda db, smartapi, feed_store: [
            {"symbol": "NIFTY", "price": 22700.0, "change_abs": 50.0, "change_percent": 0.22, "day_low": 22600, "day_high": 22750},
        ],
    )
    monkeypatch.setattr(
        agentic_module, "list_index_configs",
        lambda db: [_make_index()],
    )
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        _dispatch_by_system_prompt({
            SYSTEM_PROMPT_TECHNICAL: '{"lean":"BULLISH","confidence":0.7,"reasoning":"clean uptrend, ADX trending"}',
            SYSTEM_PROMPT_ORCHESTRATOR: '{"decision":"BUY_CE","confidence":0.65,"reasoning":"technical agent clearly bullish, no conflicting news"}',
        }),
    )

    db = _make_db()
    db.add(AgenticAISettings(
        id=1, enabled=True, live_model="gpt-6-luna", live_reasoning_effort="medium", api_key="sk-real",
        account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0,
        max_daily_loss_percent=3.0, max_concurrent_positions=1, max_capital_deployed_percent=20.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0,
    ))
    db.commit()

    smartapi = FakeSmartAPI(price=100.0, candle_rows=_trending_1m_rows(200, datetime(2026, 10, 18, 6, 0)))
    option_finder = FakeOptionFinder(_make_contract(lot_size=75))
    run_agentic_entry_exit_checks(smartapi, option_finder, _make_trade_manager(), None, db)

    trade = db.query(StrategyTrade).filter_by(origin="AGENTIC_AI").one_or_none()
    assert trade is not None
    assert trade.ai_action == "BUY_CE"
    assert trade.signal == "BUY_CE"
