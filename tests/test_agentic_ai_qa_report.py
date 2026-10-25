from __future__ import annotations

import app.ai.agentic as agentic_module
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AgenticAISettings, AIReport, Base, ReportType, StrategyTrade, TradeStatus
from app.time_utils import utc_now


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _seed_closed_trade(db, trade_id, net_pnl, result, origin="AGENTIC_AI"):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Agentic AI - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=120.0,
        stoploss=90.0, entry_time=utc_now(), exit_time=utc_now(), origin=origin,
        status=TradeStatus.CLOSED, result=result, exit_reason="TARGET",
        profit_loss=net_pnl, net_pnl=net_pnl, pnl_percent=net_pnl / 10,
    ))


def test_run_qa_report_falls_back_to_template_when_unconfigured(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", net_pnl=50.0, result="WIN")
    db.commit()

    monkeypatch.setattr(
        agentic_module, "_call_openai_qa",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model when unconfigured")),
    )

    report = agentic_module.run_qa_report(db, "AGENTIC_AI", "Test label")
    assert report.report_type == ReportType.AGENTIC_QA
    assert report.provider == "dummy"
    assert "1 closed trades" in report.summary_text


def test_run_qa_report_falls_back_to_template_when_disabled(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    db.add(AgenticAISettings(
        id=1, enabled=False, api_key="real-key", qa_model="gpt-6-ultra", qa_reasoning_effort="max",
    ))
    _seed_closed_trade(db, "t1", net_pnl=50.0, result="WIN")
    db.commit()

    monkeypatch.setattr(
        agentic_module, "_call_openai_qa",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model while disabled")),
    )

    report = agentic_module.run_qa_report(db, "AGENTIC_AI", "Test label")
    assert report.provider == "dummy"


def test_run_qa_report_uses_real_model_content_when_configured(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    db.add(AgenticAISettings(
        id=1, enabled=True, api_key="real-key", qa_model="gpt-6-ultra", qa_reasoning_effort="max",
    ))
    _seed_closed_trade(db, "t1", net_pnl=50.0, result="WIN")
    db.commit()

    monkeypatch.setattr(
        agentic_module, "_call_openai_qa",
        lambda settings, prompt: agentic_module._QACallResult("A real analyst verdict.", None),
    )

    report = agentic_module.run_qa_report(db, "AGENTIC_AI", "Test label")
    assert report.summary_text == "A real analyst verdict."
    assert report.provider == "openai"
    assert report.model == "gpt-6-ultra"


def test_run_qa_report_falls_back_to_template_when_model_call_fails(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    db.add(AgenticAISettings(
        id=1, enabled=True, api_key="real-key", qa_model="gpt-6-ultra", qa_reasoning_effort="max",
    ))
    _seed_closed_trade(db, "t1", net_pnl=50.0, result="WIN")
    db.commit()

    monkeypatch.setattr(
        agentic_module, "_call_openai_qa",
        lambda settings, prompt: agentic_module._QACallResult(None, "HTTP 500"),
    )

    report = agentic_module.run_qa_report(db, "AGENTIC_AI", "Test label")
    assert report.provider == "dummy"
    assert "1 closed trades" in report.summary_text


def test_run_qa_report_persists_via_ai_report_table(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", net_pnl=50.0, result="WIN")
    db.commit()

    agentic_module.run_qa_report(db, "AGENTIC_AI", "Test label")
    rows = list(db.query(AIReport).all())
    assert len(rows) == 1
    assert rows[0].report_type == "AGENTIC_QA"
    assert rows[0].title == "Agentic AI QA -- Test label"


def test_run_qa_report_works_against_a_different_origin_pattern(tmp_path):
    """The whole point of building this generically -- testable against real,
    already-live data before AGENTIC_AI has any trades of its own."""
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", net_pnl=50.0, result="WIN", origin="AUTONOMOUS_AI")
    db.commit()

    report = agentic_module.run_qa_report(db, "AUTONOMOUS_AI", "Autonomous AI sanity check")
    stats = agentic_module.compute_origin_stats(db, "AUTONOMOUS_AI")
    assert stats["n"] == 1
    assert report.title == "Agentic AI QA -- Autonomous AI sanity check"


def test_route_triggers_qa_report_end_to_end(tmp_path):
    from app.dashboard_routes import run_agentic_ai_qa_report

    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", net_pnl=50.0, result="WIN")
    db.commit()

    run_agentic_ai_qa_report(db=db, origin_pattern="AGENTIC_AI", label="Route test")
    rows = list(db.query(AIReport).all())
    assert len(rows) == 1
    assert rows[0].title == "Agentic AI QA -- Route test"
