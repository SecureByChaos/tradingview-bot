from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeStatus
from app.platform import get_agentic_ai_trades
from app.time_utils import utc_now


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _seed_trade(db, **overrides):
    fields = dict(
        trade_id="t1", strategy_name="Agentic AI - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=120.0,
        stoploss=90.0, entry_time=utc_now(), origin="AGENTIC_AI",
    )
    fields.update(overrides)
    db.add(StrategyTrade(**fields))


def test_get_agentic_ai_trades_isolates_origin(tmp_path):
    db = _make_db(tmp_path)
    _seed_trade(db, trade_id="t1", origin="AGENTIC_AI")
    _seed_trade(db, trade_id="t2", origin="AUTONOMOUS_AI")
    _seed_trade(db, trade_id="t3", origin="QUICK_SCALP")
    db.commit()

    trades = get_agentic_ai_trades(db)
    assert [t.trade_id for t in trades] == ["t1"]


def test_get_agentic_ai_trades_includes_open_and_closed_newest_first(tmp_path):
    import datetime

    db = _make_db(tmp_path)
    _seed_trade(
        db, trade_id="older", origin="AGENTIC_AI", status=TradeStatus.CLOSED,
        entry_time=utc_now() - datetime.timedelta(hours=2),
    )
    _seed_trade(db, trade_id="newer", origin="AGENTIC_AI", status=TradeStatus.OPEN, entry_time=utc_now())
    db.commit()

    trades = get_agentic_ai_trades(db)
    assert [t.trade_id for t in trades] == ["newer", "older"]


def test_get_agentic_ai_trades_empty_when_no_trades_yet(tmp_path):
    db = _make_db(tmp_path)
    db.commit()
    assert get_agentic_ai_trades(db) == []
