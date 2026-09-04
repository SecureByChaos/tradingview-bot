from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import OptionContract, Signal
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST, to_ist, utc_now
from app.ai.autonomous import (
    _ADX_HARD_FLOOR,
    _BACKSTOP_STOP_PERCENT_NOMINAL,
    _BACKSTOP_TARGET_PERCENT_NOMINAL,
    _BREAKEVEN_VIOLATION_ACTIVATE_PERCENT,
    _BREAKEVEN_VIOLATION_FLOOR_PERCENT,
    _PEAK_GIVEBACK_ACTIVATE_PERCENT,
    _PEAK_GIVEBACK_WIDTH_PERCENT,
    _SESSION_CLOSE_WARNING_MINUTES,
    _STALL_WINDOW_MINUTES,
    ORIGIN,
    _Features,
    _build_entry_prompt,
    _build_exit_prompt,
    _compute_futures_vwap,
    _has_open_autonomous_trade,
    _parse_entry_response,
    _parse_exit_response,
    _peak_pnl_percent,
    _regime_matches_action,
    _session_phase,
    _structural_invalidation,
    _trend_regime,
    _vwap_relation,
    check_autonomous_entry,
    check_autonomous_exits,
    open_autonomous_trade,
    run_autonomous_checks,
)
from app.market_data import ONE_MINUTE, load_bars, store_bars


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(
        symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True,
        exchange_segment="NFO", instrument_name="BANKNIFTY",
        spot_exchange="NSE", spot_symbol="NIFTY BANK", spot_token="99926009",
    )


def _make_features(
    *, adx: float | None = 25.0, session_phase: str = "MORNING_MOMENTUM",
    spot: float = 57000.0, vwap: float | None = 56900.0, fast_ema: float | None = 57000.0,
    slow_ema: float | None = 56800.0, minutes_to_close: int = 120,
) -> _Features:
    return _Features(
        spot=spot, vwap=vwap, vwap_relation=_vwap_relation(spot, vwap),
        fast_ema=fast_ema, slow_ema=slow_ema, trend_regime=_trend_regime(fast_ema, slow_ema),
        adx=adx, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        session_phase=session_phase, minutes_to_close=minutes_to_close,
    )


def _add_trade(db, *, trade_id, index_symbol="BANKNIFTY", origin=ORIGIN, status=TradeStatus.OPEN,
                current_premium=100.0, entry_price=100.0, stoploss=65.0, target=150.0,
                option_type="CE", highest_price=None, pnl_percent=None) -> None:
    if pnl_percent is None and entry_price:
        pnl_percent = round((current_premium - entry_price) / entry_price * 100, 2)
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Autonomous AI - Bank Nifty", signal=f"BUY_{option_type}",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type=option_type, quantity=35,
        entry_price=entry_price, current_premium=current_premium, stoploss=stoploss, target=target,
        entry_time=utc_now(), origin=origin, status=status, pnl_percent=pnl_percent,
        result=TradeResult.OPEN if status == TradeStatus.OPEN else TradeResult.WIN,
        mode=TradingMode.PAPER, highest_price=highest_price,
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

    def get_candles(self, *_args, **_kwargs) -> list:
        # No rows -- forces callers back onto whatever's already stored,
        # which is empty by default in these tests (build_market_context
        # then correctly returns None). Individual tests override this
        # attribute when they need a real feature set.
        return []

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("Autonomous AI must never place a real order")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract | None, futures: dict | None = None) -> None:
        self.contract = contract
        self.futures = futures
        self.calls = 0

    def find_atm_contract(self, signal: Signal, index: IndexConfig, offset: int, min_dte: int | None = None) -> OptionContract:
        self.calls += 1
        if self.contract is None:
            raise ValueError("no contract available")
        return self.contract

    def find_current_futures_contract(self, index: IndexConfig) -> dict | None:
        return self.futures


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
    d = _parse_exit_response('{"decision": "EXIT", "confidence": 0.9, "exit_reason": "PEAK_GIVEBACK_GUARD", "reasoning": "target-ish gain"}')
    assert d.action == "EXIT"
    assert d.exit_rule == "PEAK_GIVEBACK_GUARD"


