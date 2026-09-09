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


def test_quick_scalp_exit_check_uses_a_5_second_interval_trigger():
    # 8 Sep 2026 rebuild: entries moved off the scheduler entirely onto
    # app.quick_scalp_feed.QuickScalpFeed's own bar-close callback -- this
    # job is exit-management + square-off only now, sped up to match
    # app.validated_signal's own 5-second exit-poll precedent.
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = create_scheduler(_FakeMonitor(), quick_scalp_job=lambda: None)
    job = scheduler.get_job("quick-scalp-exit-check")

    assert isinstance(job.trigger, IntervalTrigger)


def test_quick_scalp_exit_check_not_registered_without_a_job():
    scheduler = create_scheduler(_FakeMonitor(), quick_scalp_job=None)
    assert scheduler.get_job("quick-scalp-exit-check") is None


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

def test_index_tick_recorder_uses_a_25_second_interval_trigger():
    # 9 Sep 2026: replaces the IndexPriceTick write that used to happen
    # inline inside app.platform.get_index_live_figures on every dashboard
    # poll -- see CLAUDE.md, "portal unresponsive during market hours",
    # Phase 2c. 25s matches app.platform._INDEX_TICK_THROTTLE_SECONDS.
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = create_scheduler(_FakeMonitor(), index_tick_recorder_job=lambda: None)
    job = scheduler.get_job("index-tick-recorder")

    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == 25


def test_index_tick_recorder_not_registered_without_a_job():
    scheduler = create_scheduler(_FakeMonitor(), index_tick_recorder_job=None)
    assert scheduler.get_job("index-tick-recorder") is None


def test_scheduler_job_defaults_set_misfire_grace_time_to_30_seconds():
    # 9 Sep 2026: the library default (1 second) is far tighter than this app
    # can guarantee under load -- a job delayed by a queued DB-pool checkout
    # or a throttled SmartAPI call would previously be silently SKIPPED
    # rather than run late. Checked at the constructor level (a job added
    # before scheduler.start() is only a pending placeholder -- job_defaults
    # aren't merged into it until the scheduler actually starts, see
    # test_scheduler_default_misfire_grace_time_applies_once_started below
    # for the fully-resolved, end-to-end version of this same check).
    scheduler = create_scheduler(_FakeMonitor())

    assert scheduler._job_defaults["misfire_grace_time"] == 30


def test_scheduler_default_misfire_grace_time_applies_once_started():
    # End-to-end version of the check above: a job with no misfire_grace_time
    # of its own (trade-monitor) inherits 30s once the scheduler resolves
    # pending jobs against job_defaults.
    scheduler = create_scheduler(_FakeMonitor())
    scheduler.start()
    try:
        job = scheduler.get_job("trade-monitor")
        assert job.misfire_grace_time == 30
    finally:
        scheduler.shutdown(wait=False)


def test_option_chain_collect_keeps_its_own_explicit_misfire_grace_time_once_started():
    # job_defaults only fills in jobs that don't specify their own -- this
    # job's existing misfire_grace_time=60 must not be overridden to 30.
    scheduler = create_scheduler(_FakeMonitor(), option_chain_job=lambda: None)
    scheduler.start()
    try:
        job = scheduler.get_job("option-chain-collect")
        assert job.misfire_grace_time == 60
    finally:
        scheduler.shutdown(wait=False)



def test_cron_jobs_get_explicit_60_second_misfire_grace_time_once_started():
    # 9 Sep 2026: differentiated tiers -- 30s (via job_defaults) for the
    # fast IntervalTrigger jobs, an explicit 60s for every CronTrigger job
    # (5-minute-or-slower cadence, which can tolerate a longer delay before
    # a missed firing actually matters).
    scheduler = create_scheduler(
        _FakeMonitor(),
        health_manager=_FakeHealthManager(),
        originator_job=lambda: None,
        autonomous_job=lambda: None,
        validated_signal_entry_job=lambda: None,
        option_chain_job=lambda: None,
    )
    scheduler.start()
    try:
        cron_job_ids = [
            "ai-origination-check", "autonomous-ai-check", "validated-signal-entry-check",
            "option-chain-collect", "daily-square-off", "pre-market-health",
            "ai-daily-summary", "ai-weekly-report", "ai-monthly-report",
        ]
        for job_id in cron_job_ids:
            job = scheduler.get_job(job_id)
            assert job is not None, f"{job_id} did not register"
            assert job.misfire_grace_time == 60, f"{job_id} expected 60, got {job.misfire_grace_time}"
    finally:
        scheduler.shutdown(wait=False)


def test_fast_interval_jobs_keep_the_30_second_default_once_started():
    scheduler = create_scheduler(
        _FakeMonitor(), quick_scalp_job=lambda: None, validated_signal_exit_job=lambda: None,
        index_tick_recorder_job=lambda: None,
    )
    scheduler.start()
    try:
        interval_job_ids = ["trade-monitor", "quick-scalp-exit-check", "validated-signal-exit-check", "index-tick-recorder"]
        for job_id in interval_job_ids:
            job = scheduler.get_job(job_id)
            assert job is not None, f"{job_id} did not register"
            assert job.misfire_grace_time == 30, f"{job_id} expected 30, got {job.misfire_grace_time}"
    finally:
        scheduler.shutdown(wait=False)
