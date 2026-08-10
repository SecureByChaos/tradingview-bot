from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, IndexConfig
from app.market_context import ADX_NO_TREND, ADX_TRENDING
from app.platform import _classify_tradability, get_market_conditions
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _seed_index(db: Session, symbol: str = "BANKNIFTY", enabled: bool = True) -> None:
    db.add(IndexConfig(symbol=symbol, display_name="Bank Nifty", enabled=enabled))
    db.commit()


def _seed_log(db: Session, *, index_name: str, regime: str, adx: float | None, cpr: str | None,
              setups: list[str], data_stale: bool = False, minutes_ago: float = 1.0) -> None:
    db.add(
        AIOriginationLog(
            timestamp=utc_now() - timedelta(minutes=minutes_ago),
            index_name=index_name,
            provider="claude",
            provider_role="primary",
            decision="NONE",
            regime=regime,
            adx=adx,
            cpr=cpr,
            setups=json.dumps(setups),
            data_stale=data_stale,
            context_json="{}",
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
