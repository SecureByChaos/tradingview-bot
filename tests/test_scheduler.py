from __future__ import annotations

from datetime import datetime

from apscheduler.triggers.cron import CronTrigger

from app.scheduler import _run_pre_market_health_if_trading_day, create_scheduler
from app.time_utils import IST


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class _FakeHealthManager:
    def __init__(self) -> None:
        self.calls = 0

    def run(self) -> None:
        self.calls += 1


def test_pre_market_health_skips_on_a_weekend(monkeypatch):
    import app.scheduler as mod

    monkeypatch.setattr(mod, "utc_now", lambda: _ist(2026, 8, 15, 9, 0))  # Saturday
    health_manager = _FakeHealthManager()

    _run_pre_market_health_if_trading_day(health_manager)

    assert health_manager.calls == 0


def test_pre_market_health_skips_on_an_nse_holiday(monkeypatch):
    import app.scheduler as mod

    monkeypatch.setattr(mod, "utc_now", lambda: _ist(2026, 1, 26, 9, 0))  # Republic Day
    health_manager = _FakeHealthManager()

    _run_pre_market_health_if_trading_day(health_manager)

    assert health_manager.calls == 0


def test_pre_market_health_runs_on_an_ordinary_trading_day(monkeypatch):
    import app.scheduler as mod

    monkeypatch.setattr(mod, "utc_now", lambda: _ist(2026, 8, 13, 9, 0))  # Thursday
    health_manager = _FakeHealthManager()

    _run_pre_market_health_if_trading_day(health_manager)

    assert health_manager.calls == 1


class _FakeMonitor:
    def tick(self) -> None:
        pass

    def square_off(self) -> None:
        pass


def _trigger_fields(trigger: CronTrigger) -> dict[str, str]:
    return {field.name: str(field) for field in trigger.fields}


def test_ai_origination_check_uses_a_weekday_session_hours_cron_not_a_24_7_interval():
    # 18 Aug 2026: was IntervalTrigger(minutes=5), firing every 5 minutes
    # around the clock. Must now be a CronTrigger scoped to weekdays and
    # roughly session hours, matching option-chain-collect's existing
    # pattern, rather than firing all night and every weekend.
    scheduler = create_scheduler(_FakeMonitor(), originator_job=lambda: None)
    job = scheduler.get_job("ai-origination-check")

    assert isinstance(job.trigger, CronTrigger)
    fields = _trigger_fields(job.trigger)
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "9-15"
    assert fields["minute"] == "*/5"


def test_ai_origination_check_not_registered_without_a_job():
    scheduler = create_scheduler(_FakeMonitor(), originator_job=None)
    assert scheduler.get_job("ai-origination-check") is None


def test_autonomous_ai_check_uses_the_same_weekday_session_hours_cron():
    scheduler = create_scheduler(_FakeMonitor(), autonomous_job=lambda: None)
    job = scheduler.get_job("autonomous-ai-check")

    assert isinstance(job.trigger, CronTrigger)
    fields = _trigger_fields(job.trigger)
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "9-15"
    assert fields["minute"] == "*/5"


def test_autonomous_ai_check_not_registered_without_a_job():
    scheduler = create_scheduler(_FakeMonitor(), autonomous_job=None)
    assert scheduler.get_job("autonomous-ai-check") is None


def test_pre_market_health_job_wired_through_the_scheduler():
    health_manager = _FakeHealthManager()
    scheduler = create_scheduler(_FakeMonitor(), health_manager=health_manager)
    job = scheduler.get_job("pre-market-health")

    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    fields = _trigger_fields(job.trigger)
    assert fields["day_of_week"] == "mon-fri"


def test_trade_monitor_stays_a_24_7_interval_trigger():
    # Deliberately unchanged -- must keep firing through every hour of an
    # actual trading day so it can still catch a trade the square-off missed.
    # Its own weekday/holiday gate (trading_day_reason, tested elsewhere)
    # plus the empty-open-trades early return already make off-hours firings
    # next to free; narrowing this one would trade away a real safety net.
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = create_scheduler(_FakeMonitor())
    job = scheduler.get_job("trade-monitor")

    assert isinstance(job.trigger, IntervalTrigger)
