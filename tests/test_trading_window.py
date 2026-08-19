from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.multi_strategy as multi_strategy_module
from app.ai.originator import _past_trading_end, _still_observing
from app.config import Settings
from app.dashboard_routes import _parse_hhmm_strict, apply_settings, update_settings_page
from app.db_models import (
    Base,
    IndexConfig,
    IndexSymbol,
    PlatformSettings,
    StrategyConfig,
    StrategyTrade,
    TradeResult,
    TradeStatus,
    TradingMode,
)
from app.models import Signal
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST, parse_hhmm


# ---------------------------------------------------------------------------
# time_utils.parse_hhmm
# ---------------------------------------------------------------------------


def test_parse_hhmm_valid() -> None:
    assert parse_hhmm("09:45", (0, 0)) == (9, 45)
    assert parse_hhmm("15:15", (0, 0)) == (15, 15)


@pytest.mark.parametrize("bad", [None, "", "not-a-time", "25:00", "10:99", "10"])
def test_parse_hhmm_falls_back_on_malformed_input(bad) -> None:
    assert parse_hhmm(bad, (9, 45)) == (9, 45)


# ---------------------------------------------------------------------------
# dashboard_routes: Settings > General validation
# ---------------------------------------------------------------------------


def test_parse_hhmm_strict_rejects_malformed() -> None:
    assert _parse_hhmm_strict("not-a-time") is None
    assert _parse_hhmm_strict("25:00") is None
    assert _parse_hhmm_strict("09:45") == (9, 45)


