from __future__ import annotations

import json
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.reports import (
    _template_narrative,
    _template_pattern_narrative,
    generate_daily_summary,
    generate_monthly_report,
    generate_pattern_discovery,
    generate_weekly_report,
)
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
        tradingsymbol="X",
        symboltoken="1",
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
        origin="SIGNAL",
        exit_reason="TARGET",
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_generate_daily_summary_includes_origination_stats_alongside_signal_stats():
    db = _make_session()
    db.add(_trade(trade_id="s-1", origin="SIGNAL", profit_loss=100.0, result=TradeResult.WIN))
    db.add(_trade(
        trade_id="o-1", origin="AI_ORIGIN_OPENAI", strategy_name="AI Origination - Bank Nifty",
        profit_loss=-50.0, pnl_percent=-5.0, result=TradeResult.LOSS,
    ))
    db.commit()

    report = generate_daily_summary(db, report_date=date.today())

    stats = json.loads(report.stats_json)
    assert stats["total_trades"] == 1  # SIGNAL population, unaffected by the addition
    assert "origination_stats" in stats
    assert stats["origination_stats"]["total_trades"] == 1
    assert stats["origination_stats"]["losses"] == 1


def test_generate_daily_summary_reports_zero_origination_trades_correctly():
    db = _make_session()
    db.add(_trade(trade_id="s-1", origin="SIGNAL"))
    db.commit()

    report = generate_daily_summary(db, report_date=date.today())

    stats = json.loads(report.stats_json)
    assert stats["origination_stats"]["total_trades"] == 0


def test_generate_daily_summary_includes_origination_stats_even_with_zero_signal_trades():
    # Regression: the template-fallback narrative used to return early when
    # there were no SIGNAL trades, which would have silently dropped any AI
    # Origination summary for a day with AI trades but no signal trades.
    db = _make_session()
    db.add(_trade(
        trade_id="o-1", origin="AI_ORIGIN_CLAUDE", strategy_name="AI Origination - Nifty 50",
        profit_loss=75.0, pnl_percent=7.5, result=TradeResult.WIN,
    ))
    db.commit()

    report = generate_daily_summary(db, report_date=date.today())

    stats = json.loads(report.stats_json)
    assert stats["total_trades"] == 0
    assert stats["origination_stats"]["total_trades"] == 1
    assert "AI Origination: 1 trades" in report.summary_text


def test_template_narrative_mentions_origination_when_present_and_populated():
    stats = {
        "total_trades": 2, "wins": 1, "losses": 1, "win_rate": 50.0, "net_pnl": 50.0,
        "origination_stats": {
            "total_trades": 3, "wins": 2, "losses": 1, "win_rate": 66.67, "net_pnl": 120.0,
            "best_provider": "OPENAI",
            "by_provider": {"OPENAI": {"net_pnl": 120.0, "win_rate": 66.67}},
        },
    }

    text = _template_narrative("daily summary", "25 Aug 2026", stats)

    assert "AI Origination: 3 trades" in text
    assert "Best AI Origination provider: OPENAI" in text


def test_template_narrative_reports_no_origination_trades_when_block_present_but_empty():
    stats = {
        "total_trades": 1, "wins": 1, "losses": 0, "win_rate": 100.0, "net_pnl": 50.0,
        "origination_stats": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0},
    }

    text = _template_narrative("daily summary", "25 Aug 2026", stats)

    assert "No closed AI Origination trades were recorded in this period." in text


def test_template_narrative_omits_origination_section_when_key_absent():
    # weekly/monthly reports don't populate origination_stats -- confirms
    # this addition is a true no-op for them, not just an empty section.
    stats = {"total_trades": 1, "wins": 1, "losses": 0, "win_rate": 100.0, "net_pnl": 50.0}

    text = _template_narrative("weekly report", "25 Aug 2026", stats)

    assert "AI Origination" not in text


