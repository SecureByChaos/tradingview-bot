from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, Signal
from app.multi_strategy import (
    _AGENTIC_SCRATCH_MFE_FLOOR_PERCENT,
    _AGENTIC_SCRATCH_WINDOW_MINUTES,
    MultiStrategyTradeManager,
)
from app.time_utils import IST

# 29 Oct 2026, a real Agentic AI Nifty BUY_CE never once showed a positive
# tick (MFE stayed below zero across its entire 34-minute hold) before
# running to its full -16.76% stop -- a "never confirmed" shape distinct
# from GIVEBACK_STOP's "gave back a real peak" shape. These tests pin the
# new deterministic "time stop"/"scratch rule" (app.multi_strategy's
# AGENTIC_SCRATCH_* constants) that targets exactly that case.


class _NullTelegram:
    def send(self, *args, **kwargs) -> None:
        pass


class _SequenceSmartAPI:
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
        smartapi_api_key="x", smartapi_client_id="x", smartapi_pin="x", smartapi_totp_secret="x",
    )


#
# SQLite doesn't round-trip tzinfo even on a DateTime(timezone=True) column
# (see CLAUDE.md's own documented gotcha, and the identical pattern in
# test_giveback_ratio_stop.py) -- a trade's entry_time comes back naive
# after a commit/reload, and to_ist() then treats those raw naive numbers
# as UTC and adds +5:30. So the STORED value (_ENTRY_TIME, 04:30) and the
# value the code actually computes elapsed time FROM (_ENTRY_TIME_IST,
# 10:00) are deliberately different objects -- every "now" passed to _run
# below must be built from _ENTRY_TIME_IST, not from _ENTRY_TIME itself,
# or the elapsed-minutes math silently comes out wrong.
_ENTRY_TIME = datetime(2026, 10, 29, 4, 30, tzinfo=IST)
_ENTRY_TIME_IST = datetime(2026, 10, 29, 10, 0, tzinfo=IST)


def _open_trade(
    db: Session, origin: str = "AGENTIC_AI", trade_id: str = "t1",
    entry_price: float = 100.0, stoploss: float = 85.0, target: float = 130.0,
    entry_time: datetime = _ENTRY_TIME,
) -> StrategyTrade:
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name="Agentic AI - Nifty 50", signal=Signal.BUY_CE.value,
        index_symbol="NIFTY", tradingsymbol="NIFTY29OCT26C22900", symboltoken="456",
        strike=22900, expiry="29OCT2026", option_type="CE", quantity=75,
        entry_price=entry_price, stoploss=stoploss, target=target,
        entry_time=entry_time,
        mode=TradingMode.PAPER, status=TradeStatus.OPEN, result=TradeResult.OPEN,
        origin=origin, sl_mode="FIXED", highest_price=entry_price, lowest_price=entry_price,
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


def _run(monkeypatch, db, now: datetime, prices: list[float]) -> StrategyTrade:
    _ist_now(monkeypatch, now)
    manager = MultiStrategyTradeManager(
        _make_settings(), _SequenceSmartAPI(prices), option_finder=None, telegram=_NullTelegram(),
    )
    manager.monitor_open_trades(db)
    return db.query(StrategyTrade).filter(StrategyTrade.trade_id == "t1").first()


def test_constants_are_the_reasoned_starting_points():
    assert _AGENTIC_SCRATCH_WINDOW_MINUTES == 15
    assert _AGENTIC_SCRATCH_MFE_FLOOR_PERCENT == 1.0


def test_fires_at_the_window_boundary_when_never_confirmed_and_negative(monkeypatch, db_session: Session) -> None:
    # Reproduces the real incident's shape: entry 100, never ticks above
    # 100.5 (MFE 0.5%, below the 1% floor), currently -5% at the 15-minute
    # mark -- must scratch, not ride the wider 85 stop.
    _open_trade(db_session, entry_price=100.0, stoploss=85.0)
    _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [100.5])
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=15), [95.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AGENTIC_SCRATCH_EXIT.value
    assert trade.exit_price == 95.0


def test_does_not_fire_before_the_window_elapses(monkeypatch, db_session: Session) -> None:
    _open_trade(db_session, entry_price=100.0, stoploss=85.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=10), [95.0])
    assert trade.status == TradeStatus.OPEN


def test_does_not_fire_once_it_had_a_real_favorable_tick(monkeypatch, db_session: Session) -> None:
    # MFE cleared the 1% floor (peaked at 103, +3%) before falling back
    # negative -- this is GIVEBACK_STOP's job, not the scratch rule's; the
    # scratch rule must leave it alone and the trade stays open (giveback
    # toggle is off by default, and the plain 85 stop hasn't been reached).
    _open_trade(db_session, entry_price=100.0, stoploss=85.0)
    _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [103.0])
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=20), [98.0])
    assert trade.status == TradeStatus.OPEN


def test_does_not_fire_while_still_non_negative(monkeypatch, db_session: Session) -> None:
    # Never confirmed (flat at entry), but not actually losing money --
    # this is a flat trade, not a losing one, and the rule is scoped to
    # negative P&L only.
    _open_trade(db_session, entry_price=100.0, stoploss=85.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=20), [100.0])
    assert trade.status == TradeStatus.OPEN


def test_does_not_apply_to_other_origins(monkeypatch, db_session: Session) -> None:
    for origin in ("QUICK_SCALP", "AUTONOMOUS_AI", "SIGNAL"):
        db_session.query(StrategyTrade).delete()
        db_session.commit()
        _open_trade(db_session, origin=origin, entry_price=100.0, stoploss=85.0)
        trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=20), [95.0])
        assert trade.status == TradeStatus.OPEN, f"scratch rule must not apply to {origin}"


def test_still_falls_through_to_plain_stoploss_when_actually_hit(monkeypatch, db_session: Session) -> None:
    # A trade that's already crossed its own real stop by the 15-minute
    # mark must still read STOPLOSS, not AGENTIC_SCRATCH_EXIT -- the plain
    # stop check runs earlier in the same decision chain and wins when both
    # conditions are technically true.
    _open_trade(db_session, entry_price=100.0, stoploss=90.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=20), [89.0])
    assert trade.exit_reason == ExitReason.STOPLOSS.value