def test_parse_exit_response_valid_hold():
    d = _parse_exit_response('{"decision": "HOLD", "reasoning": "still developing"}')
    assert d.action == "HOLD"
    assert d.exit_rule is None


def test_parse_exit_response_rejects_unknown_decision():
    d = _parse_exit_response('{"decision": "SELL"}')
    assert d.action == "ERROR"


# ---------------------------------------------------------------------------
# _session_phase
# ---------------------------------------------------------------------------

def test_session_phase_boundaries():
    def phase_at(h, m):
        return _session_phase(datetime(2026, 8, 31, h, m, tzinfo=IST))

    assert phase_at(9, 15) == "OPENING_VOLATILITY"
    assert phase_at(9, 29) == "OPENING_VOLATILITY"
    assert phase_at(9, 30) == "MORNING_MOMENTUM"
    assert phase_at(11, 14) == "MORNING_MOMENTUM"
    assert phase_at(11, 15) == "CHOP_ZONE"
    assert phase_at(13, 29) == "CHOP_ZONE"
    assert phase_at(13, 30) == "AFTERNOON_TREND"
    assert phase_at(14, 59) == "AFTERNOON_TREND"
    assert phase_at(15, 0) == "SQUARE_OFF_ZONE"
    assert phase_at(15, 30) == "SQUARE_OFF_ZONE"


# ---------------------------------------------------------------------------
# _vwap_relation / _trend_regime / _structural_invalidation / _peak_pnl_percent
# ---------------------------------------------------------------------------

def test_vwap_relation_above_below_and_chop_band():
    assert _vwap_relation(57100.0, 57000.0) == "ABOVE_VWAP"
    assert _vwap_relation(56900.0, 57000.0) == "BELOW_VWAP"
    # Within 0.1% of 57000 (i.e. within ~57 points) counts as AT_VWAP.
    assert _vwap_relation(57030.0, 57000.0) == "AT_VWAP"
    assert _vwap_relation(57000.0, None) == "UNKNOWN"


def test_trend_regime_bullish_bearish_neutral_unknown():
    assert _trend_regime(100.0, 90.0) == "BULLISH"
    assert _trend_regime(90.0, 100.0) == "BEARISH"
    assert _trend_regime(100.0, 100.0) == "NEUTRAL"
    assert _trend_regime(None, 100.0) == "UNKNOWN"


def test_structural_invalidation_ce_contradicted_by_below_vwap():
    trade = StrategyTrade(option_type="CE")
    features = _Features(
        spot=100, vwap=200, vwap_relation="BELOW_VWAP", fast_ema=None, slow_ema=None,
        trend_regime="UNKNOWN", adx=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        session_phase="MORNING_MOMENTUM", minutes_to_close=100,
    )
    assert _structural_invalidation(trade, features) is True


def test_structural_invalidation_pe_contradicted_by_above_vwap():
    trade = StrategyTrade(option_type="PE")
    features = _Features(
        spot=200, vwap=100, vwap_relation="ABOVE_VWAP", fast_ema=None, slow_ema=None,
        trend_regime="UNKNOWN", adx=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        session_phase="MORNING_MOMENTUM", minutes_to_close=100,
    )
    assert _structural_invalidation(trade, features) is True


def test_structural_invalidation_not_triggered_when_aligned():
    trade = StrategyTrade(option_type="CE")
    features = _Features(
        spot=200, vwap=100, vwap_relation="ABOVE_VWAP", fast_ema=None, slow_ema=None,
        trend_regime="UNKNOWN", adx=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        session_phase="MORNING_MOMENTUM", minutes_to_close=100,
    )
    assert _structural_invalidation(trade, features) is False


def test_structural_invalidation_never_fires_when_vwap_unknown():
    trade = StrategyTrade(option_type="CE")
    features = _Features(
        spot=200, vwap=None, vwap_relation="UNKNOWN", fast_ema=None, slow_ema=None,
        trend_regime="UNKNOWN", adx=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        session_phase="MORNING_MOMENTUM", minutes_to_close=100,
    )
    assert _structural_invalidation(trade, features) is False


