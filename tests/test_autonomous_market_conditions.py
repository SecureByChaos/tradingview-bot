"""get_autonomous_ai_market_conditions -- the same read-only, zero-new-
computation pattern as get_market_conditions (tests/test_market_conditions.
py), reading AutonomousAILog instead of AIOriginationLog. Replaced the AI-
Origination version on the live dashboard 17 Sep 2026, once AI Origination
was paused. See CLAUDE.md's 17 Sep entry.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AutonomousAILog, Base, IndexConfig
from app.market_context import ADX_NO_TREND, ADX_TRENDING
from app.platform import get_autonomous_ai_market_conditions
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _seed_index(db: Session, symbol: str = "BANKNIFTY", enabled: bool = True) -> None:
    db.add(IndexConfig(symbol=symbol, display_name="Bank Nifty", enabled=enabled))
    db.commit()


def _seed_log(
    db: Session, *, index_name: str, trend_regime: str | None, adx: float | None,
    session_phase: str | None = "MORNING_MOMENTUM", vwap_relation: str | None = "ABOVE_VWAP",
    recent_price_change_percent: float | None = None, minutes_ago: float = 1.0,
    raw_decision: str = "NONE", block_reason: str | None = None,
    chop_efficiency_ratio: float | None = None, confidence: float | None = None,
) -> None:
    db.add(
        AutonomousAILog(
            timestamp=utc_now() - timedelta(minutes=minutes_ago),
            index_name=index_name,
            raw_decision=raw_decision,
            block_reason=block_reason,
            trend_regime=trend_regime,
            adx=adx,
            session_phase=session_phase,
            vwap_relation=vwap_relation,
            recent_price_change_percent=recent_price_change_percent,
            chop_efficiency_ratio=chop_efficiency_ratio,
            confidence=confidence,
        )
    )
    db.commit()


def test_returns_unknown_placeholder_when_no_log_exists_yet():
    db = _make_session()
    _seed_index(db)

    conditions = get_autonomous_ai_market_conditions(db)

    assert len(conditions) == 1
    entry = conditions[0]
    assert entry["symbol"] == "BANKNIFTY"
    assert entry["tradability"] == "UNKNOWN"
    assert entry["trend_regime"] is None
    assert entry["session_phase"] is None
    assert entry["vwap_relation"] is None
    assert entry["recent_price_change_percent"] is None
    assert entry["chop_efficiency_ratio"] is None
    assert entry["chop_label"] == "UNKNOWN"
    assert entry["confidence"] is None


def test_reads_latest_log_row_not_the_highest_adx():
    db = _make_session()
    _seed_index(db)
    _seed_log(db, index_name="BANKNIFTY", trend_regime="BEARISH", adx=28.5, minutes_ago=10)
    _seed_log(db, index_name="BANKNIFTY", trend_regime="NEUTRAL", adx=18.0, minutes_ago=1)

    entry = get_autonomous_ai_market_conditions(db)[0]

    assert entry["trend_regime"] == "NEUTRAL"
    assert entry["adx"] == 18.0
    assert entry["tradability"] == "NOT_TRADABLE"


def test_tradability_bands_match_shared_thresholds():
    db = _make_session()
    _seed_index(db)
    _seed_log(db, index_name="BANKNIFTY", trend_regime="BULLISH", adx=ADX_TRENDING)

    entry = get_autonomous_ai_market_conditions(db)[0]

    assert entry["tradability"] == "TRENDING"

    db2 = _make_session()
    _seed_index(db2)
    _seed_log(db2, index_name="BANKNIFTY", trend_regime="NEUTRAL", adx=ADX_NO_TREND - 0.1)
    assert get_autonomous_ai_market_conditions(db2)[0]["tradability"] == "NOT_TRADABLE"


def test_session_phase_and_vwap_relation_and_recent_move_passed_through():
    db = _make_session()
    _seed_index(db)
    _seed_log(
        db, index_name="BANKNIFTY", trend_regime="BEARISH", adx=24.3,
        session_phase="AFTERNOON_TREND", vwap_relation="BELOW_VWAP",
        recent_price_change_percent=-0.42,
    )

    entry = get_autonomous_ai_market_conditions(db)[0]

    assert entry["session_phase"] == "AFTERNOON_TREND"
    assert entry["vwap_relation"] == "BELOW_VWAP"
    assert entry["recent_price_change_percent"] == -0.42


def test_disabled_index_excluded():
    db = _make_session()
    _seed_index(db, symbol="SENSEX", enabled=False)

    assert get_autonomous_ai_market_conditions(db) == []


def test_multiple_indexes_each_get_their_own_latest_row():
    db = _make_session()
    _seed_index(db, symbol="BANKNIFTY")
    _seed_index(db, symbol="NIFTY")
    _seed_log(db, index_name="BANKNIFTY", trend_regime="BULLISH", adx=30.0)
    _seed_log(db, index_name="NIFTY", trend_regime="NEUTRAL", adx=12.0)

    conditions = {c["symbol"]: c for c in get_autonomous_ai_market_conditions(db)}

    assert conditions["BANKNIFTY"]["tradability"] == "TRENDING"
    assert conditions["NIFTY"]["tradability"] == "NOT_TRADABLE"


def test_chop_and_confidence_read_from_the_latest_row():
    db = _make_session()
    _seed_index(db)
    _seed_log(
        db, index_name="BANKNIFTY", trend_regime="BEARISH", adx=28.4,
        raw_decision="BUY_PE", chop_efficiency_ratio=0.22, confidence=0.78,
    )

    entry = get_autonomous_ai_market_conditions(db)[0]

    assert entry["chop_efficiency_ratio"] == 0.22
    assert entry["chop_label"] == "CHOPPY"
    assert entry["confidence"] == 0.78


def test_confidence_is_none_on_a_position_open_marker_row():
    # A POSITION_OPEN marker row (see check_autonomous_entry, 17 Sep 2026)
    # carries a real feature snapshot -- computed every cycle regardless of
    # whether a position is already open -- but no real model decision, so
    # confidence must read None honestly rather than a stale prior value.
    db = _make_session()
    _seed_index(db)
    _seed_log(
        db, index_name="BANKNIFTY", trend_regime="BEARISH", adx=24.3,
        raw_decision="NONE", block_reason="POSITION_OPEN",
        chop_efficiency_ratio=0.55, confidence=None,
    )

    entry = get_autonomous_ai_market_conditions(db)[0]

    assert entry["adx"] == 24.3
    assert entry["chop_efficiency_ratio"] == 0.55
    assert entry["confidence"] is None
