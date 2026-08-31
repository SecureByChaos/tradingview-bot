from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.platform import get_autonomous_ai_trades
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _trade(**overrides) -> StrategyTrade:
    fields = dict(
        trade_id="t-1", strategy_name="Autonomous AI - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=100.0, stoploss=65.0, target=150.0, entry_time=utc_now(),
        origin="AUTONOMOUS_AI", status=TradeStatus.OPEN, result=TradeResult.OPEN,
        mode=TradingMode.PAPER,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_includes_both_open_and_closed_autonomous_ai_trades():
    db = _make_session()
    db.add(_trade(trade_id="t-open", status=TradeStatus.OPEN))
    db.add(_trade(trade_id="t-closed", status=TradeStatus.CLOSED, result=TradeResult.WIN, pnl_percent=12.0))
    db.commit()

    trades = get_autonomous_ai_trades(db)

    assert {t.trade_id for t in trades} == {"t-open", "t-closed"}


def test_excludes_trades_from_other_origins():
    db = _make_session()
    db.add(_trade(trade_id="t-auto", origin="AUTONOMOUS_AI"))
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.add(_trade(trade_id="t-ai", origin="AI_ORIGIN_OPENAI"))
    db.add(_trade(trade_id="t-validated", origin="VALIDATED_SIGNAL"))
    db.commit()

    trades = get_autonomous_ai_trades(db)

    assert [t.trade_id for t in trades] == ["t-auto"]


def test_ordered_newest_first():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t-old", entry_time=now - timedelta(hours=2)))
    db.add(_trade(trade_id="t-new", entry_time=now))
    db.commit()

    trades = get_autonomous_ai_trades(db)

    assert [t.trade_id for t in trades] == ["t-new", "t-old"]


def test_empty_when_no_autonomous_ai_trades_exist():
    db = _make_session()
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.commit()

    assert get_autonomous_ai_trades(db) == []
