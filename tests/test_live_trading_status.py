from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, IndexConfig
from app.platform import get_live_trading_status


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


class _FakeSettings:
    def __init__(self, live_trading: bool) -> None:
        self.live_trading = live_trading


class _FakeSmartAPI:
    def __init__(self, live_trading: bool) -> None:
        self.settings = _FakeSettings(live_trading)


def _seed_index(db: Session, **overrides) -> None:
    fields = dict(
        symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True,
        spot_exchange="NSE", spot_symbol="Nifty Bank", spot_token="99926009",
        ai_origination_live_trade=False,
    )
    fields.update(overrides)
    db.add(IndexConfig(**fields))


def test_reports_server_flag_on():
    db = _make_session()
    status = get_live_trading_status(db, _FakeSmartAPI(live_trading=True))
    assert status["server_flag_on"] is True


def test_reports_server_flag_off():
    db = _make_session()
    status = get_live_trading_status(db, _FakeSmartAPI(live_trading=False))
    assert status["server_flag_on"] is False


def test_reports_per_index_live_status():
    db = _make_session()
    _seed_index(db, symbol="BANKNIFTY", display_name="Bank Nifty", ai_origination_live_trade=True)
    _seed_index(db, symbol="NIFTY", display_name="Nifty 50", spot_token="99926000", ai_origination_live_trade=False)
    db.commit()

    status = get_live_trading_status(db, _FakeSmartAPI(live_trading=True))

    by_symbol = {row["symbol"]: row["live"] for row in status["indices"]}
    assert by_symbol == {"BANKNIFTY": True, "NIFTY": False}


def test_excludes_disabled_indices():
    db = _make_session()
    _seed_index(db, symbol="BANKNIFTY", enabled=False, ai_origination_live_trade=True)
    db.commit()

    status = get_live_trading_status(db, _FakeSmartAPI(live_trading=True))

    assert status["indices"] == []


def test_missing_smartapi_settings_defaults_to_flag_off():
    db = _make_session()
    status = get_live_trading_status(db, smartapi=object())
    assert status["server_flag_on"] is False
