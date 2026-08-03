from __future__ import annotations

from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

from app import reports
from app.ai.exit_shadow import run_exit_shadow_checks
from app.monitor import TradeMonitor

IST = ZoneInfo("Asia/Kolkata")


def create_scheduler(
    monitor: TradeMonitor,
    health_manager: object | None = None,
    originator_job: Callable[[], None] | None = None,
    option_chain_job: Callable[[], None] | None = None,
    option_chain_interval_minutes: int = 5,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(
        monitor.tick,
        trigger=IntervalTrigger(seconds=30),
        id="trade-monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_exit_shadow_checks,
        trigger=IntervalTrigger(minutes=3),
        id="ai-exit-shadow-check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if originator_job is not None:
        scheduler.add_job(
            originator_job,
            trigger=IntervalTrigger(minutes=5),
            id="ai-origination-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if option_chain_job is not None:
        # Archival only -- nothing live reads what this writes. Restricted to
        # weekdays and roughly session hours by cron as a first gate; the job
        # itself re-checks market hours, so a holiday costs one no-op call
        # rather than a wasted sweep.
        #
        # coalesce + max_instances=1 matter more here than elsewhere: if the
        # process is paused or the broker is slow, a backlog of chain sweeps
        # firing at once is exactly the burst that would contend with live
        # trading's rate-limit budget.
        scheduler.add_job(
            option_chain_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute=f"*/{max(option_chain_interval_minutes, 1)}",
                timezone=IST,
            ),
            id="option-chain-collect",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
    scheduler.add_job(
        monitor.square_off,
        trigger=CronTrigger(hour=15, minute=15, timezone=IST),
        id="daily-square-off",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if health_manager is not None:
        scheduler.add_job(
            health_manager.run,
            trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=IST),
            id="pre-market-health",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        reports.run_daily_summary_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=IST),
        id="ai-daily-summary",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reports.run_weekly_report_job,
        trigger=CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=IST),
        id="ai-weekly-report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reports.run_monthly_report_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone=IST),
        id="ai-monthly-report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
