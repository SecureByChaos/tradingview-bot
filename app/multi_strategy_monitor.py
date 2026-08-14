from __future__ import annotations

import logging

from app.database import SessionLocal
from app.db_models import BotStatus
from app.multi_strategy import MultiStrategyTradeManager
from app.platform import get_or_create_state, log_event
from app.risk import RiskProtectionService
from app.signal_validation import trading_day_reason
from app.time_utils import to_ist, utc_now

logger = logging.getLogger(__name__)


class MultiStrategyMonitor:
    def __init__(self, manager: MultiStrategyTradeManager, risk: RiskProtectionService, v7_manager: object | None = None) -> None:
        self.manager = manager
        self.risk = risk
        self.v7_manager = v7_manager

    def tick(self) -> None:
        # 14 Aug 2026: day-only gate (weekday + NSE holiday, no hour-of-day
        # component -- see trading_day_reason's docstring). This job is on a
        # bare 30s IntervalTrigger with no day constraint, so it fires
        # unconditionally every 30s including weekends/holidays; in practice
        # monitor_open_trades/v7_manager.monitor_open_trades already cost
        # nothing on those days AS LONG AS every trade closed at the prior
        # session's square-off, since both early-return on an empty open-trade
        # query before making any SmartAPI call. This gate is a second,
        # independent line of defence for the case that assumption doesn't
        # hold (a trade square-off missed for some reason) -- deliberately
        # day-only, not also gated by hour-of-day like AI Origination's own
        # gate, because this job carries ongoing exit-safety responsibility
        # for real open positions and must keep running through every hour of
        # a real trading day, including right up to and past the 15:15
        # square-off, to catch exactly that kind of stuck trade rather than
        # going silent on it.
        if trading_day_reason(to_ist(utc_now())) is not None:
            return
        with SessionLocal() as db:
            state = get_or_create_state(db)
            if state.status not in {BotStatus.RUNNING, BotStatus.RISK_LOCKED}:
                return
            try:
                self.manager.monitor_open_trades(db)
                if self.v7_manager is not None:
                    self.v7_manager.monitor_open_trades(db)
                self.risk.enforce_daily_loss_limits(db)
            except Exception as exc:
                logger.exception("Multi-strategy monitor tick failed")
                log_event(db, "ERROR", "Multi-strategy monitor tick failed", "ERROR", {"error": str(exc)})

    def square_off(self) -> None:
        with SessionLocal() as db:
            try:
                closed = self.manager.square_off_all(db)
                if self.v7_manager is not None:
                    closed.extend(self.v7_manager.square_off_all(db))
                if closed:
                    log_event(db, "TRADE", f"Scheduled square-off closed {len(closed)} open trade(s)")
            except Exception as exc:
                logger.exception("Multi-strategy square-off failed")
                log_event(db, "ERROR", "Multi-strategy square-off failed", "ERROR", {"error": str(exc)})
