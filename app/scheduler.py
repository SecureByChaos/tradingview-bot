from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

from app import reports
from app.monitor import TradeMonitor

IST = ZoneInfo("Asia/Kolkata")


def create_scheduler(monitor: TradeMonitor, health_manager: object | None = None) -> BackgroundScheduler:
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