@pytest.fixture()
def settings_db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_update_settings_page_rejects_start_after_close(settings_db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        update_settings_page(db=settings_db_session, square_off_time="10:00", trading_start_time="11:00")
    assert exc_info.value.status_code == 400


def test_update_settings_page_rejects_close_later_than_1515(settings_db_session: Session) -> None:
    # 15:15 is the ceiling because the daily-square-off safety-net cron is
    # still fixed at 15:15 -- a later configured close would let the cron
    # force-close trades early.
    with pytest.raises(HTTPException):
        update_settings_page(db=settings_db_session, square_off_time="15:30", trading_start_time="09:45")


def test_update_settings_page_rejects_start_before_market_open(settings_db_session: Session) -> None:
    with pytest.raises(HTTPException):
        update_settings_page(db=settings_db_session, square_off_time="15:15", trading_start_time="09:00")


def test_update_settings_page_accepts_valid_window_and_persists(settings_db_session: Session) -> None:
    response = update_settings_page(db=settings_db_session, square_off_time="14:30", trading_start_time="10:00")
    assert response.status_code == 303
    settings_db_session.commit()
    row = settings_db_session.get(PlatformSettings, 1)
    assert row.trading_start_time == "10:00"
    assert row.square_off_time == "14:30"


def test_apply_settings_sets_trading_start_time() -> None:
    settings = PlatformSettings(id=1)
    apply_settings(settings, "14:30", "10:00", "token", "chat")
    assert settings.trading_start_time == "10:00"
    assert settings.square_off_time == "14:30"


# ---------------------------------------------------------------------------
# app.ai.originator: _still_observing / _past_trading_end now take explicit
# (hour, minute) tuples instead of reading hardcoded module constants.
# ---------------------------------------------------------------------------


def _ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_still_observing_honours_explicit_start() -> None:
    assert _still_observing(_ist(2026, 8, 19, 9, 30), (9, 45)) is True
    assert _still_observing(_ist(2026, 8, 19, 9, 45), (9, 45)) is False
    # A custom, later-than-default start is honoured too.
    assert _still_observing(_ist(2026, 8, 19, 10, 0), (10, 15)) is True


def test_past_trading_end_honours_explicit_end() -> None:
    assert _past_trading_end(_ist(2026, 8, 19, 15, 14), (15, 15)) is False
    assert _past_trading_end(_ist(2026, 8, 19, 15, 15), (15, 15)) is True
    # A custom, earlier-than-default close is honoured too.
    assert _past_trading_end(_ist(2026, 8, 19, 14, 5), (14, 0)) is True


# ---------------------------------------------------------------------------
# app.multi_strategy: entry-time gate + dynamic TIME_EXIT
# ---------------------------------------------------------------------------


class _StopProbe(Exception):
    pass


class _RecordingOptionFinder:
    def __init__(self) -> None:
        self.calls = 0

    def find_atm_contract(self, signal, index=None, expiry_itm_strikes=0, min_dte=0):
        self.calls += 1
        raise _StopProbe()


class _NullSmartAPI:
    def get_ltp(self, *args, **kwargs):
        raise AssertionError("get_ltp should never be called once the trading-window gate rejects entry")


class _NullTelegram:
    def send(self, *args, **kwargs) -> None:
        pass


class _FixedDateTime(datetime):
    _fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


@pytest.fixture()
def strategy_db_session():
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


def _seed_window(db: Session, start: str, close: str) -> None:
    db.add(PlatformSettings(id=1, trading_start_time=start, square_off_time=close))
    db.commit()


def _set_now(monkeypatch, when: datetime) -> None:
    _FixedDateTime._fixed = when
    monkeypatch.setattr(multi_strategy_module, "datetime", _FixedDateTime)


def test_handle_signal_rejects_entry_before_trading_start(monkeypatch, strategy_db_session: Session) -> None:
    _seed_index(strategy_db_session)
    _seed_strategy(strategy_db_session, "BNV7")
    _seed_window(strategy_db_session, "09:45", "15:15")
    _set_now(monkeypatch, _ist(2026, 8, 19, 9, 20))
    finder = _RecordingOptionFinder()
    manager = MultiStrategyTradeManager(_make_settings(), _NullSmartAPI(), finder, _NullTelegram())

    response = manager.handle_signal(strategy_db_session, "BNV7", Signal.BUY_CE)

    assert response.accepted is False
    assert "trading window" in response.message
    assert finder.calls == 0


def test_handle_signal_rejects_entry_at_or_after_close(monkeypatch, strategy_db_session: Session) -> None:
    _seed_index(strategy_db_session)
    _seed_strategy(strategy_db_session, "BNV7")
    _seed_window(strategy_db_session, "09:45", "15:15")
    _set_now(monkeypatch, _ist(2026, 8, 19, 15, 15))
    finder = _RecordingOptionFinder()
    manager = MultiStrategyTradeManager(_make_settings(), _NullSmartAPI(), finder, _NullTelegram())

    response = manager.handle_signal(strategy_db_session, "BNV7", Signal.BUY_PE)

    assert response.accepted is False
    assert finder.calls == 0


def test_handle_signal_allows_entry_inside_configured_window(monkeypatch, strategy_db_session: Session) -> None:
    _seed_index(strategy_db_session)
    _seed_strategy(strategy_db_session, "BNV7")
    # A custom, non-default window -- proves this reads the DB setting, not
    # the hardcoded fallback.
    _seed_window(strategy_db_session, "10:00", "14:00")
    _set_now(monkeypatch, _ist(2026, 8, 19, 12, 0))
    finder = _RecordingOptionFinder()
    manager = MultiStrategyTradeManager(_make_settings(), _NullSmartAPI(), finder, _NullTelegram())

    with pytest.raises(_StopProbe):
        manager.handle_signal(strategy_db_session, "BNV7", Signal.BUY_CE)

    assert finder.calls == 1


def test_handle_signal_uses_fallback_window_when_settings_row_missing(monkeypatch, strategy_db_session: Session) -> None:
    # No PlatformSettings row seeded at all -- get_or_create_settings creates
    # one with the ORM defaults (09:45/15:15), matching this app's previous
    # hardcoded behaviour.
    _seed_index(strategy_db_session)
    _seed_strategy(strategy_db_session, "BNV7")
    _set_now(monkeypatch, _ist(2026, 8, 19, 9, 30))
    finder = _RecordingOptionFinder()
    manager = MultiStrategyTradeManager(_make_settings(), _NullSmartAPI(), finder, _NullTelegram())

    response = manager.handle_signal(strategy_db_session, "BNV7", Signal.BUY_CE)

    assert response.accepted is False
    assert finder.calls == 0


def _open_trade(db: Session, strategy_name: str = "BNV7") -> StrategyTrade:
    trade = StrategyTrade(
        trade_id="t1",
        strategy_name=strategy_name,
        signal=Signal.BUY_CE.value,
        index_symbol=IndexSymbol.NIFTY,
        tradingsymbol="NIFTY19AUG26C24000",
        symboltoken="123",
        strike=24000,
        expiry="19AUG2026",
        option_type="CE",
        quantity=75,
        entry_price=100.0,
        stoploss=90.0,
        target=120.0,
        entry_time=datetime(2026, 8, 19, 4, 0, tzinfo=IST),  # ~09:30 IST
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        highest_price=100.0,
        lowest_price=100.0,
    )
    db.add(trade)
    db.commit()
    return trade


class _FlatSmartAPI:
    def get_ltp(self, *args, **kwargs) -> float:
        return 100.0  # exactly flat -- neither STOPLOSS nor TARGET fires


def test_monitor_open_trades_honours_configured_square_off_time(monkeypatch, strategy_db_session: Session) -> None:
    _seed_index(strategy_db_session)
    _seed_strategy(strategy_db_session, "BNV7")
    # Configured close well before the old hardcoded 15:15 -- if the dynamic
    # check weren't wired in, this trade would stay open at 14:05.
    _seed_window(strategy_db_session, "09:45", "14:00")
    _open_trade(strategy_db_session)
    _set_now(monkeypatch, _ist(2026, 8, 19, 14, 5))
    manager = MultiStrategyTradeManager(_make_settings(), _FlatSmartAPI(), _RecordingOptionFinder(), _NullTelegram())

    closed = manager.monitor_open_trades(strategy_db_session)

    assert len(closed) == 1
    assert closed[0].exit_reason == "TIME_EXIT"


def test_monitor_open_trades_keeps_trade_open_before_configured_close(monkeypatch, strategy_db_session: Session) -> None:
    _seed_index(strategy_db_session)
    _seed_strategy(strategy_db_session, "BNV7")
    _seed_window(strategy_db_session, "09:45", "14:00")
    _open_trade(strategy_db_session)
    _set_now(monkeypatch, _ist(2026, 8, 19, 13, 0))
    manager = MultiStrategyTradeManager(_make_settings(), _FlatSmartAPI(), _RecordingOptionFinder(), _NullTelegram())

    closed = manager.monitor_open_trades(strategy_db_session)

    assert closed == []
