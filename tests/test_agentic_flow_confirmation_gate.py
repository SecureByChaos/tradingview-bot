"""Flow Confirmation Gate (22 Nov 2026) -- Agentic AI's admin opt-in, not-
yet-backtested VWAP/option-chain-OI confirmation check. Built from a real
3-month finding that BUY_CE on Nifty specifically (not Nifty broadly)
reliably underperformed the same signal on Bank Nifty; see app.ai.agentic.
_flow_gate_blocks's own docstring for the full reasoning. Self-contained,
not importing helpers from tests/test_agentic_ai_entry_exit.py, per this
project's established per-test-file convention.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.ai.agentic as agentic_module
import app.option_chain_store as chain_store
from app.ai.agentic import (
    _AgentVerdict,
    _build_technical_prompt,
    _compute_flow_reading,
    _compute_futures_vwap,
    _FlowReading,
    _nearest_expiry,
    _oi_contradicts,
    _oi_delta_percent,
    _TechnicalFeatures,
    _vwap_confirms,
    _flow_gate_blocks,
    check_agentic_entry,
)
from app.db_models import AgenticAISettings, Base, IndexConfig
from app.models import OptionContract
from app.time_utils import IST, to_ist, utc_now


@pytest.fixture(autouse=True)
def _scratch_chain_db(tmp_path, monkeypatch):
    db_path = tmp_path / "option_chain.db"
    monkeypatch.setenv("OPTION_CHAIN_DB_PATH", str(db_path))
    monkeypatch.setattr(chain_store, "_engine", None)
    monkeypatch.setattr(chain_store, "_SessionLocal", None)
    yield
    monkeypatch.setattr(chain_store, "_engine", None)
    monkeypatch.setattr(chain_store, "_SessionLocal", None)


def _make_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index(symbol: str = "NIFTY") -> IndexConfig:
    return IndexConfig(symbol=symbol, display_name="Nifty 50", enabled=True, lot_size=75, strike_interval=50, spot_token="99926000", spot_exchange="NSE")


def _make_settings(**overrides) -> AgenticAISettings:
    base = dict(
        id=1, enabled=True, api_key="sk-real", base_url="", live_model="gpt-6-luna",
        live_reasoning_effort="medium", qa_model="", qa_reasoning_effort="max", timeout_seconds=20,
        account_capital_rupees=100000.0, max_risk_percent_per_trade=10.0, max_daily_loss_percent=3.0,
        max_concurrent_positions=1, max_capital_deployed_percent=20.0,
        nominal_stop_percent=15.0, nominal_target_percent=30.0, flow_confirmation_enabled=False,
    )
    base.update(overrides)
    return AgenticAISettings(**base)


class FakeSmartAPI:
    def __init__(self, price: float = 100.0, candle_rows: list | None = None):
        self.price = price
        self._candle_rows = candle_rows if candle_rows is not None else []

    def get_ltp(self, *_a, **_k):
        return self.price

    def get_candles(self, *_a, **_k):
        return self._candle_rows


class FakeOptionFinder:
    def __init__(self, contract: OptionContract | None, futures: dict | None = None):
        self.contract = contract
        self.futures = futures
        self.calls = 0

    def find_atm_contract(self, signal, index, offset, min_dte=None):
        self.calls += 1
        if self.contract is None:
            raise ValueError("no contract available")
        return self.contract

    def find_current_futures_contract(self, index):
        return self.futures


def _make_contract(dte_days: int = 8, lot_size: int = 75) -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=dte_days)).strftime("%d%b%Y").upper()
    return OptionContract(tradingsymbol=f"NIFTY{expiry}22500CE", symboltoken="1", strike=22500, expiry=expiry, option_type="CE", lot_size=lot_size)


def _ist_now(y=2026, m=11, d=22, hh=11, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _identity_rescale(nominal, index_symbol, option_type, dte):
    return nominal, True


def _futures_row(ts: datetime, price: float, volume: float) -> list:
    return [ts.strftime("%Y-%m-%dT%H:%M:%S+05:30"), price, price + 1, price - 1, price, volume]


def _chain_row(snapshot_ts, strike, option_type, oi, symbol="NIFTY", expiry="27NOV2026", spot=22500.0):
    return {
        "snapshot_ts": snapshot_ts, "index_symbol": symbol, "expiry": expiry, "strike": strike,
        "option_type": option_type, "symboltoken": f"{strike}{option_type}", "tradingsymbol": f"{symbol}{expiry}{strike}{option_type}",
        "ltp": 100.0, "open_interest": oi, "volume": 1000.0, "implied_volatility": 15.0, "spot": spot,
    }


# ---------------------------------------------------------------------------
# _compute_futures_vwap (duplicated from app.ai.autonomous)
# ---------------------------------------------------------------------------


def test_compute_futures_vwap_from_real_volume():
    db = _make_db()
    index = _make_index()
    now_ist = datetime(2026, 11, 22, 10, 0, tzinfo=IST)
    start = datetime(2026, 11, 22, 9, 15)
    rows = [_futures_row(start, 100.0, 10.0), _futures_row(start + timedelta(minutes=1), 200.0, 30.0)]

    class _FuturesSmartAPI(FakeSmartAPI):
        def get_candles(self, *_a, **_k):
            return rows

    option_finder = FakeOptionFinder(_make_contract(), futures={"exchange": "NFO", "tradingsymbol": "X", "symboltoken": "999", "expiry": "28NOV2026"})
    vwap = _compute_futures_vwap(db, index, option_finder, _FuturesSmartAPI(), now_ist)
    assert vwap == 175.0  # (100*10 + 200*30) / 40


def test_compute_futures_vwap_none_without_a_futures_contract():
    db = _make_db()
    index = _make_index()
    option_finder = FakeOptionFinder(_make_contract(), futures=None)
    vwap = _compute_futures_vwap(db, index, option_finder, FakeSmartAPI(), _ist_now())
    assert vwap is None


# ---------------------------------------------------------------------------
# _nearest_expiry / _oi_delta_percent
# ---------------------------------------------------------------------------


def test_nearest_expiry_picks_soonest_across_month_boundary():
    rows = [
        type("R", (), {"expiry": "04DEC2026"})(),
        type("R", (), {"expiry": "27NOV2026"})(),
    ]
    assert _nearest_expiry(rows) == "27NOV2026"


def test_nearest_expiry_none_when_unparseable():
    rows = [type("R", (), {"expiry": "not-a-date"})()]
    assert _nearest_expiry(rows) is None


def test_oi_delta_percent_rising_and_falling():
    assert _oi_delta_percent(1000, 1100) == 10.0
    assert _oi_delta_percent(1000, 900) == -10.0


def test_oi_delta_percent_none_on_missing_or_zero_base():
    assert _oi_delta_percent(None, 1000) is None
    assert _oi_delta_percent(0, 1000) is None


# ---------------------------------------------------------------------------
# _compute_flow_reading -- every fail-closed path, then a real computation
# ---------------------------------------------------------------------------


def test_compute_flow_reading_none_when_archive_empty():
    assert _compute_flow_reading("NIFTY", 22500.0, _ist_now()) is None


def test_compute_flow_reading_none_with_only_one_snapshot():
    chain_store.store_snapshot([_chain_row(datetime(2026, 11, 22, 10, 30), 22500, "CE", 1000)])
    reading = _compute_flow_reading("NIFTY", 22500.0, datetime(2026, 11, 22, 10, 35, tzinfo=IST))
    assert reading is None


def test_compute_flow_reading_none_when_newest_snapshot_is_stale():
    old = datetime(2026, 11, 22, 9, 0)
    older = datetime(2026, 11, 22, 8, 55)
    chain_store.store_snapshot([_chain_row(older, 22500, "CE", 900), _chain_row(old, 22500, "CE", 1000)])
    # Newest snapshot is ~2 hours before "now" -- far past the 20-minute freshness floor.
    reading = _compute_flow_reading("NIFTY", 22500.0, datetime(2026, 11, 22, 11, 0, tzinfo=IST))
    assert reading is None


def test_compute_flow_reading_computes_pcr_and_near_strike_deltas():
    older = datetime(2026, 11, 22, 10, 0)
    newer = datetime(2026, 11, 22, 10, 15)
    chain_store.store_snapshot([
        _chain_row(older, 22500, "CE", 1000), _chain_row(older, 22400, "PE", 800),
        _chain_row(newer, 22500, "CE", 900), _chain_row(newer, 22400, "PE", 880),
    ])
    now_ist = datetime(2026, 11, 22, 10, 18, tzinfo=IST)
    reading = _compute_flow_reading("NIFTY", 22450.0, now_ist)
    assert reading is not None
    assert reading.pcr == round(880 / 900, 3)
    assert reading.call_oi_delta_percent == -10.0  # 1000 -> 900, falling: short covering shape
    assert reading.put_oi_delta_percent == 10.0  # 800 -> 880, rising


def test_compute_flow_reading_none_when_different_index_only():
    chain_store.store_snapshot([
        _chain_row(datetime(2026, 11, 22, 10, 0), 57000, "CE", 1000, symbol="BANKNIFTY"),
        _chain_row(datetime(2026, 11, 22, 10, 15), 57000, "CE", 900, symbol="BANKNIFTY"),
    ])
    reading = _compute_flow_reading("NIFTY", 22450.0, datetime(2026, 11, 22, 10, 18, tzinfo=IST))
    assert reading is None


# ---------------------------------------------------------------------------
# _vwap_confirms / _oi_contradicts / _flow_gate_blocks
# ---------------------------------------------------------------------------


def test_vwap_confirms_buy_ce_above_and_disagrees_below():
    assert _vwap_confirms("BUY_CE", 22600.0, 22500.0) is True
    assert _vwap_confirms("BUY_CE", 22400.0, 22500.0) is False


def test_vwap_confirms_buy_pe_below_and_disagrees_above():
    assert _vwap_confirms("BUY_PE", 22400.0, 22500.0) is True
    assert _vwap_confirms("BUY_PE", 22600.0, 22500.0) is False


def test_vwap_confirms_none_when_unavailable_or_at_vwap():
    assert _vwap_confirms("BUY_CE", 22500.0, None) is None
    assert _vwap_confirms("BUY_CE", 22510.0, 22500.0) is None  # inside the AT_VWAP band


def test_oi_contradicts_buy_ce_on_rising_call_oi_only():
    flow = _FlowReading(pcr=1.0, call_oi_delta_percent=12.0, put_oi_delta_percent=-12.0, snapshots_used=2)
    assert _oi_contradicts("BUY_CE", flow) is True
    assert _oi_contradicts("BUY_PE", flow) is False  # falling put OI does not contradict a BUY_PE


def test_oi_contradicts_none_inside_noise_band():
    flow = _FlowReading(pcr=1.0, call_oi_delta_percent=2.0, put_oi_delta_percent=None, snapshots_used=2)
    assert _oi_contradicts("BUY_CE", flow) is None


def test_oi_contradicts_none_when_flow_or_leg_missing():
    assert _oi_contradicts("BUY_CE", None) is None
    assert _oi_contradicts("BUY_CE", _FlowReading(pcr=None, call_oi_delta_percent=None, put_oi_delta_percent=None, snapshots_used=0)) is None


def test_flow_gate_blocks_on_vwap_contradiction():
    features = _TechnicalFeatures(
        ema9=None, ema21=None, trend_regime="UNKNOWN", adx=None, chop_efficiency_ratio=None, chop_label="UNKNOWN",
        recent_price_change_percent=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        vwap=22600.0, vwap_relation="BELOW_VWAP", flow=None,
    )
    blocked, reason = _flow_gate_blocks("BUY_CE", 22500.0, features)
    assert blocked is True
    assert "VWAP" in reason


def test_flow_gate_blocks_on_oi_contradiction_when_vwap_has_no_opinion():
    flow = _FlowReading(pcr=1.0, call_oi_delta_percent=15.0, put_oi_delta_percent=None, snapshots_used=2)
    features = _TechnicalFeatures(
        ema9=None, ema21=None, trend_regime="UNKNOWN", adx=None, chop_efficiency_ratio=None, chop_label="UNKNOWN",
        recent_price_change_percent=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        vwap=None, vwap_relation="UNKNOWN", flow=flow,
    )
    blocked, reason = _flow_gate_blocks("BUY_CE", 22500.0, features)
    assert blocked is True
    assert "call" in reason


def test_flow_gate_never_blocks_on_missing_features():
    assert _flow_gate_blocks("BUY_CE", 22500.0, None) == (False, "")


def test_flow_gate_does_not_block_when_everything_confirms_or_is_missing():
    features = _TechnicalFeatures(
        ema9=None, ema21=None, trend_regime="UNKNOWN", adx=None, chop_efficiency_ratio=None, chop_label="UNKNOWN",
        recent_price_change_percent=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        vwap=22400.0, vwap_relation="ABOVE_VWAP", flow=None,
    )
    assert _flow_gate_blocks("BUY_CE", 22500.0, features) == (False, "")


# ---------------------------------------------------------------------------
# check_agentic_entry integration -- the gate itself
# ---------------------------------------------------------------------------


_CONTRADICTING_FEATURES = _TechnicalFeatures(
    ema9=None, ema21=None, trend_regime="UNKNOWN", adx=None, chop_efficiency_ratio=None, chop_label="UNKNOWN",
    recent_price_change_percent=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
    vwap=110.0, vwap_relation="BELOW_VWAP", flow=None,
)


def test_check_agentic_entry_toggle_off_opens_trade_despite_contradicting_flow_data(monkeypatch):
    """Regression: with the gate disabled (the default), a VWAP/OI
    reading that would otherwise block must change nothing -- deploying
    this feature must not alter any existing behaviour on its own."""
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"BUY_CE","confidence":0.75,"reasoning":"clean breakout"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(flow_confirmation_enabled=False)
    trade = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()),
        {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "clean uptrend"),
        technical_features=_CONTRADICTING_FEATURES,
    )
    assert trade is not None


def test_check_agentic_entry_toggle_on_blocks_on_vwap_contradiction(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"BUY_CE","confidence":0.75,"reasoning":"clean breakout"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(flow_confirmation_enabled=True)
    option_finder = FakeOptionFinder(_make_contract())
    trade = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(price=100.0), option_finder, {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "clean uptrend"),
        technical_features=_CONTRADICTING_FEATURES,
    )
    assert trade is None
    assert option_finder.calls == 0  # never even reached contract resolution


def test_check_agentic_entry_toggle_on_blocks_on_oi_contradiction(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"BUY_CE","confidence":0.75,"reasoning":"clean breakout"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(flow_confirmation_enabled=True)
    flow = _FlowReading(pcr=1.0, call_oi_delta_percent=20.0, put_oi_delta_percent=None, snapshots_used=2)
    features = _TechnicalFeatures(
        ema9=None, ema21=None, trend_regime="UNKNOWN", adx=None, chop_efficiency_ratio=None, chop_label="UNKNOWN",
        recent_price_change_percent=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        vwap=None, vwap_relation="UNKNOWN", flow=flow,
    )
    trade = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "clean uptrend"),
        technical_features=features,
    )
    assert trade is None


def test_check_agentic_entry_toggle_on_still_opens_when_nothing_contradicts(monkeypatch):
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"BUY_CE","confidence":0.75,"reasoning":"clean breakout"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(flow_confirmation_enabled=True)
    features = _TechnicalFeatures(
        ema9=None, ema21=None, trend_regime="UNKNOWN", adx=None, chop_efficiency_ratio=None, chop_label="UNKNOWN",
        recent_price_change_percent=None, pdh=None, pdl=None, dist_to_pdh=None, dist_to_pdl=None,
        vwap=90.0, vwap_relation="ABOVE_VWAP", flow=None,
    )
    trade = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "clean uptrend"),
        technical_features=features,
    )
    assert trade is not None


def test_check_agentic_entry_toggle_on_still_opens_when_technical_features_absent(monkeypatch):
    """A real missing-data case (e.g. a cycle where VWAP/flow couldn't be
    computed) must fail open, never closed."""
    monkeypatch.setattr(agentic_module, "symmetric_premium_percent", _identity_rescale)
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"BUY_CE","confidence":0.75,"reasoning":"clean breakout"}', None),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(flow_confirmation_enabled=True)
    trade = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(price=100.0), FakeOptionFinder(_make_contract()), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "clean uptrend"),
        technical_features=None,
    )
    assert trade is not None


def test_check_agentic_entry_gate_never_evaluated_on_a_none_decision(monkeypatch):
    """The gate only ever runs after the Orchestrator has already chosen
    a direction -- a NONE decision must decline exactly as before,
    without even reaching _flow_gate_blocks."""
    monkeypatch.setattr(
        agentic_module, "_call_openai_live",
        lambda *a, **k: agentic_module._LiveCallResult('{"decision":"NONE","confidence":0.3,"reasoning":"unclear"}', None),
    )
    monkeypatch.setattr(
        agentic_module, "_flow_gate_blocks",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called on a NONE decision")),
    )
    db = _make_db()
    index = _make_index()
    settings = _make_settings(flow_confirmation_enabled=True)
    trade = check_agentic_entry(
        db, index, _ist_now(), settings, FakeSmartAPI(price=100.0), FakeOptionFinder(None), {"NIFTY": {"price": 100.0}},
        technical_verdict=_AgentVerdict("BULLISH", 0.7, "clean uptrend"),
        technical_features=_CONTRADICTING_FEATURES,
    )
    assert trade is None
