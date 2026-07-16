from __future__ import annotations

import logging

from app.database import SessionLocal
from app.db_models import BotStatus
from app.multi_strategy import MultiStrategyTradeManager
from app.platform import get_or_create_state, log_event
from app.risk import RiskProtectionService

logger = logging.getLogger(__name__)


class MultiStrategyMonitor:
    def __init__(self, manager: MultiStrategyTradeManager, risk: RiskProtectionService, v7_manager: object | None = None) -> None:
        self.manager = manager
        self.risk = risk
        self.v7_manager = v7_manager

    def tick(self) -> None:
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
