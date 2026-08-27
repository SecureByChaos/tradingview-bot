from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, IndexConfig
from app.market_context import ADX_NO_TREND, ADX_TRENDING
from app.platform import _classify_chop, _classify_tradability, get_market_conditions
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _seed_index(db: Session, symbol: str = "BANKNIFTY", enabled: bool = True) -> None:
    db.add(IndexConfig(symbol=symbol, display_name="Bank Nifty", enabled=enabled))
    db.commit()


def _seed_log(db: Session, *, index_name: str, regime: str, adx: float | None, cpr: str | None,
              setups: list[str], data_stale: bool = False, minutes_ago: float = 1.0,
              decision: str = "NONE", chop_efficiency_ratio: float | None = None,
              confidence: float | None = None, setup_quality: float | None = None,
              entry_quality: float | None = None, risk_quality: float | None = None,
              market_alignment: float | None = None) -> None:
    db.add(
        AIOriginationLog(
            timestamp=utc_now() - timedelta(minutes=minutes_ago),
            index_name=index_name,
            provider="claude",
            provider_role="primary",
            decision=decision,
            regime=regime,
            adx=adx,
            cpr=cpr,
            setups=json.dumps(setups),
            data_stale=data_stale,
            context_json="{}",
            chop_efficiency_ratio=chop_efficiency_ratio,
            confidence=confidence,
            setup_quality=setup_quality,
            entry_quality=entry_quality,
            risk_quality=risk_quality,
            market_alignment=market_alignment,
        )
    )
    db.commit()


def test_classify_tradability_bands():
    assert _classify_tradability(None) == "UNKNOWN"
    assert _classify_tradability(ADX_NO_TREND - 0.1) == "NOT_TRADABLE"
    assert _classify_tradability(ADX_NO_TREND) == "MARGINAL"
    assert _classify_tradability(ADX_TRENDING - 0.1) == "MARGINAL"
    assert _classify_tradability(ADX_TRENDING) == "TRENDING"
    assert _classify_tradability(ADX_TRENDING + 5) == "TRENDING"


def test_returns_unknown_placeholder_when_no_log_exists_yet():
    db = _make_session()
    _seed_index(db)

    conditions = get_market_conditions(db)

    assert len(conditions) == 1
    entry = conditions[0]
    assert entry["symbol"] == "BANKNIFTY"
    assert entry["tradability"] == "UNKNOWN"
    assert entry["regime"] is None
    assert entry["setups"] == []


def test_reads_latest_log_row():
    db = _make_session()
    _seed_index(db)
    _seed_log(db, index_name="BANKNIFTY", regime="TREND", adx=28.5, cpr="NARROW",
               setups=["EMA_STACK_UP", "TREND_REGIME"], minutes_ago=10)
    _seed_log(db, index_name="BANKNIFTY", regime="MIXED", adx=18.0, cpr="WIDE",
               setups=[], minutes_ago=1)

    conditions = get_market_conditions(db)

    entry = conditions[0]
    # Most recent row wins, not the first/highest ADX.
    assert entry["regime"] == "MIXED"
    assert entry["adx"] == 18.0
    assert entry["cpr"] == "WIDE"
    assert entry["tradability"] == "NOT_TRADABLE"
    assert entry["setups"] == []


def test_setups_parsed_from_json_and_tradability_trending():
    db = _make_session()
    _seed_index(db)
    _seed_log(db, index_name="BANKNIFTY", regime="TREND", adx=30.0, cpr="NARROW",
               setups=["EMA_STACK_UP", "ORB_BREAK_UP", "TREND_REGIME"])

    entry = get_market_conditions(db)[0]

    assert entry["setups"] == ["EMA_STACK_UP", "ORB_BREAK_UP", "TREND_REGIME"]
    assert entry["tradability"] == "TRENDING"


def test_data_stale_flag_passed_through():
    db = _make_session()
    _seed_index(db)
    _seed_log(db, index_name="BANKNIFTY", regime="MIXED", adx=22.0, cpr="MODERATE",
               setups=[], data_stale=True)

    entry = get_market_conditions(db)[0]

    assert entry["data_stale"] is True
    assert entry["tradability"] == "MARGINAL"


def test_disabled_index_excluded():
    db = _make_session()
    _seed_index(db, symbol="SENSEX", enabled=False)

    assert get_market_conditions(db) == []


def test_multiple_indexes_each_get_their_own_latest_row():
    db = _make_session()
    _seed_index(db, symbol="BANKNIFTY")
    _seed_index(db, symbol="NIFTY")
    _seed_log(db, index_name="BANKNIFTY", regime="TREND", adx=30.0, cpr="NARROW", setups=[])
    _seed_log(db, index_name="NIFTY", regime="RANGE", adx=12.0, cpr="WIDE", setups=[])

    conditions = {c["symbol"]: c for c in get_market_conditions(db)}

    assert conditions["BANKNIFTY"]["tradability"] == "TRENDING"
    assert conditions["NIFTY"]["tradability"] == "NOT_TRADABLE"


def test_classify_chop_bands():
    assert _classify_chop(None) == "UNKNOWN"
    assert _classify_chop(0.29) == "CHOPPY"
    assert _classify_chop(0.3) == "MIXED"
    assert _classify_chop(0.49) == "MIXED"
    assert _classify_chop(0.5) == "CLEAN"
    assert _classify_chop(0.95) == "CLEAN"


def test_chop_and_confidence_sub_scores_read_from_the_latest_row():
    db = _make_session()
    _seed_index(db)
    _seed_log(
        db, index_name="BANKNIFTY", regime="TREND", adx=28.4, cpr="NARROW", setups=[],
        decision="BUY_PE", chop_efficiency_ratio=0.22, confidence=0.78,
        setup_quality=82.0, entry_quality=79.0, risk_quality=76.0, market_alignment=74.0,
    )

    entry = get_market_conditions(db)[0]

    assert entry["chop_efficiency_ratio"] == 0.22
    assert entry["chop_label"] == "CHOPPY"
    assert entry["confidence"] == 0.78
    assert entry["setup_quality"] == 82.0
    assert entry["entry_quality"] == 79.0
    assert entry["risk_quality"] == 76.0
    assert entry["market_alignment"] == 74.0


def test_confidence_and_sub_scores_are_none_on_a_slot_occupied_marker_row():
    # A SLOT_OCCUPIED marker row (see the "Market Conditions panel froze"
    # fix) carries real context/chop data -- built every cycle regardless of
    # slot occupancy -- but no real decision, so confidence/sub-scores must
    # read None honestly rather than a stale value from a previous row.
    db = _make_session()
    _seed_index(db)
    _seed_log(
        db, index_name="BANKNIFTY", regime="TREND", adx=28.4, cpr="NARROW", setups=[],
        decision="SLOT_OCCUPIED", chop_efficiency_ratio=0.61,
        confidence=None, setup_quality=None, entry_quality=None, risk_quality=None, market_alignment=None,
    )

    entry = get_market_conditions(db)[0]

    assert entry["chop_efficiency_ratio"] == 0.61
    assert entry["chop_label"] == "CLEAN"
    assert entry["confidence"] is None
    assert entry["setup_quality"] is None


def test_new_fields_default_to_unknown_placeholder_when_no_log_exists_yet():
    db = _make_session()
    _seed_index(db)

    entry = get_market_conditions(db)[0]

    assert entry["chop_efficiency_ratio"] is None
    assert entry["chop_label"] == "UNKNOWN"
    assert entry["confidence"] is None
