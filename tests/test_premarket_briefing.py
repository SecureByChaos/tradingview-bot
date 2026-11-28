from __future__ import annotations

from datetime import date, datetime

import app.ai.premarket as premarket_module
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.news import Headline
from app.db_models import AIReport, Base, IndexConfig, ReportType
from app.market_data import FIVE_MINUTE, Bar, store_bars


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _add_index(db, symbol="NIFTY", display_name="Nifty 50", enabled=True):
    db.add(IndexConfig(symbol=symbol, display_name=display_name, enabled=enabled))
    db.commit()


def _seed_previous_day_bars(db, symbol, day, high, low, close):
    bars = [
        Bar(datetime(day.year, day.month, day.day, 9, 15), open=close, high=high, low=low, close=close - 5),
        Bar(datetime(day.year, day.month, day.day, 15, 10), open=close - 5, high=high, low=low, close=close),
    ]
    store_bars(db, symbol, FIVE_MINUTE, bars)


# ---------------------------------------------------------------------------
# _levels_for_index
# ---------------------------------------------------------------------------


def test_levels_for_index_none_with_no_bars(tmp_path):
    db = _make_db(tmp_path)
    _add_index(db)
    index = db.query(IndexConfig).one()
    assert premarket_module._levels_for_index(db, index, date(2026, 11, 28)) is None


def test_levels_for_index_computes_classic_pivot_from_previous_day(tmp_path):
    db = _make_db(tmp_path)
    _add_index(db)
    index = db.query(IndexConfig).one()
    _seed_previous_day_bars(db, "NIFTY", date(2026, 11, 27), high=24300.0, low=24100.0, close=24250.0)

    levels = premarket_module._levels_for_index(db, index, date(2026, 11, 28))

    assert levels is not None
    assert levels.previous_day_high == 24300.0
    assert levels.previous_day_low == 24100.0
    assert levels.previous_day_close == 24250.0
    # Classic pivot = (H + L + C) / 3
    expected_pivot = round((24300.0 + 24100.0 + 24250.0) / 3, 2)
    assert levels.pivot == expected_pivot
    assert levels.resistance_1 == round(2 * expected_pivot - 24100.0, 2)
    assert levels.support_1 == round(2 * expected_pivot - 24300.0, 2)
    assert levels.display_name == "Nifty 50"


def test_levels_for_index_falls_back_to_symbol_when_display_name_blank(tmp_path):
    db = _make_db(tmp_path)
    _add_index(db, display_name="")
    index = db.query(IndexConfig).one()
    _seed_previous_day_bars(db, "NIFTY", date(2026, 11, 27), high=24300.0, low=24100.0, close=24250.0)

    levels = premarket_module._levels_for_index(db, index, date(2026, 11, 28))
    assert levels.display_name == "NIFTY"


# ---------------------------------------------------------------------------
# _format_briefing_text
# ---------------------------------------------------------------------------


def test_format_briefing_text_includes_levels_and_headlines():
    levels = premarket_module._IndexLevels(
        display_name="Nifty 50", previous_day_high=24300.0, previous_day_low=24100.0,
        previous_day_close=24250.0, pivot=24216.67, resistance_1=24333.34, support_1=24133.34,
    )
    text = premarket_module._format_briefing_text([levels], '- "Dow Jones hits record high" (Feed)')
    assert "Nifty 50" in text
    assert "pivot 24216.67" in text
    assert "Dow Jones hits record high" in text


def test_format_briefing_text_no_headlines_says_so_explicitly():
    text = premarket_module._format_briefing_text([], "")
    assert "none found this morning" in text


def test_format_briefing_text_no_levels_omits_that_section():
    text = premarket_module._format_briefing_text([], "")
    assert "Support/resistance" not in text


# ---------------------------------------------------------------------------
# generate_premarket_briefing
# ---------------------------------------------------------------------------


