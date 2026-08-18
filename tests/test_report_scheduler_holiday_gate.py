from __future__ import annotations

from datetime import datetime

import app.reports as reports_module
from app.reports import run_daily_summary_job, run_monthly_report_job, run_weekly_report_job
from app.time_utils import IST


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class _ExplodingSessionLocal:
    def __call__(self):
        raise AssertionError("must not open a DB session -- report generation should not run today")


def test_daily_summary_job_skips_on_a_weekend(monkeypatch):
    monkeypatch.setattr(reports_module, "utc_now", lambda: _ist(2026, 8, 15, 16, 0))  # Saturday
    monkeypatch.setattr(reports_module, "SessionLocal", _ExplodingSessionLocal())
    run_daily_summary_job()  # must return without raising


def test_daily_summary_job_skips_on_an_nse_holiday(monkeypatch):
    monkeypatch.setattr(reports_module, "utc_now", lambda: _ist(2026, 1, 26, 16, 0))  # Republic Day
    monkeypatch.setattr(reports_module, "SessionLocal", _ExplodingSessionLocal())
    run_daily_summary_job()


def test_daily_summary_job_runs_on_an_ordinary_trading_day(monkeypatch):
    monkeypatch.setattr(reports_module, "utc_now", lambda: _ist(2026, 8, 13, 16, 0))  # Thursday
    called = {"n": 0}
    monkeypatch.setattr(reports_module, "generate_daily_summary", lambda db: called.__setitem__("n", called["n"] + 1))

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(reports_module, "SessionLocal", lambda: _FakeSession())
    run_daily_summary_job()

    assert called["n"] == 1


def test_weekly_report_job_skips_on_a_weekend(monkeypatch):
    monkeypatch.setattr(reports_module, "utc_now", lambda: _ist(2026, 8, 15, 17, 0))  # Saturday
    monkeypatch.setattr(reports_module, "SessionLocal", _ExplodingSessionLocal())
    run_weekly_report_job()


def test_monthly_report_job_skips_on_an_nse_holiday_even_when_it_is_the_last_weekday(monkeypatch):
    # is_last_trading_day_of_month only accounts for weekends -- if the last
    # weekday of the month also happens to be an NSE holiday, this must still
    # skip rather than generate a report for a day nothing traded.
    monkeypatch.setattr(reports_module, "utc_now", lambda: _ist(2026, 1, 26, 18, 0))  # Republic Day (a Monday)
    monkeypatch.setattr(reports_module, "today_ist", lambda: _ist(2026, 1, 26).date())
    monkeypatch.setattr(reports_module, "is_last_trading_day_of_month", lambda today: True)
    monkeypatch.setattr(reports_module, "SessionLocal", _ExplodingSessionLocal())
    run_monthly_report_job()
