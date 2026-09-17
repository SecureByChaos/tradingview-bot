"""The Autonomous AI decision log must capture NONE and blocked decisions,
not just opened trades -- NONE is the module's own dominant output, and
until this table it left no queryable trace at all. See CLAUDE.md's
"Autonomous AI CE/PE bias investigation" entry (17 Sep 2026).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai.autonomous_log import record_entry_decision
from app.db_models import AutonomousAILog, Base, StrategyTrade, TradeStatus, TradingMode
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


@dataclass
class FakeFeatures:
    spot: float = 57000.0
    vwap: float | None = 56900.0
    vwap_relation: str = "ABOVE_VWAP"
    fast_ema: float | None = 57000.0
    slow_ema: float | None = 56800.0
    trend_regime: str = "BULLISH"
    adx: float | None = 27.4
    dist_to_pdh: float | None = 120.5
    dist_to_pdl: float | None = -340.2
    session_phase: str = "MORNING_MOMENTUM"
    chop_efficiency_ratio: float | None = 0.62
    recent_price_change_percent: float | None = 0.15


def _only_row(db: Session) -> AutonomousAILog:
    rows = list(db.scalars(select(AutonomousAILog)))
    assert len(rows) == 1
    return rows[0]


def test_records_a_none_decision_with_full_feature_snapshot():
    db = _make_session()
    record_entry_decision(
        db, index_symbol="BANKNIFTY", features=FakeFeatures(), raw_decision="NONE",
        confidence=0.42, reasoning="chop, no clean setup", latency_ms=8.1,
    )
    row = _only_row(db)
    assert row.index_name == "BANKNIFTY"
    assert row.raw_decision == "NONE"
    assert row.block_reason is None
    assert row.confidence == 0.42
    assert row.reasoning == "chop, no clean setup"
    assert row.trade_id is None
    assert row.spot == 57000.0
    assert row.vwap == 56900.0
    assert row.vwap_relation == "ABOVE_VWAP"
    assert row.fast_ema == 57000.0
    assert row.slow_ema == 56800.0
    assert row.trend_regime == "BULLISH"
    assert row.adx == 27.4
    assert row.dist_to_pdh == 120.5
    assert row.dist_to_pdl == -340.2
    assert row.session_phase == "MORNING_MOMENTUM"
    assert row.chop_efficiency_ratio == 0.62
    assert row.recent_price_change_percent == 0.15
    assert row.latency_ms == 8.1


def test_records_a_deterministic_pre_call_block_with_no_model_involved():
    db = _make_session()
    record_entry_decision(
        db, index_symbol="NIFTY", features=FakeFeatures(session_phase="CHOP_ZONE"),
        raw_decision="NONE", block_reason="SESSION_PHASE",
        reasoning="Blocked before any model call -- session phase CHOP_ZONE",
    )
    row = _only_row(db)
    assert row.raw_decision == "NONE"
    assert row.block_reason == "SESSION_PHASE"
    assert row.confidence is None
    assert row.latency_ms is None


def test_records_the_models_raw_direction_even_when_overridden():
    # The whole point of raw_decision: it must show BUY_PE here, not the NONE
    # the override actually produced -- otherwise the CE/PE bias question
    # this table exists to answer would be answering the wrong question.
    db = _make_session()
    record_entry_decision(
        db, index_symbol="NIFTY", features=FakeFeatures(trend_regime="BULLISH"),
        raw_decision="BUY_PE", block_reason="EMA_REGIME_OVERRIDE",
        confidence=0.61, reasoning="bearish thesis despite bullish EMA stack",
    )
    row = _only_row(db)
    assert row.raw_decision == "BUY_PE"
    assert row.block_reason == "EMA_REGIME_OVERRIDE"


def test_records_trade_id_when_a_trade_actually_opened():
    db = _make_session()
    trade = StrategyTrade(
        trade_id="abc123", strategy_name="Autonomous AI - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", tradingsymbol="X", symboltoken="1", strike=23000,
        expiry="28AUG2026", option_type="CE", quantity=75, entry_price=100.0,
        current_premium=100.0, stoploss=65.0, target=150.0, entry_time=utc_now(),
        origin="AUTONOMOUS_AI", status=TradeStatus.OPEN, mode=TradingMode.PAPER,
    )
    db.add(trade)
    db.commit()

    record_entry_decision(
        db, index_symbol="NIFTY", features=FakeFeatures(), raw_decision="BUY_CE",
        confidence=0.7, reasoning="clean breakout", trade=trade,
    )
    row = _only_row(db)
    assert row.trade_id == "abc123"
    assert row.block_reason is None


def test_records_execution_failed_when_open_returns_none_after_passing_gates():
    db = _make_session()
    record_entry_decision(
        db, index_symbol="NIFTY", features=FakeFeatures(), raw_decision="BUY_CE",
        block_reason="EXECUTION_FAILED", confidence=0.7, reasoning="clean breakout", trade=None,
    )
    row = _only_row(db)
    assert row.block_reason == "EXECUTION_FAILED"
    assert row.trade_id is None


def test_records_a_provider_error_with_no_feature_snapshot_field_crashing():
    db = _make_session()
    record_entry_decision(
        db, index_symbol="NIFTY", features=FakeFeatures(), raw_decision="ERROR",
        reasoning="HTTP 500", latency_ms=None,
    )
    row = _only_row(db)
    assert row.raw_decision == "ERROR"
    assert row.reasoning == "HTTP 500"


def test_empty_reasoning_is_stored_as_none_not_an_empty_string():
    db = _make_session()
    record_entry_decision(db, index_symbol="NIFTY", features=FakeFeatures(), raw_decision="NONE", reasoning="")
    row = _only_row(db)
    assert row.reasoning is None


def test_never_raises_when_the_write_fails(monkeypatch):
    db = _make_session()

    def _exploding_add(_obj):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add", _exploding_add)
    # Must not raise -- see the module's own "swallows its own failures" docstring.
    record_entry_decision(db, index_symbol="NIFTY", features=FakeFeatures(), raw_decision="NONE")
    assert list(db.scalars(select(AutonomousAILog))) == []


def test_missing_features_object_does_not_crash():
    # features=None is a real possibility if a future caller ever logs before
    # the feature engine ran -- every field must default to None, not raise.
    db = _make_session()
    record_entry_decision(db, index_symbol="NIFTY", features=None, raw_decision="ERROR", reasoning="no features")
    row = _only_row(db)
    assert row.spot is None
    assert row.adx is None
