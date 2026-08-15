from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import (
    AIReport,
    Base,
    ReportType,
    StrategyTrade,
    TradeResult,
    TradeStatus,
    TradingMode,
)
from app.reports import (
    _origination_trade_stats,
    _origination_trades_between,
    _provider_from_origin,
    generate_origination_summary,
)
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _trade(**overrides) -> StrategyTrade:
    fields = dict(
        trade_id="t-1",
        strategy_name="AI_ORIGIN",
        signal="BUY_CE",
        index_symbol="BANKNIFTY",
        tradingsymbol="BANKNIFTY28AUG2657800CE",
        symboltoken="123",
        strike=57800,
        expiry="28AUG2026",
        option_type="CE",
        quantity=35,
        entry_price=100.0,
        exit_price=110.0,
        stoploss=90.0,
        target=120.0,
        entry_time=utc_now(),
        exit_time=utc_now(),
        profit_loss=350.0,
        pnl_percent=10.0,
        result=TradeResult.WIN,
        status=TradeStatus.CLOSED,
        mode=TradingMode.PAPER,
        origin="AI_ORIGIN_CLAUDE",
        exit_reason="TARGET",
        ai_confidence=0.7,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_provider_from_origin():
    assert _provider_from_origin("AI_ORIGIN_CLAUDE") == "CLAUDE"
    assert _provider_from_origin("AI_ORIGIN_OPENAI") == "OPENAI"
    # Never mistaken for something it isn't (see CLAUDE.md's LIKE 'AI_ORIGIN_%%' rule).
    assert _provider_from_origin("SIGNAL") == "SIGNAL"


def test_origination_trades_between_excludes_signal_and_ai_alt():
    # CLAUDE.md: match with LIKE 'AI_ORIGIN_%%', never != 'SIGNAL' -- a loose
    # negation would also sweep in AI_ALT_* evaluation trades.
    db = _make_session()
    today = date.today()
    db.add(_trade(trade_id="t-origin", origin="AI_ORIGIN_CLAUDE"))
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.add(_trade(trade_id="t-alt", origin="AI_ALT_CLAUDE"))
    db.commit()

    trades = _origination_trades_between(db, today - timedelta(days=1), today + timedelta(days=1))

    assert [t.trade_id for t in trades] == ["t-origin"]


def test_origination_trades_between_excludes_open_trades():
    db = _make_session()
    today = date.today()
    db.add(_trade(trade_id="t-closed", origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED))
    db.add(_trade(trade_id="t-open", origin="AI_ORIGIN_CLAUDE", status=TradeStatus.OPEN, exit_time=None))
    db.commit()

    trades = _origination_trades_between(db, today - timedelta(days=1), today + timedelta(days=1))

    assert [t.trade_id for t in trades] == ["t-closed"]


def test_origination_trade_stats_empty():
    stats = _origination_trade_stats([])
    assert stats["total_trades"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["by_provider"] == {}
    assert stats["avg_confidence"] is None


def test_origination_trade_stats_buckets_by_provider_index_and_mode():
    trades = [
        _trade(
            trade_id="t-claude-win", origin="AI_ORIGIN_CLAUDE", index_symbol="BANKNIFTY",
            result=TradeResult.WIN, profit_loss=350.0, mode=TradingMode.PAPER, ai_confidence=0.8,
        ),
        _trade(
            trade_id="t-openai-loss", origin="AI_ORIGIN_OPENAI", index_symbol="NIFTY",
            result=TradeResult.LOSS, profit_loss=-150.0, mode=TradingMode.LIVE, ai_confidence=0.6,
            option_type="PE", exit_reason="STOPLOSS",
        ),
    ]

    stats = _origination_trade_stats(trades)

    assert stats["total_trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["net_pnl"] == 200.0
    assert stats["by_provider"]["CLAUDE"]["net_pnl"] == 350.0
    assert stats["by_provider"]["OPENAI"]["net_pnl"] == -150.0
    assert stats["by_index"]["BANKNIFTY"]["trades"] == 1
    assert stats["by_index"]["NIFTY"]["trades"] == 1
    assert stats["by_option_type"]["CE"]["trades"] == 1
    assert stats["by_option_type"]["PE"]["trades"] == 1
    assert stats["by_exit_reason"] == {"TARGET": 1, "STOPLOSS": 1}
    assert stats["by_mode"] == {TradingMode.PAPER: 1, TradingMode.LIVE: 1}
    assert stats["best_provider"] == "CLAUDE"
    assert stats["worst_provider"] == "OPENAI"
    assert stats["avg_confidence"] == 0.7


def test_origination_trade_stats_max_consecutive_losses():
    trades = [
        _trade(trade_id="t-1", result=TradeResult.LOSS),
        _trade(trade_id="t-2", result=TradeResult.LOSS),
        _trade(trade_id="t-3", result=TradeResult.WIN),
        _trade(trade_id="t-4", result=TradeResult.LOSS),
    ]

    stats = _origination_trade_stats(trades)

    assert stats["max_consecutive_losses"] == 2


def test_generate_origination_summary_saves_report_with_no_ai_provider_configured():
    db = _make_session()
    db.add(_trade(trade_id="t-1", origin="AI_ORIGIN_CLAUDE"))
    db.commit()

    report = generate_origination_summary(db, lookback_days=30)

    assert report.report_type == ReportType.ORIGINATION
    assert "AI Origination Summary" in report.title
    assert report.provider == "dummy"
    saved = db.query(AIReport).filter_by(id=report.id).one()
    assert saved.report_type == ReportType.ORIGINATION


def test_generate_origination_summary_excludes_signal_trades():
    db = _make_session()
    db.add(_trade(trade_id="t-origin", origin="AI_ORIGIN_CLAUDE", profit_loss=100.0))
    db.add(_trade(trade_id="t-signal", origin="SIGNAL", profit_loss=99999.0))
    db.commit()

    report = generate_origination_summary(db, lookback_days=30)

    assert "1" in report.summary_text  # total trades count line
    assert "99999" not in report.summary_text