def test_peak_pnl_percent_from_highest_price():
    trade = StrategyTrade(entry_price=100.0, highest_price=120.0, pnl_percent=5.0)
    assert _peak_pnl_percent(trade) == 20.0


def test_peak_pnl_percent_falls_back_to_pnl_percent_without_highest_price():
    trade = StrategyTrade(entry_price=100.0, highest_price=None, pnl_percent=5.0)
    assert _peak_pnl_percent(trade) == 5.0


# ---------------------------------------------------------------------------
# _compute_futures_vwap
# ---------------------------------------------------------------------------

def _futures_row(ts: datetime, price: float, volume: float) -> list:
    return [ts.strftime("%Y-%m-%dT%H:%M:%S+05:30"), price, price + 1, price - 1, price, volume]


def test_compute_futures_vwap_from_real_volume():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 8, 31, 10, 0, tzinfo=IST)
    start = datetime(2026, 8, 31, 9, 15)
    rows = [
        _futures_row(start, 100.0, 10.0),
        _futures_row(start + timedelta(minutes=1), 200.0, 30.0),
    ]

    class _FuturesSmartAPI(FakeSmartAPI):
        def get_candles(self, *_args, **_kwargs):
            return rows

    option_finder = FakeOptionFinder(_make_contract(), futures={
        "exchange": "NFO", "tradingsymbol": "BANKNIFTY28AUG26FUT", "symboltoken": "999", "expiry": "28AUG2026",
    })
    vwap = _compute_futures_vwap(db, index, option_finder, _FuturesSmartAPI(), now_ist)
    # typical price == close for these single-price bars: (100*10 + 200*30) / 40 = 175
    assert vwap == 175.0


def test_compute_futures_vwap_none_when_no_futures_contract():
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract(), futures=None)
    vwap = _compute_futures_vwap(db, index, option_finder, FakeSmartAPI(), datetime(2026, 8, 31, 10, 0, tzinfo=IST))
    assert vwap is None


def test_compute_futures_vwap_none_when_no_volume_yet():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 8, 31, 9, 16, tzinfo=IST)
    store_bars(db, "BANKNIFTY_FUT", ONE_MINUTE, [])
    option_finder = FakeOptionFinder(_make_contract(), futures={
        "exchange": "NFO", "tradingsymbol": "X", "symboltoken": "999", "expiry": "28AUG2026",
    })
    vwap = _compute_futures_vwap(db, index, option_finder, FakeSmartAPI(), now_ist)
    assert vwap is None


def test_compute_futures_vwap_does_not_pollute_real_index_candle_history():
    db = _make_session()
    index = _make_index()
    now_ist = datetime(2026, 8, 31, 10, 0, tzinfo=IST)
    rows = [_futures_row(datetime(2026, 8, 31, 9, 15), 100.0, 10.0)]

    class _FuturesSmartAPI(FakeSmartAPI):
        def get_candles(self, *_args, **_kwargs):
            return rows

    option_finder = FakeOptionFinder(_make_contract(), futures={
        "exchange": "NFO", "tradingsymbol": "X", "symboltoken": "999", "expiry": "28AUG2026",
    })
    _compute_futures_vwap(db, index, option_finder, _FuturesSmartAPI(), now_ist)
    assert load_bars(db, "BANKNIFTY", ONE_MINUTE) == []
    assert len(load_bars(db, "BANKNIFTY_FUT", ONE_MINUTE)) == 1


# ---------------------------------------------------------------------------
# _build_entry_prompt / _build_exit_prompt
# ---------------------------------------------------------------------------

def test_build_entry_prompt_mentions_vwap_adx_ema_and_session_phase():
    features = _make_features()
    prompt = _build_entry_prompt(features, "Bank Nifty")
    for expected in ("VWAP", "ADX", "EMA", "Session Phase", "MORNING_MOMENTUM", "57000.00"):
        assert expected in prompt


