from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db_models import (
    Base,
    StrategyTrade,
    TradeResult,
    TradeStatus,
    TradingMode,
)
from app.models import Signal
from app.multi_strategy import MultiStrategyTradeManager
from app.time_utils import IST


class _NullTelegram:
    def send(self, *args, **kwargs) -> None:
        pass


class _SequenceSmartAPI:
    """Returns successive LTP values from a fixed sequence, one per
    monitor_open_trades call, so a trade's premium path can be scripted
    across several ticks."""

    def __init__(self, prices: list[float]) -> None:
        self._prices = list(prices)

    def get_ltp(self, *args, **kwargs) -> float:
        return self._prices.pop(0)


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


def _open_trade(db: Session, signal: Signal = Signal.BUY_CE, trade_id: str = "t1") -> StrategyTrade:
    trade = StrategyTrade(
        trade_id=trade_id,
        strategy_name="BNV7",
        signal=signal.value,
        index_symbol="NIFTY",
        tradingsymbol="NIFTY19AUG26C24000",
        symboltoken="123",
        strike=24000,
        expiry="19AUG2026",
        option_type="CE" if signal in (Signal.BUY_CE, Signal.SELL_CE) else "PE",
        quantity=75,
        entry_price=100.0,
        stoploss=50.0,   # wide enough that noise in this test never trips it
        target=200.0,
        entry_time=datetime(2026, 8, 19, 4, 0, tzinfo=IST),  # ~09:30 IST, inside the default trading window
        mode=TradingMode.PAPER,
        status=TradeStatus.OPEN,
        result=TradeResult.OPEN,
        highest_price=100.0,
        lowest_price=100.0,
    )
    db.add(trade)
    db.commit()
    return trade


def _ist_now(monkeypatch, when: datetime) -> None:
    import app.multi_strategy as multi_strategy_module

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(multi_strategy_module, "datetime", _FixedDateTime)


def test_lowest_price_tracks_a_real_running_low_on_a_long_trade(monkeypatch, db_session: Session) -> None:
    # 24 Aug 2026 real-data report: lowest_price stayed pinned at entry_price
    # for every long trade (every AI Origination trade, and every rule-based
    # strategy entry -- SELL_* never opens a position). Root cause: the only
    # update to lowest_price lived in the is_short branch, which is
    # structurally unreachable for a long-only trade population.
    _open_trade(db_session)
    _ist_now(monkeypatch, datetime(2026, 8, 19, 10, 0, tzinfo=IST))
    manager = MultiStrategyTradeManager(
        _make_settings(), _SequenceSmartAPI([90.0]), option_finder=None, telegram=_NullTelegram(),
    )

    manager.monitor_open_trades(db_session)

    trade = db_session.get(StrategyTrade, db_session.query(StrategyTrade).first().id)
    assert trade.lowest_price == 90.0


def test_lowest_price_keeps_the_minimum_across_multiple_ticks(monkeypatch, db_session: Session) -> None:
    _open_trade(db_session)
    _ist_now(monkeypatch, datetime(2026, 8, 19, 10, 0, tzinfo=IST))
    manager = MultiStrategyTradeManager(
        _make_settings(), _SequenceSmartAPI([95.0]), option_finder=None, telegram=_NullTelegram(),
    )
    manager.monitor_open_trades(db_session)

    manager.smartapi = _SequenceSmartAPI([85.0])
    manager.monitor_open_trades(db_session)

    manager.smartapi = _SequenceSmartAPI([92.0])  # bounces back up
    manager.monitor_open_trades(db_session)

    trade = db_session.query(StrategyTrade).first()
    assert trade.lowest_price == 85.0  # the lowest point reached, not the latest price


def test_highest_price_still_tracks_correctly_alongside_the_fix(monkeypatch, db_session: Session) -> None:
    # Regression check -- the existing, already-correct highest_price logic
    # must be unaffected by adding the lowest_price mirror next to it.
    _open_trade(db_session)
    _ist_now(monkeypatch, datetime(2026, 8, 19, 10, 0, tzinfo=IST))
    manager = MultiStrategyTradeManager(
        _make_settings(), _SequenceSmartAPI([110.0]), option_finder=None, telegram=_NullTelegram(),
    )

    manager.monitor_open_trades(db_session)

    trade = db_session.query(StrategyTrade).first()
    assert trade.highest_price == 110.0
    assert trade.lowest_price == 100.0  # never went below entry, so stays at entry


def test_lowest_price_never_recorded_above_entry_when_premium_only_rises(monkeypatch, db_session: Session) -> None:
    _open_trade(db_session)
    _ist_now(monkeypatch, datetime(2026, 8, 19, 10, 0, tzinfo=IST))
    manager = MultiStrategyTradeManager(
        _make_settings(), _SequenceSmartAPI([105.0]), option_finder=None, telegram=_NullTelegram(),
    )
    manager.monitor_open_trades(db_session)

    manager.smartapi = _SequenceSmartAPI([115.0])
    manager.monitor_open_trades(db_session)

    trade = db_session.query(StrategyTrade).first()
    assert trade.lowest_price == 100.0
    assert trade.highest_price == 115.0
