from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, Signal
from app.multi_strategy import (
    _AUTONOMOUS_NEVER_CONFIRMED_LOSS_PERCENT,
    _AUTONOMOUS_NEVER_CONFIRMED_MFE_FLOOR_PERCENT,
    MultiStrategyTradeManager,
)
from app.time_utils import IST

# 17 Nov 2026, recommendation #1 of the full-history loss analysis (CLAUDE.md,
# 15 Nov 2026): "Add a loss cap for trades that never confirm, checked every
# 30 seconds: exit at about -5% if the trade has never been up 1%." These
# tests pin the new AUTONOMOUS_AI-only deterministic check (app.multi_
# strategy's _AUTONOMOUS_NEVER_CONFIRMED_* constants) that implements that
# recommendation exactly as specified -- a LOSS floor with no elapsed-time
# requirement, distinct from AGENTIC_SCRATCH_EXIT's elapsed-TIME floor for
# the analogous AGENTIC_AI case (see tests/test_agentic_scratch_exit.py).


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


_ENTRY_TIME = datetime(2026, 11, 17, 4, 30, tzinfo=IST)
_ENTRY_TIME_IST = datetime(2026, 11, 17, 10, 0, tzinfo=IST)


def _open_trade(
    db: Session, origin: str = "AUTONOMOUS_AI", trade_id: str = "t1",
    entry_price: float = 100.0, stoploss: float = 50.0, target: float = 300.0,
    entry_time: datetime = _ENTRY_TIME,
) -> StrategyTrade:
    trade = StrategyTrade(
        trade_id=trade_id, strategy_name="Autonomous AI - Nifty 50", signal=Signal.BUY_CE.value,
        index_symbol="NIFTY", tradingsymbol="NIFTY17NOV26C22900", symboltoken="789",
        strike=22900, expiry="17NOV2026", option_type="CE", quantity=75,
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
    assert _AUTONOMOUS_NEVER_CONFIRMED_LOSS_PERCENT == 5.0
    assert _AUTONOMOUS_NEVER_CONFIRMED_MFE_FLOOR_PERCENT == 1.0


def test_fires_immediately_on_the_first_tick_no_time_window_required(monkeypatch, db_session: Session) -> None:
    # Unlike AGENTIC_SCRATCH_EXIT (15-minute window), this cap has no
    # elapsed-time requirement at all -- a single 30s tick straight to -5%
    # with no prior favorable move must close right there.
    _open_trade(db_session, entry_price=100.0, stoploss=50.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(seconds=30), [95.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AUTONOMOUS_NEVER_CONFIRMED_STOP.value
    assert trade.exit_price == 95.0


def test_does_not_fire_above_the_loss_floor(monkeypatch, db_session: Session) -> None:
    _open_trade(db_session, entry_price=100.0, stoploss=50.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [96.0])
    assert trade.status == TradeStatus.OPEN


def test_fires_at_the_exact_loss_floor_boundary(monkeypatch, db_session: Session) -> None:
    _open_trade(db_session, entry_price=100.0, stoploss=50.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [95.0])
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.AUTONOMOUS_NEVER_CONFIRMED_STOP.value


def test_does_not_fire_once_it_had_a_real_favorable_tick(monkeypatch, db_session: Session) -> None:
    # MFE cleared the 1% floor (peaked at 101.5, +1.5%) before falling back
    # to -5% -- this trade DID confirm at some point, so the cap must leave
    # it alone (GIVEBACK_STOP, if enabled, would be the mechanism for a real
    # pullback from a real peak, not this one).
    _open_trade(db_session, entry_price=100.0, stoploss=50.0)
    _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [101.5])
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=10), [95.0])
    assert trade.status == TradeStatus.OPEN


def test_does_not_fire_while_still_non_negative_or_only_mildly_negative(monkeypatch, db_session: Session) -> None:
    _open_trade(db_session, entry_price=100.0, stoploss=50.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [97.0])
    assert trade.status == TradeStatus.OPEN


def test_does_not_apply_to_other_origins(monkeypatch, db_session: Session) -> None:
    # Per the full-history analysis's own conclusion: robustly supported for
    # Autonomous AI at every floor 3-12% tested, but turns NEGATIVE for
    # Agentic AI at this same 5% floor (several of its real winners dipped
    # more than 5% before recovering) -- that analysis explicitly said
    # Agentic AI needs its own tick replay before this is trusted for it, so
    # it stays scoped to AUTONOMOUS_AI alone for now. Quick Scalp and SIGNAL
    # were never part of this analysis either.
    for origin in ("AGENTIC_AI", "QUICK_SCALP", "SIGNAL", "AI_ORIGIN_OPENAI", "VALIDATED_SIGNAL"):
        db_session.query(StrategyTrade).delete()
        db_session.commit()
        _open_trade(db_session, origin=origin, entry_price=100.0, stoploss=50.0)
        trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [95.0])
        assert trade.status == TradeStatus.OPEN, f"never-confirmed stop must not apply to {origin}"


def test_still_falls_through_to_plain_stoploss_when_actually_hit(monkeypatch, db_session: Session) -> None:
    # A trade whose own real stop is tighter than this cap (here, 90 vs the
    # -5% = 95 level) must still read STOPLOSS -- the plain stop check runs
    # earlier in the same decision chain and wins when both are technically
    # true at once.
    _open_trade(db_session, entry_price=100.0, stoploss=90.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [89.0])
    assert trade.exit_reason == ExitReason.STOPLOSS.value


def test_still_exits_at_target_when_reached_first(monkeypatch, db_session: Session) -> None:
    _open_trade(db_session, entry_price=100.0, stoploss=50.0, target=130.0)
    trade = _run(monkeypatch, db_session, _ENTRY_TIME_IST + timedelta(minutes=5), [131.0])
    assert trade.exit_reason == ExitReason.TARGET.value
