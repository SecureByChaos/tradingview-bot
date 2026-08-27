"""Four 0-100 sub-scores (setup_quality/entry_quality/risk_quality/
market_alignment) recorded alongside confidence, 26 Aug 2026 -- pure
instrumentation for future calibration research, no gating/sizing change.
See CLAUDE.md's dated entry and the confidence-calibration discussion that
preceded it.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.originator import SYSTEM_PROMPT, _Decision, _open_trade, _parse_response
from app.db_models import Base, IndexConfig, StrategyTrade, TradeStatus, TradingMode
from app.models import OptionContract, Signal
from app.time_utils import to_ist, utc_now


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

_FULL_RESPONSE = (
    '{"decision": "BUY_CE", "confidence": 0.78, "setup_quality": 82, '
    '"entry_quality": 79, "risk_quality": 76, "market_alignment": 74, '
    '"sl_percent": 10, "target_percent": 20, "reasoning": "clean setup"}'
)


def test_parse_response_reads_all_four_sub_scores():
    decision = _parse_response(_FULL_RESPONSE)
    assert decision.setup_quality == 82.0
    assert decision.entry_quality == 79.0
    assert decision.risk_quality == 76.0
    assert decision.market_alignment == 74.0
    # Existing fields must be unaffected by the new ones being present.
    assert decision.action == "BUY_CE"
    assert decision.confidence == 0.78


def test_parse_response_clamps_out_of_range_scores():
    text = (
        '{"decision": "NONE", "confidence": 0.4, "setup_quality": 140, '
        '"entry_quality": -20, "reasoning": "x"}'
    )
    decision = _parse_response(text)
    assert decision.setup_quality == 100.0
    assert decision.entry_quality == 0.0


def test_parse_response_missing_sub_scores_default_to_none():
    text = '{"decision": "NONE", "confidence": 0.4, "reasoning": "x"}'
    decision = _parse_response(text)
    assert decision.setup_quality is None
    assert decision.entry_quality is None
    assert decision.risk_quality is None
    assert decision.market_alignment is None


def test_parse_response_invalid_sub_score_type_is_none_not_an_error():
    text = (
        '{"decision": "NONE", "confidence": 0.4, "risk_quality": "n/a", '
        '"reasoning": "x"}'
    )
    decision = _parse_response(text)
    assert decision.action == "NONE"  # not ERROR -- a bad sub-score must not fail the whole parse
    assert decision.risk_quality is None


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT
# ---------------------------------------------------------------------------

def test_system_prompt_requests_all_four_sub_scores():
    for field in ("setup_quality", "entry_quality", "risk_quality", "market_alignment"):
        assert f'"{field}": 0-100' in SYSTEM_PROMPT


def test_system_prompt_states_sub_scores_do_not_gate_or_size():
    assert "do not currently gate or size any trade" in SYSTEM_PROMPT


def test_system_prompt_confidence_schema_and_calibration_paragraph_survive():
    # Confirms this addition didn't clobber the 19 Aug confidence-calibration
    # rewrite it was inserted next to.
    assert '"confidence": 0-1' in SYSTEM_PROMPT
    assert "full 0.0-1.0 range" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# _open_trade persistence
# ---------------------------------------------------------------------------

def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index() -> IndexConfig:
    return IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty", ai_origination_live_trade=False)


def _make_contract() -> OptionContract:
    expiry = (to_ist(utc_now()).date() + timedelta(days=20)).strftime("%d%b%Y").upper()
    return OptionContract(
        tradingsymbol="BANKNIFTY25AUG2650000CE",
        symboltoken="123",
        strike=50000,
        expiry=expiry,
        option_type="CE",
        lot_size=35,
    )


class _FakeSettings:
    live_trading = False


class FakeSmartAPI:
    def __init__(self, price: float = 100.0) -> None:
        self.price = price
        self.settings = _FakeSettings()

    def get_ltp(self, *_args, **_kwargs) -> float:
        return self.price

    def place_market_order(self, *_args, **_kwargs) -> str:
        raise AssertionError("should never place a real order in these tests")


class FakeOptionFinder:
    def __init__(self, contract: OptionContract) -> None:
        self.contract = contract

    def find_atm_contract(self, signal: Signal, index: IndexConfig, offset: int, min_dte: int | None = None) -> OptionContract:
        return self.contract


def test_open_trade_persists_sub_scores_onto_the_strategy_trade():
    db = _make_session()
    index = _make_index()
    decision = _Decision(
        action="BUY_CE", confidence=0.78, sl_percent=10.0, target_percent=20.0, reasoning="clean setup",
        setup_quality=82.0, entry_quality=79.0, risk_quality=76.0, market_alignment=74.0,
    )
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "openai", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.ai_setup_quality == 82.0
    assert trade.ai_entry_quality == 79.0
    assert trade.ai_risk_quality == 76.0
    assert trade.ai_market_alignment == 74.0


def test_open_trade_with_no_sub_scores_still_opens_and_stores_none():
    # Regression guard: adding these fields must not change whether or how a
    # trade opens when a provider (or an older cached response) omits them.
    db = _make_session()
    index = _make_index()
    decision = _Decision(action="BUY_CE", confidence=0.78, sl_percent=10.0, target_percent=20.0, reasoning="clean setup")
    smartapi = FakeSmartAPI()
    option_finder = FakeOptionFinder(_make_contract())

    trade = _open_trade(db, index, "openai", decision, smartapi, option_finder)

    assert trade is not None
    assert trade.status == TradeStatus.OPEN
    assert trade.mode == TradingMode.PAPER
    assert trade.ai_setup_quality is None
    assert trade.ai_market_alignment is None