def test_template_narrative_dispatch_is_not_confused_by_origination_stats_key():
    # origination_stats' OWN inner dict has a by_provider key, but that must
    # not leak to the top level and misroute this into
    # _template_origination_narrative (which would print "AI Origination
    # Summary for ..." as the report's own title, wrong for a daily report).
    stats = {
        "total_trades": 1, "wins": 1, "losses": 0, "win_rate": 100.0, "net_pnl": 50.0,
        "origination_stats": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0},
    }

    text = _template_narrative("daily summary", "25 Aug 2026", stats)

    assert text.startswith("Daily Summary for 25 Aug 2026.")


# ---------------------------------------------------------------------------
# 25 Aug 2026: "make ai origination part of every summary" -- extended from
# Daily-only to Weekly, Monthly and Pattern Discovery too.
# ---------------------------------------------------------------------------

def test_generate_weekly_report_includes_origination_stats():
    db = _make_session()
    today = date.today()
    db.add(_trade(trade_id="s-1", origin="SIGNAL", entry_time=utc_now(), exit_time=utc_now()))
    db.add(_trade(
        trade_id="o-1", origin="AI_ORIGIN_OPENAI", strategy_name="AI Origination - Bank Nifty",
        profit_loss=25.0, pnl_percent=2.5, result=TradeResult.WIN,
        entry_time=utc_now(), exit_time=utc_now(),
    ))
    db.commit()

    report = generate_weekly_report(db, reference=today)

    stats = json.loads(report.stats_json)
    assert stats["origination_stats"]["total_trades"] == 1


def test_generate_monthly_report_includes_origination_stats():
    db = _make_session()
    today = date.today()
    db.add(_trade(
        trade_id="o-1", origin="AI_ORIGIN_CLAUDE", strategy_name="AI Origination - Nifty 50",
        profit_loss=-15.0, pnl_percent=-1.5, result=TradeResult.LOSS,
        entry_time=utc_now(), exit_time=utc_now(),
    ))
    db.commit()

    report = generate_monthly_report(db, reference=today)

    stats = json.loads(report.stats_json)
    assert stats["origination_stats"]["total_trades"] == 1
    assert stats["origination_stats"]["losses"] == 1


def test_generate_pattern_discovery_includes_origination_stats():
    db = _make_session()
    db.add(_trade(trade_id="s-1", origin="SIGNAL", entry_time=utc_now(), exit_time=utc_now()))
    db.add(_trade(
        trade_id="o-1", origin="AI_ORIGIN_OPENAI", strategy_name="AI Origination - Nifty 50",
        profit_loss=40.0, pnl_percent=4.0, result=TradeResult.WIN,
        entry_time=utc_now(), exit_time=utc_now(),
    ))
    db.commit()

    report = generate_pattern_discovery(db, lookback_days=90)

    stats = json.loads(report.stats_json)
    assert "origination_stats" in stats
    assert stats["origination_stats"]["total_trades"] == 1
    assert "trade_stats" in stats  # confirms the existing nested shape is unchanged


def test_pattern_narrative_includes_origination_even_with_zero_signal_trades():
    # Same early-return bug class fixed in _template_narrative's default
    # branch also existed in _template_pattern_narrative -- confirm it's
    # fixed here too.
    stats = {
        "trade_stats": {"total_trades": 0},
        "ai_correlation": {},
        "time_patterns": {},
        "origination_stats": {
            "total_trades": 1, "wins": 1, "losses": 0, "win_rate": 100.0, "net_pnl": 40.0,
        },
    }

    text = _template_pattern_narrative("25 Aug 2026", stats)

    assert "No closed trades were recorded in this period." in text
    assert "AI Origination: 1 trades" in text


def test_pattern_narrative_unaffected_when_origination_stats_absent():
    stats = {
        "trade_stats": {"total_trades": 1, "win_rate": 100.0, "net_pnl": 40.0},
        "ai_correlation": {},
        "time_patterns": {},
    }

    text = _template_pattern_narrative("25 Aug 2026", stats)

    assert "AI Origination" not in text
