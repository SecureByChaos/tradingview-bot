from __future__ import annotations

from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

from app import reports
from app.monitor import TradeMonitor

IST = ZoneInfo("Asia/Kolkata")


def create_scheduler(
    monitor: TradeMonitor,
    health_manager: object | None = None,
    originator_job: Callable[[], None] | None = None,
    option_chain_job: Callable[[], None] | None = None,
    option_chain_interval_minutes: int = 5,
    closing_auction_job: Callable[[], None] | None = None,
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
        # day_of_week added 17 Aug 2026 -- every other cron job in this file
        # already has it; this one didn't, so it technically fired at 15:15 on
        # Saturday/Sunday too. Harmless in practice (square_off_all's own
        # empty-open-trades early return makes a weekend firing a no-op), but
        # a real SmartAPI-touching call with no reason to happen outside a
        # trading day. Flagged but deliberately not fixed on 14 Aug since that
        # task was scoped elsewhere; fixed now while touching this file for
        # the same class of gap (see app/live_feed.py's market-hours gate,
        # same date).
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=15, timezone=IST),
        id="daily-square-off",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if closing_auction_job is not None:
        # 15:45, after the auction concludes (~15:35) and after derivatives
        # stop at 15:40. Deliberately NOT at 15:35 -- the closing bar has to be
        # published and served by the historical API before it can be fetched,
        # and this job runs once a day, so ten minutes of slack costs nothing
        # while being early costs the whole point of the job.
        scheduler.add_job(
            closing_auction_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=45, timezone=IST),
            id="closing-auction-capture",
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
