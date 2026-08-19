from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.multi_strategy as multi_strategy_module
from app.config import Settings
from app.db_models import Base, IndexConfig, IndexSymbol, StrategyConfig
from app.models import Signal
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST


class _StopProbe(Exception):
    """Raised by the fake option finder once it has recorded its call args,
    so handle_signal never has to be driven any further (no real contract,
    price, or DB write needed to answer "what min_dte was passed")."""


class _RecordingOptionFinder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def find_atm_contract(self, signal, index=None, expiry_itm_strikes=0, min_dte=0):
        self.calls.append((signal, expiry_itm_strikes, min_dte))
        raise _StopProbe()


class _NullSmartAPI:
    pass


class _NullTelegram:
    pass


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_settings() -> Settings:
    return Settings(
        smartapi_api_key="x",
        smartapi_client_id="x",
        smartapi_pin="x",
        smartapi_totp_secret="x",
    )


def _seed_index(db: Session) -> None:
    db.add(IndexConfig(symbol=IndexSymbol.NIFTY, enabled=True, strike_interval=50))
    db.commit()


def _seed_strategy(db: Session, name: str) -> None:
    db.add(StrategyConfig(name=name, enabled=True, index_symbol=IndexSymbol.NIFTY))
    db.commit()


class _FixedDateTime(datetime):
    """19 Aug 2026: handle_signal gained a trading-window gate (Settings >
    General) checked before find_atm_contract -- these tests need wall-clock
    time frozen inside the default 09:45-15:15 IST window so they exercise
    the DTE floor regardless of when the suite actually runs."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 19, 11, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def _freeze_inside_trading_window(monkeypatch):
    monkeypatch.setattr(multi_strategy_module, "datetime", _FixedDateTime)


def test_nv1_entry_passes_the_dte_floor(db_session: Session) -> None:
    _seed_index(db_session)
    _seed_strategy(db_session, "NV1")
    finder = _RecordingOptionFinder()
    manager = MultiStrategyTradeManager(_make_settings(), _NullSmartAPI(), finder, _NullTelegram())

    with pytest.raises(_StopProbe):
        manager.handle_signal(db_session, "NV1", Signal.BUY_PE)

    assert len(finder.calls) == 1
    _, _, min_dte = finder.calls[0]
    assert min_dte == 1


def test_other_strategies_are_not_given_a_dte_floor(db_session: Session) -> None:
    _seed_index(db_session)
    _seed_strategy(db_session, "BNV7")
    finder = _RecordingOptionFinder()
    manager = MultiStrategyTradeManager(_make_settings(), _NullSmartAPI(), finder, _NullTelegram())

    with pytest.raises(_StopProbe):
        manager.handle_signal(db_session, "BNV7", Signal.BUY_CE)

    assert len(finder.calls) == 1
    _, _, min_dte = finder.calls[0]
    assert min_dte == 0


def test_nv1_name_match_is_exact_not_a_prefix(db_session: Session) -> None:
    # A strategy merely named similarly (e.g. "NV1B") must not accidentally
    # inherit the floor -- the check is strategy.name == "NV1", not a prefix.
    _seed_index(db_session)
    _seed_strategy(db_session, "NV1B")
    finder = _RecordingOptionFinder()
    manager = MultiStrategyTradeManager(_make_settings(), _NullSmartAPI(), finder, _NullTelegram())

    with pytest.raises(_StopProbe):
        manager.handle_signal(db_session, "NV1B", Signal.BUY_PE)

    assert len(finder.calls) == 1
    _, _, min_dte = finder.calls[0]
    assert min_dte == 0
