from __future__ import annotations

from datetime import date, datetime

import app.ai.agentic as agentic_module
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AgenticAISettings, AIReport, Base, ReportType, StrategyTrade, TradeStatus
from app.time_utils import IST


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _ist_exit(y, m, d, hh=6, mm=0):
    """A naive datetime holding UTC wall-clock numbers that land on IST
    calendar day (y, m, d) once shifted +5:30 by to_ist() -- mirrors exactly
    how a real DateTime(timezone=True) column reads back after the SQLite
    round-trip (tzinfo dropped, the UTC numbers themselves preserved; see
    CLAUDE.md's own documented gotcha and to_ist()'s re-attach-UTC-then-
    convert behaviour). Default 06:00 UTC = 11:30 IST, safely mid-day."""
    return datetime(y, m, d, hh, mm)


def _seed_closed_trade(db, trade_id, net_pnl, result, origin, exit_time):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name=f"{origin} - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=120.0,
        stoploss=90.0, entry_time=exit_time, exit_time=exit_time, origin=origin,
        status=TradeStatus.CLOSED, result=result, exit_reason="TARGET",
        profit_loss=net_pnl, net_pnl=net_pnl, pnl_percent=net_pnl / 10,
    ))


def test_compute_origin_stats_on_date_scopes_to_the_right_ist_calendar_day(tmp_path):
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "today", 100.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 25, 12, 0))
    _seed_closed_trade(db, "yesterday", 200.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 24, 12, 0))
    db.commit()

    stats = agentic_module.compute_origin_stats(db, "AUTONOMOUS_AI", on_date=date(2026, 10, 25))
    assert stats["n"] == 1
    assert stats["net_pnl_total"] == 100.0


def test_compute_origin_stats_on_date_none_is_all_time_unchanged(tmp_path):
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", 100.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 25, 12, 0))
    _seed_closed_trade(db, "t2", 200.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 24, 12, 0))
    db.commit()

    stats = agentic_module.compute_origin_stats(db, "AUTONOMOUS_AI")
    assert stats["n"] == 2


def test_compute_origin_stats_on_date_with_no_matching_trades_reports_the_date(tmp_path):
    db = _make_db(tmp_path)
    db.commit()
    stats = agentic_module.compute_origin_stats(db, "AUTONOMOUS_AI", on_date=date(2026, 10, 25))
    assert stats["n"] == 0
    assert "2026-10-25" in stats["note"]


def test_run_daily_log_combines_every_tracked_strategy(tmp_path):
    db = _make_db(tmp_path)
    today = date(2026, 10, 25)
    _seed_closed_trade(db, "aai1", 500.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 25, 11, 0))
    _seed_closed_trade(db, "qs1", -200.0, "LOSS", "QUICK_SCALP", _ist_exit(2026, 10, 25, 10, 0))
    _seed_closed_trade(db, "orig1", 300.0, "WIN", "AI_ORIGIN_OPENAI", _ist_exit(2026, 10, 25, 13, 0))
    # A different day -- must not leak into today's log.
    _seed_closed_trade(db, "old", 9999.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 24, 11, 0))
    db.commit()

    report = agentic_module.run_daily_log(db, today)
    import json
    saved_stats = json.loads(report.stats_json)
    assert saved_stats["day_total_net_pnl"] == 600.0  # 500 - 200 + 300
    assert saved_stats["by_strategy"]["Autonomous AI"]["n"] == 1
    assert saved_stats["by_strategy"]["Quick Scalp"]["n"] == 1
    assert saved_stats["by_strategy"]["AI Origination"]["n"] == 1
    assert saved_stats["by_strategy"]["Validated Signal"]["n"] == 0
    assert saved_stats["by_strategy"]["Agentic AI"]["n"] == 0


def test_run_daily_log_defaults_to_today_ist_when_no_date_given(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(agentic_module, "today_ist", lambda: date(2026, 10, 25))
    report = agentic_module.run_daily_log(db)
    assert report.period_start == date(2026, 10, 25)


def test_run_daily_log_falls_back_to_template_when_unconfigured(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(
        agentic_module, "_call_openai_qa",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model when unconfigured")),
    )
    report = agentic_module.run_daily_log(db, date(2026, 10, 25))
    assert report.provider == "dummy"
    assert "No closed trades across any tracked strategy" in report.summary_text


def test_run_daily_log_uses_real_model_content_when_configured(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    db.add(AgenticAISettings(
        id=1, enabled=True, api_key="real-key", qa_model="gpt-6-ultra", qa_reasoning_effort="max",
    ))
    _seed_closed_trade(db, "aai1", 500.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 25, 11, 0))
    db.commit()

    monkeypatch.setattr(
        agentic_module, "_call_openai_qa",
        lambda settings, prompt: agentic_module._QACallResult("A real daily recap.", None),
    )
    report = agentic_module.run_daily_log(db, date(2026, 10, 25))
    assert report.summary_text == "A real daily recap."
    assert report.provider == "openai"


def test_run_daily_log_persists_with_the_right_report_type(tmp_path):
    db = _make_db(tmp_path)
    agentic_module.run_daily_log(db, date(2026, 10, 25))
    rows = list(db.query(AIReport).all())
    assert len(rows) == 1
    assert rows[0].report_type == ReportType.AGENTIC_DAILY_LOG
    assert rows[0].title == "Agentic AI Daily Log -- 2026-10-25"


def test_route_triggers_daily_log_with_an_explicit_date(tmp_path):
    from app.dashboard_routes import run_agentic_ai_daily_log

    db = _make_db(tmp_path)
    _seed_closed_trade(db, "aai1", 500.0, "WIN", "AUTONOMOUS_AI", _ist_exit(2026, 10, 25, 11, 0))
    db.commit()

    run_agentic_ai_daily_log(db=db, report_date="2026-10-25")
    rows = list(db.query(AIReport).all())
    assert len(rows) == 1
    assert rows[0].period_start == date(2026, 10, 25)


def test_route_triggers_daily_log_defaulting_to_today_when_blank(tmp_path, monkeypatch):
    from app.dashboard_routes import run_agentic_ai_daily_log

    db = _make_db(tmp_path)
    monkeypatch.setattr(agentic_module, "today_ist", lambda: date(2026, 10, 25))

    run_agentic_ai_daily_log(db=db, report_date="")
    rows = list(db.query(AIReport).all())
    assert rows[0].period_start == date(2026, 10, 25)


def test_run_daily_log_job_skips_on_a_weekend(tmp_path, monkeypatch):
    import app.ai.agentic as mod
    from app.time_utils import IST as ist_tz

    monkeypatch.setattr(mod, "to_ist", lambda dt: datetime(2026, 10, 24, 10, 0, tzinfo=ist_tz))  # Saturday
    monkeypatch.setattr(
        mod, "run_daily_log",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run on a weekend")),
    )
    mod.run_daily_log_job()  # must return without raising