def test_build_entry_prompt_handles_missing_vwap_and_adx():
    features = _make_features(adx=None, vwap=None, fast_ema=None, slow_ema=None)
    prompt = _build_entry_prompt(features, "Bank Nifty")
    assert "unavailable" in prompt


def test_build_exit_prompt_includes_risk_boundaries_and_vwap_status():
    db = _make_session()
    trade = StrategyTrade(
        trade_id="t1", strategy_name="x", signal="BUY_CE", index_symbol="BANKNIFTY",
        tradingsymbol="X", symboltoken="1", strike=57000, expiry="28AUG2026", option_type="CE",
        quantity=35, entry_price=100.0, current_premium=110.0, stoploss=65.0, target=150.0,
        entry_time=utc_now(), origin=ORIGIN, status=TradeStatus.OPEN, pnl_percent=10.0,
        highest_price=110.0, mode=TradingMode.PAPER,
    )
    features = _make_features(vwap=100.0, spot=110.0)
    prompt = _build_exit_prompt(trade, features, to_ist(utc_now()))
    assert "Defined Risk Boundaries" in prompt
    assert "Underlying Spot vs VWAP" in prompt
    assert features.vwap_relation in prompt


def test_build_exit_prompt_handles_missing_features():
    trade = StrategyTrade(
        trade_id="t1", strategy_name="x", signal="BUY_CE", index_symbol="BANKNIFTY",
        tradingsymbol="X", symboltoken="1", strike=57000, expiry="28AUG2026", option_type="CE",
        quantity=35, entry_price=100.0, current_premium=110.0, stoploss=65.0, target=150.0,
        entry_time=utc_now(), origin=ORIGIN, status=TradeStatus.OPEN, pnl_percent=10.0,
        mode=TradingMode.PAPER,
    )
    prompt = _build_exit_prompt(trade, None, to_ist(utc_now()))
    assert "UNKNOWN" in prompt


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

def test_open_autonomous_trade_opens_a_paper_fixed_trade_with_tightened_backstop_stop_target():
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
    # a no-op, so the tightened 15%/30% nominal backstop applies directly
    # (was 35%/50% before the 3 Sep "without judgement" rebuild).
    assert _BACKSTOP_STOP_PERCENT_NOMINAL == 15.0
    assert _BACKSTOP_TARGET_PERCENT_NOMINAL == 30.0
    assert trade.stoploss == round(100.0 * (1 - 0.15), 2)
    assert trade.target == round(100.0 * (1 + 0.30), 2)
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
# check_autonomous_entry -- deterministic hard gates
# ---------------------------------------------------------------------------