def test_generate_premarket_briefing_persists_a_report_with_no_llm_call(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _add_index(db)
    _seed_previous_day_bars(db, "NIFTY", date(2026, 11, 27), high=24300.0, low=24100.0, close=24250.0)
    monkeypatch.setattr(
        premarket_module, "fetch_market_headlines",
        lambda **kwargs: [Headline("Fed holds rates steady", "Feed", None)],
    )

    report = premarket_module.generate_premarket_briefing(db, now_ist=datetime(2026, 11, 28, 8, 50))

    assert report.report_type == ReportType.PRE_MARKET
    assert report.period_start == date(2026, 11, 28)
    assert report.period_end == date(2026, 11, 28)
    assert report.provider == "dummy"
    assert report.model == ""
    assert "Fed holds rates steady" in report.summary_text
    assert "Nifty 50" in report.summary_text
    persisted = db.query(AIReport).filter(AIReport.report_type == ReportType.PRE_MARKET).one()
    assert persisted.id == report.id


def test_generate_premarket_briefing_disabled_index_excluded(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _add_index(db, enabled=False)
    _seed_previous_day_bars(db, "NIFTY", date(2026, 11, 27), high=24300.0, low=24100.0, close=24250.0)
    monkeypatch.setattr(premarket_module, "fetch_market_headlines", lambda **kwargs: [])

    report = premarket_module.generate_premarket_briefing(db, now_ist=datetime(2026, 11, 28, 8, 50))
    assert "Nifty 50" not in report.summary_text


def test_generate_premarket_briefing_survives_a_headline_fetch_failure(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _add_index(db)
    _seed_previous_day_bars(db, "NIFTY", date(2026, 11, 27), high=24300.0, low=24100.0, close=24250.0)

    def _explode(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(premarket_module, "fetch_market_headlines", _explode)

    report = premarket_module.generate_premarket_briefing(db, now_ist=datetime(2026, 11, 28, 8, 50))
    assert "none found this morning" in report.summary_text
    assert "Nifty 50" in report.summary_text


def test_generate_premarket_briefing_survives_a_levels_computation_failure(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _add_index(db)
    monkeypatch.setattr(premarket_module, "fetch_market_headlines", lambda **kwargs: [])

    def _explode(db_, index, session_date):
        raise RuntimeError("bad candle row")

    monkeypatch.setattr(premarket_module, "_levels_for_index", _explode)

    # Must not raise -- a failure computing one index's levels should still
    # produce a persisted (if sparse) report, not abort the whole job.
    report = premarket_module.generate_premarket_briefing(db, now_ist=datetime(2026, 11, 28, 8, 50))
    assert report.report_type == ReportType.PRE_MARKET


def test_generate_premarket_briefing_passes_the_wide_global_lookback(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _add_index(db)
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(premarket_module, "fetch_market_headlines", _capture)
    premarket_module.generate_premarket_briefing(db, now_ist=datetime(2026, 11, 28, 8, 50))

    assert captured["lookback_minutes"] == premarket_module._GLOBAL_HEADLINE_LOOKBACK_MINUTES
    assert captured["feeds"] == premarket_module._GLOBAL_MARKET_FEEDS


# ---------------------------------------------------------------------------
# get_todays_premarket_briefing_text
# ---------------------------------------------------------------------------


def test_get_todays_briefing_text_empty_when_nothing_generated(tmp_path):
    db = _make_db(tmp_path)
    assert premarket_module.get_todays_premarket_briefing_text(db, day=date(2026, 11, 28)) == ""


def test_get_todays_briefing_text_returns_persisted_text_for_the_right_day(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _add_index(db)
    monkeypatch.setattr(premarket_module, "fetch_market_headlines", lambda **kwargs: [])
    premarket_module.generate_premarket_briefing(db, now_ist=datetime(2026, 11, 28, 8, 50))

    assert premarket_module.get_todays_premarket_briefing_text(db, day=date(2026, 11, 28)) != ""
    # A different day has nothing generated yet.
    assert premarket_module.get_todays_premarket_briefing_text(db, day=date(2026, 11, 27)) == ""


def test_get_todays_briefing_text_picks_the_latest_row_for_the_day(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _add_index(db)
    monkeypatch.setattr(premarket_module, "fetch_market_headlines", lambda **kwargs: [])
    premarket_module.generate_premarket_briefing(db, now_ist=datetime(2026, 11, 28, 8, 50))
    # A second, later run the same day (e.g. a manual re-trigger) must win.
    db.add(AIReport(
        report_type=ReportType.PRE_MARKET, period_start=date(2026, 11, 28), period_end=date(2026, 11, 28),
        title="later", summary_text="later text", stats_json="{}", provider="dummy", model="",
    ))
    db.commit()

    assert premarket_module.get_todays_premarket_briefing_text(db, day=date(2026, 11, 28)) == "later text"


# ---------------------------------------------------------------------------
# run_premarket_briefing_job
# ---------------------------------------------------------------------------


def test_run_premarket_briefing_job_skips_on_a_holiday_without_opening_a_session(monkeypatch):
    import app.signal_validation as signal_validation_module
    monkeypatch.setattr(signal_validation_module, "trading_day_reason", lambda now_ist: "weekend")

    called = {"value": False}

    class _ExplodingSessionLocal:
        def __call__(self):
            called["value"] = True
            raise AssertionError("must not open a session on a non-trading day")

    monkeypatch.setattr("app.database.SessionLocal", _ExplodingSessionLocal())
    premarket_module.run_premarket_briefing_job()
    assert called["value"] is False


def test_run_premarket_briefing_job_runs_on_an_ordinary_trading_day(tmp_path, monkeypatch):
    import app.signal_validation as signal_validation_module
    monkeypatch.setattr(signal_validation_module, "trading_day_reason", lambda now_ist: None)

    db = _make_db(tmp_path)

    class _FakeSessionLocal:
        def __call__(self):
            return db

    monkeypatch.setattr("app.database.SessionLocal", _FakeSessionLocal())
    monkeypatch.setattr(premarket_module, "fetch_market_headlines", lambda **kwargs: [])

    premarket_module.run_premarket_briefing_job()

    assert db.query(AIReport).filter(AIReport.report_type == ReportType.PRE_MARKET).count() == 1
