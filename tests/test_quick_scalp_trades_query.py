from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.platform import get_quick_scalp_trades
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _trade(**overrides) -> StrategyTrade:
    fields = dict(
        trade_id="t-1", strategy_name="Quick Scalp - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=100.0, stoploss=97.0, target=105.0, entry_time=utc_now(),
        origin="QUICK_SCALP", status=TradeStatus.OPEN, result=TradeResult.OPEN,
        mode=TradingMode.PAPER,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_includes_both_open_and_closed_quick_scalp_trades():
    db = _make_session()
    db.add(_trade(trade_id="t-open", status=TradeStatus.OPEN))
    db.add(_trade(trade_id="t-closed", status=TradeStatus.CLOSED, result=TradeResult.WIN, pnl_percent=5.0))
    db.commit()

    trades = get_quick_scalp_trades(db)

    assert {t.trade_id for t in trades} == {"t-open", "t-closed"}


def test_excludes_trades_from_other_origins():
    db = _make_session()
    db.add(_trade(trade_id="t-scalp", origin="QUICK_SCALP"))
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.add(_trade(trade_id="t-autonomous", origin="AUTONOMOUS_AI"))
    db.add(_trade(trade_id="t-validated", origin="VALIDATED_SIGNAL"))
    db.commit()

    trades = get_quick_scalp_trades(db)

    assert [t.trade_id for t in trades] == ["t-scalp"]


def test_ordered_newest_first():
    db = _make_session()
    now = utc_now()
    db.add(_trade(trade_id="t-old", entry_time=now - timedelta(hours=2)))
    db.add(_trade(trade_id="t-new", entry_time=now))
    db.commit()

    trades = get_quick_scalp_trades(db)

    assert [t.trade_id for t in trades] == ["t-new", "t-old"]


def test_empty_when_no_quick_scalp_trades_exist():
    db = _make_session()
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.commit()

    assert get_quick_scalp_trades(db) == []