def test_check_entry_skips_when_position_already_open(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    _add_trade(db, trade_id="t1")
    option_finder = FakeOptionFinder(_make_contract())

    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model")))

    result = check_autonomous_entry(db, index, _make_features(), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder)
    assert result is None
    assert option_finder.calls == 0


def test_check_entry_skips_when_no_features(monkeypatch):
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model")))

    result = check_autonomous_entry(db, index, None, to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder)
    assert result is None


def test_check_entry_deterministic_block_on_chop_zone(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM call during CHOP_ZONE")))

    result = check_autonomous_entry(
        db, index, _make_features(session_phase="CHOP_ZONE"), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )
    assert result is None
    assert option_finder.calls == 0


def test_check_entry_deterministic_block_on_opening_volatility(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM call during OPENING_VOLATILITY")))

    result = check_autonomous_entry(
        db, index, _make_features(session_phase="OPENING_VOLATILITY"), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )
    assert result is None


def test_check_entry_deterministic_block_on_square_off_zone(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM call during SQUARE_OFF_ZONE")))

    result = check_autonomous_entry(
        db, index, _make_features(session_phase="SQUARE_OFF_ZONE"), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )
    assert result is None


def test_check_entry_deterministic_block_below_adx_hard_floor(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM call below the ADX hard floor")))

    assert _ADX_HARD_FLOOR == 18.0
    result = check_autonomous_entry(
        db, index, _make_features(adx=17.9), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )
    assert result is None


def test_check_entry_deterministic_block_on_missing_adx(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM call with missing ADX")))

    result = check_autonomous_entry(
        db, index, _make_features(adx=None), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )
    assert result is None


def test_check_entry_reaches_llm_at_or_above_adx_hard_floor_in_an_allowed_phase(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "NONE", "reasoning": "borderline"}', None, 5.0))

    result = check_autonomous_entry(
        db, index, _make_features(adx=18.0, session_phase="MORNING_MOMENTUM"), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )
    assert result is None  # NONE decision, but the model WAS reached (no assertion exploded)


# ---------------------------------------------------------------------------
# _regime_matches_action / the EMA-regime override (4 Sep 2026)
#
# Real production trigger: Autonomous AI's first two live trades under this
# rebuild (both Nifty BUY_PE) opened with EMA9 > EMA21 (a BULLISH regime),
# directly contradicting SYSTEM_PROMPT_ENTRY's own required "Fast EMA <
# Slow EMA" criterion for a PE -- the model's own returned reasoning named
# the contradiction explicitly and traded anyway. See
# _regime_matches_action's own docstring for the full account.
# ---------------------------------------------------------------------------

def test_regime_matches_action_buy_ce_requires_bullish():
    bullish = _make_features(fast_ema=57000.0, slow_ema=56800.0)
    bearish = _make_features(fast_ema=56800.0, slow_ema=57000.0)
    assert _regime_matches_action(bullish, "BUY_CE") is True
    assert _regime_matches_action(bearish, "BUY_CE") is False


def test_regime_matches_action_buy_pe_requires_bearish():
    bullish = _make_features(fast_ema=57000.0, slow_ema=56800.0)
    bearish = _make_features(fast_ema=56800.0, slow_ema=57000.0)
    assert _regime_matches_action(bearish, "BUY_PE") is True
    assert _regime_matches_action(bullish, "BUY_PE") is False


def test_regime_matches_action_fails_closed_on_neutral_or_unknown():
    neutral = _make_features(fast_ema=57000.0, slow_ema=57000.0)
    unknown = _make_features(fast_ema=None, slow_ema=None)
    assert _regime_matches_action(neutral, "BUY_CE") is False
    assert _regime_matches_action(neutral, "BUY_PE") is False
    assert _regime_matches_action(unknown, "BUY_CE") is False
    assert _regime_matches_action(unknown, "BUY_PE") is False


def test_regime_matches_action_none_always_passes():
    features = _make_features(fast_ema=57000.0, slow_ema=56800.0)
    assert _regime_matches_action(features, "NONE") is True


def test_check_entry_overridden_when_regime_contradicts_decision(monkeypatch):
    # Reproduces the real 4 Sep 2026 trigger exactly: EMA9=23910.87,
    # EMA21=23908.96 (BULLISH, EMA9 > EMA21), model decides BUY_PE anyway.
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(
        module, "_call_provider",
        lambda *a, **k: module._RawCall(
            '{"decision": "BUY_PE", "confidence": 0.61, '
            '"reasoning": "Fast EMA is below Slow EMA is NOT true, so momentum alignment is contradictory; '
            'however price is strictly below VWAP and ADX confirms trend strength."}',
            None, 12.0,
        ),
    )

    result = check_autonomous_entry(
        db, index, _make_features(fast_ema=23910.87, slow_ema=23908.96), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )

    assert result is None
    assert option_finder.calls == 0


def test_check_entry_not_overridden_when_regime_agrees_with_decision(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(
        module, "_call_provider",
        lambda *a, **k: module._RawCall('{"decision": "BUY_PE", "confidence": 0.65, "reasoning": "clean bearish setup"}', None, 12.0),
    )

    result = check_autonomous_entry(
        db, index, _make_features(fast_ema=56800.0, slow_ema=57000.0), to_ist(utc_now()), _Settings(), FakeSmartAPI(), option_finder,
    )

    assert result is not None
    assert result.signal == "BUY_PE"


def test_check_entry_opens_a_trade_on_buy_decision(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())

    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "BUY_PE", "confidence": 0.6, "reasoning": "drifting down"}', None, 12.0))

    # EMA regime must actually support the model's own chosen direction --
    # see test_check_entry_overridden_when_regime_contradicts_decision for
    # the case where it doesn't.
    result = check_autonomous_entry(
        db, index, _make_features(fast_ema=56800.0, slow_ema=57000.0), to_ist(utc_now()), _Settings(), FakeSmartAPI(price=100.0), option_finder,
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

    result = check_autonomous_entry(db, index, _make_features(), to_ist(utc_now()), _Settings(), FakeSmartAPI(price=100.0), option_finder)
    assert result is None
    assert option_finder.calls == 0


def test_check_entry_provider_error_opens_nothing(monkeypatch):
    import app.ai.autonomous as module
    db = _make_session()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract())

    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: module._RawCall(None, "HTTP 500", None))

    result = check_autonomous_entry(db, index, _make_features(), to_ist(utc_now()), _Settings(), FakeSmartAPI(price=100.0), option_finder)
    assert result is None
    assert option_finder.calls == 0


# ---------------------------------------------------------------------------
# check_autonomous_exits -- LLM discretion path (unchanged shape)
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


# ---------------------------------------------------------------------------
# check_autonomous_exits -- unconditional 15:00 cutoff (unchanged)
# ---------------------------------------------------------------------------

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
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 30, tzinfo=IST))
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_autonomous_exits -- AUTONOMOUS_SESSION_CLOSE (3 Sep 2026, doc rebuild)
# ---------------------------------------------------------------------------

def test_check_exits_session_close_warning_fires_at_15_minutes_remaining(monkeypatch):
    import app.ai.autonomous as module
    assert _SESSION_CLOSE_WARNING_MINUTES == 15
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 14, 45, tzinfo=IST))
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call inside the session-close window")))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "AUTONOMOUS_SESSION_CLOSE"


def test_check_exits_no_session_close_warning_before_the_window(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 14, 44, tzinfo=IST))
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=105.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_autonomous_exits -- AUTONOMOUS_TRAIL_EXIT (fixed-width peak-giveback)
# ---------------------------------------------------------------------------

