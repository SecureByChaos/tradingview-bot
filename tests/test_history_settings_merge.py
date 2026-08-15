from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.platform import compute_performance_kpis, signal_strategy_names, strategy_trades_query_for_filter
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _trade(**overrides) -> StrategyTrade:
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
        status=TradeStatus.OPEN,
        mode=TradingMode.PAPER,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_ai_alt_origin_value_no_longer_filters():
    # 15 Aug 2026: "AI Alternatives" removed from Trade History -- origin="ai_alt"
    # is no longer a recognized filter and must fall through to "no filter",
    # not silently filter to nothing.
    db = _make_session()
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.add(_trade(trade_id="t-alt", origin="AI_ALT_CLAUDE"))
    db.commit()

    trades = list(db.scalars(strategy_trades_query_for_filter("today", None, None, "ai_alt")))

    assert {t.trade_id for t in trades} == {"t-signal", "t-alt"}


def test_strategy_name_filter_applies_at_query_level():
    db = _make_session()
    db.add(_trade(trade_id="t-bnv7", origin="SIGNAL", strategy_name="BNV7"))
    db.add(_trade(trade_id="t-nv1", origin="SIGNAL", strategy_name="NV1"))
    db.commit()

    trades = list(db.scalars(strategy_trades_query_for_filter("today", None, None, "signal", "BNV7")))

    assert [t.trade_id for t in trades] == ["t-bnv7"]


def test_strategy_and_origin_filters_combine():
    db = _make_session()
    db.add(_trade(trade_id="t-match", origin="SIGNAL", strategy_name="BNV7"))
    db.add(_trade(trade_id="t-wrong-strategy", origin="SIGNAL", strategy_name="NV1"))
    db.add(_trade(trade_id="t-wrong-origin", origin="AI_ORIGIN_CLAUDE", strategy_name="BNV7"))
    db.commit()

    trades = list(db.scalars(strategy_trades_query_for_filter("today", None, None, "signal", "BNV7")))

    assert [t.trade_id for t in trades] == ["t-match"]


def test_signal_strategy_names_excludes_ai_trades():
    db = _make_session()
    db.add(_trade(trade_id="t-1", origin="SIGNAL", strategy_name="BNV7"))
    db.add(_trade(trade_id="t-2", origin="SIGNAL", strategy_name="NV1"))
    db.add(_trade(trade_id="t-3", origin="AI_ORIGIN_CLAUDE", strategy_name="AI_ORIGIN"))
    db.add(_trade(trade_id="t-4", origin="AI_ALT_CLAUDE", strategy_name="AI_ALT"))
    db.commit()

    names = signal_strategy_names(db)

    assert names == ["BNV7", "NV1"]


def test_compute_performance_kpis_empty():
    kpis = compute_performance_kpis([])
    assert kpis["kpis"]["total_trades"] == 0
    assert kpis["kpis"]["win_rate"] == 0.0
    assert kpis["equity_curve"] == []
    assert kpis["daily_pnl"] == []


def test_compute_performance_kpis_computes_win_rate_and_drawdown():
    now = utc_now()
    closed = [
        _trade(
            trade_id="t-win", status=TradeStatus.CLOSED, result=TradeResult.WIN,
            profit_loss=500.0, pnl_percent=10.0, entry_time=now - timedelta(days=1), exit_time=now - timedelta(days=1),
        ),
        _trade(
            trade_id="t-loss", status=TradeStatus.CLOSED, result=TradeResult.LOSS,
            profit_loss=-200.0, pnl_percent=-4.0, entry_time=now, exit_time=now,
        ),
    ]

    result = compute_performance_kpis(closed)

    assert result["kpis"]["total_trades"] == 2
    assert result["kpis"]["win_rate"] == 50.0
    assert result["kpis"]["net_pnl_amount"] == 300.0
    assert result["win_loss"] == {"wins": 1, "losses": 1}
    assert len(result["equity_curve"]) == 2
