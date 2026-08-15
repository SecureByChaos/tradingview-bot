from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeStatus
from app.platform import get_open_trades_with_ticks, get_today_activity
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _base_trade(**overrides) -> dict:
    fields = dict(
        trade_id="t-1",
        strategy_name="BNV7",
        signal="BUY_CE",
        index_symbol="BANKNIFTY",
        tradingsymbol="BANKNIFTY28AUG2657800CE",
        symboltoken="123",
        strike=57800,
        expiry="28AUG2026",
        option_type="CE",
        quantity=35,
        entry_price=100.0,
        stoploss=90.0,
        target=120.0,
        entry_time=utc_now(),
        origin="SIGNAL",
    )
    fields.update(overrides)
    return fields


def test_open_trades_with_ticks_includes_strike():
    db = _make_session()
    db.add(StrategyTrade(**_base_trade(status=TradeStatus.OPEN)))
    db.commit()

    trades = get_open_trades_with_ticks(db)

    assert len(trades) == 1
    assert trades[0]["strike"] == 57800


def test_today_activity_entry_message_includes_strike():
    db = _make_session()
    db.add(StrategyTrade(**_base_trade()))
    db.commit()

    activity = get_today_activity(db)

    assert len(activity) == 1
    assert "57800" in activity[0]["message"]
    assert activity[0]["message"] == "[BNV7] Entered Bank Nifty 57800 long call"


def test_today_activity_exit_message_includes_strike():
    db = _make_session()
    db.add(StrategyTrade(**_base_trade(
        trade_id="t-2",
        status=TradeStatus.CLOSED,
        exit_time=utc_now(),
        pnl_percent=-3.2,
    )))
    db.commit()

    activity = get_today_activity(db)

    # This trade entered AND closed today, so both events are reported --
    # only the "Closed" one is under test here.
    messages = [event["message"] for event in activity]
    assert "[BNV7] Closed Bank Nifty 57800 long call, -3.2%" in messages


def test_open_trades_with_ticks_includes_ai_origination_strike():
    # 15 Aug 2026: the AI Origination page is gone -- its open positions now
    # surface here instead, on the main dashboard's Active Trades.
    db = _make_session()
    db.add(StrategyTrade(**_base_trade(
        trade_id="t-3",
        origin="AI_ORIGIN_CLAUDE",
        status=TradeStatus.OPEN,
    )))
    db.commit()

    trades = get_open_trades_with_ticks(db)

    assert len(trades) == 1
    assert trades[0]["strike"] == 57800