def test_check_exits_peak_giveback_fires_at_20_percent_peak_8_percent_drop(monkeypatch):
    import app.ai.autonomous as module
    assert _PEAK_GIVEBACK_ACTIVATE_PERCENT == 20.0
    assert _PEAK_GIVEBACK_WIDTH_PERCENT == 8.0
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    # entry 100, peak 122 (22% MFE, clears the 20% floor), now at 112 (12%,
    # a 10-point drop from peak -- clears the 8% width).
    _add_trade(db, trade_id="t1", entry_price=100.0, current_premium=112.0, highest_price=122.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call once the giveback guard fires")))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "AUTONOMOUS_TRAIL_EXIT"


def test_check_exits_peak_giveback_does_not_fire_below_the_activation_floor(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    # Peak only 14% (below the 20% floor, and below the breakeven-violation
    # 15% floor too) -- must not trigger either deterministic rule; falls
    # through to the model.
    _add_trade(db, trade_id="t1", entry_price=100.0, current_premium=105.0, highest_price=114.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_autonomous_exits -- AUTONOMOUS_BREAKEVEN_EXIT
# ---------------------------------------------------------------------------

def test_check_exits_breakeven_violation_fires_at_15_percent_peak_1_percent_floor(monkeypatch):
    import app.ai.autonomous as module
    assert _BREAKEVEN_VIOLATION_ACTIVATE_PERCENT == 15.0
    assert _BREAKEVEN_VIOLATION_FLOOR_PERCENT == 1.0
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    # Peak 16% (clears 15%), now back down to 0.5% (<=1%) -- but NOT past the
    # 20%/8% peak-giveback rule (peak below 20%), so this is genuinely
    # testing the breakeven rule specifically, not the giveback rule firing
    # first for an unrelated reason.
    _add_trade(db, trade_id="t1", entry_price=100.0, current_premium=100.5, highest_price=116.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call once breakeven-violation fires")))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "AUTONOMOUS_BREAKEVEN_EXIT"


def test_check_exits_breakeven_violation_does_not_fire_above_the_floor(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    # Peak 16% (clears the activate threshold) but current is still +5%,
    # well above the <=1% floor -- must not fire. Also below the 20%
    # peak-giveback floor, so that rule can't fire here either.
    _add_trade(db, trade_id="t1", entry_price=100.0, current_premium=105.0, highest_price=116.0, pnl_percent=5.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_autonomous_exits -- AUTONOMOUS_STALL_EXIT, window now 25 min (was 60)
# ---------------------------------------------------------------------------

def _add_trade_at(db, *, trade_id, entry_time_utc, pnl_percent, current_premium=100.0, entry_price=100.0,
                   highest_price=None, option_type="CE") -> None:
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Autonomous AI - Bank Nifty", signal=f"BUY_{option_type}",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type=option_type, quantity=35,
        entry_price=entry_price, current_premium=current_premium, pnl_percent=pnl_percent,
        stoploss=65.0, target=150.0, entry_time=entry_time_utc, origin=ORIGIN,
        status=TradeStatus.OPEN, result=TradeResult.OPEN, mode=TradingMode.PAPER,
        highest_price=highest_price,
    ))
    db.commit()


def test_stall_window_is_25_minutes_per_the_design_document():
    assert _STALL_WINDOW_MINUTES == 25


def test_check_exits_closes_a_stalled_trade_with_no_model_call(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    # 30 minutes before "now" -- past the (now 25-minute) stall window.
    _add_trade_at(db, trade_id="t1", entry_time_utc=datetime(2026, 8, 31, 6, 0, tzinfo=UTC), pnl_percent=1.5, highest_price=101.5)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call for a stalled trade")))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "AUTONOMOUS_STALL_EXIT"


def test_check_exits_does_not_stall_before_the_window_elapses(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    # 10 minutes before "now" -- inside the 25-minute stall window, so this
    # must still reach the model's own HOLD/EXIT judgment as normal.
    _add_trade_at(db, trade_id="t1", entry_time_utc=datetime(2026, 8, 31, 6, 20, tzinfo=UTC), pnl_percent=1.5, highest_price=101.5)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_does_not_stall_a_trade_that_has_moved(monkeypatch):
    import app.ai.autonomous as module
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    # 30 minutes elapsed (past the window) but +8% P&L is outside the +-5%
    # stall band, and peak (108, only 8%) doesn't clear either the giveback
    # or breakeven activation floors -- a real, moving trade must still
    # reach the model.
    _add_trade_at(db, trade_id="t1", entry_time_utc=datetime(2026, 8, 31, 6, 0, tzinfo=UTC), pnl_percent=8.0, highest_price=108.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0))

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# check_autonomous_exits -- AUTONOMOUS_STRUCTURAL_EXIT
# ---------------------------------------------------------------------------

def test_check_exits_structural_invalidation_fires_when_features_contradict_side(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=100.5, entry_price=100.0, option_type="CE", highest_price=101.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call once structural invalidation fires")))

    features_by_index = {"BANKNIFTY": _make_features(vwap=57200.0, spot=57000.0)}  # spot < vwap -> BELOW_VWAP, contradicts CE
    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0), features_by_index)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == "AUTONOMOUS_STRUCTURAL_EXIT"


def test_check_exits_structural_invalidation_does_not_fire_when_aligned(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=100.5, entry_price=100.0, option_type="CE", highest_price=101.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    features_by_index = {"BANKNIFTY": _make_features(vwap=56800.0, spot=57000.0)}  # spot > vwap -> ABOVE_VWAP, aligned with CE
    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0), features_by_index)

    trade = db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").one()
    assert trade.status == TradeStatus.OPEN


def test_check_exits_structural_invalidation_skipped_without_features(monkeypatch):
    import app.ai.autonomous as module
    _before_cutoff(monkeypatch, module)
    db = _make_session()
    _add_trade(db, trade_id="t1", current_premium=100.5, entry_price=100.0, option_type="CE", highest_price=101.0)
    trade_manager = _make_trade_manager()
    monkeypatch.setattr(module, "_call_provider",
                         lambda *a, **k: module._RawCall('{"decision": "HOLD", "reasoning": "still developing"}', None, 5.0))

    check_autonomous_exits(db, trade_manager, _Settings(), (15, 0), None)

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

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 20, 0, tzinfo=IST))  # night
    db = _make_session()

    def _exploding(*a, **k):
        raise AssertionError("must not reach settings lookup outside market hours")

    monkeypatch.setattr(module, "get_settings", _exploding)

    run_autonomous_checks(FakeSmartAPI(), FakeOptionFinder(None), _make_trade_manager(), db=db)


def _trending_1m_rows(n: int, start_ist: datetime, base: float = 57000.0) -> list:
    """n consecutive 1-minute SmartAPI-shaped rows with a clean, consistent
    upward drift -- enough for ADX to warm up comfortably above the 18/20
    floors this module now gates on, same shape as the trend-generating
    helpers other tests in this project already use for the same purpose."""
    rows = []
    price = base
    for i in range(n):
        price += 1.5 if i % 3 != 0 else -0.3
        ts = start_ist + timedelta(minutes=i)
        rows.append([ts.strftime("%Y-%m-%dT%H:%M:%S+05:30"), price - 1, price + 1, price - 2, price, 0])
    return rows


class _TrendingSmartAPI(FakeSmartAPI):
    """A SmartAPI stand-in whose get_candles returns a real, ADX-warm
    trending series -- needed for run_autonomous_checks' end-to-end tests
    now that entries go through the full deterministic feature engine
    (_compute_features) rather than a bare price/range dict."""

    def get_candles(self, *_args, **_kwargs):
        return _trending_1m_rows(200, datetime(2026, 8, 31, 6, 0))


def test_run_autonomous_checks_blocks_new_entries_at_the_dedicated_3pm_cutoff(monkeypatch):
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

    run_autonomous_checks(_TrendingSmartAPI(price=100.0, spot=57000.0), option_finder, _make_trade_manager(), db=db)

    assert option_finder.calls == 0


def test_run_autonomous_checks_still_enters_before_the_3pm_cutoff(monkeypatch):
    import app.ai.autonomous as module
    from app.ai.repository import create_settings

    # 10:00 IST -- MORNING_MOMENTUM, not CHOP_ZONE (11:15-13:30 is
    # deterministically blocked per the design document, so noon would not
    # reach the model at all -- this must land in an allowed phase).
    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 10, 0, tzinfo=IST))
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

    run_autonomous_checks(_TrendingSmartAPI(price=57000.0, spot=57000.0), option_finder, _make_trade_manager(), db=db)

    # The model WAS actually asked (declined) -- confirms 10:00 IST is
    # correctly inside the trading window (MORNING_MOMENTUM) and the feature
    # engine produced a real, ADX-warm feature set that cleared both
    # deterministic gates.
    assert len(calls) == 1


def test_run_autonomous_checks_skips_entry_when_feature_engine_has_no_history(monkeypatch):
    # The default FakeSmartAPI.get_candles returns [] -> build_market_context
    # has nothing to work with -> features is None for every index -> the
    # entry check must skip cleanly (not crash) rather than reach the model.
    import app.ai.autonomous as module
    from app.ai.repository import create_settings

    monkeypatch.setattr(module, "utc_now", lambda: datetime(2026, 8, 31, 12, 0, tzinfo=IST))
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai")
    db.commit()

    option_finder = FakeOptionFinder(_make_contract())
    monkeypatch.setattr(module, "_call_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM call with no feature history")))

    run_autonomous_checks(FakeSmartAPI(price=57000.0, spot=57000.0), option_finder, _make_trade_manager(), db=db)

    assert option_finder.calls == 0
