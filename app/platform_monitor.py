from __future__ import annotations

import logging

from app.database import SessionLocal
from app.db_models import BotStatus
from app.logger import TradeCSVLogger
from app.platform import get_or_create_settings, get_or_create_state, log_event, sync_trade_row
from app.risk import RiskProtectionService
from app.telegram_service import TelegramService
from app.trade_manager import TradeManager

logger = logging.getLogger(__name__)


class PlatformTradeMonitor:
    def __init__(
        self,
        trade_manager: TradeManager,
        trade_logger: TradeCSVLogger,
        telegram: TelegramService,
        risk: RiskProtectionService,
    ) -> None:
        self.trade_manager = trade_manager
        self.trade_logger = trade_logger
        self.telegram = telegram
        self.risk = risk

    def tick(self) -> None:
        with SessionLocal() as db:
            state = get_or_create_state(db)
            if state.status not in {BotStatus.RUNNING, BotStatus.RISK_LOCKED}:
                return
            before = self.trade_manager.get_active_trade()
            if before is None:
                self.risk.enforce_daily_loss_limit(db)
                return
            try:
                self.trade_manager.evaluate_exit()
            except Exception as exc:
                log_event(db, "ERROR", "Live trade monitor failed", "ERROR", {"error": str(exc)})
                self.telegram.send(db, f"System Error\nLive trade monitor failed: {exc}")
                logger.exception("Live trade monitor failed")
                return

            after = self.trade_manager.get_active_trade()
            if after is None:
                settings = get_or_create_settings(db)
                rows = self.trade_logger.read_all()
                if rows:
                    record = sync_trade_row(db, rows[-1], settings.trading_mode)
                    if record is not None:
                        log_event(db, "TRADE", f"Trade closed: {record.exit_reason}", payload={"pnl_percent": record.pnl_percent})
                        self.telegram.send(db, f"Trade Closed\nReason: {record.exit_reason}\nP&L: {record.pnl_percent:.2f}%")
                        if record.exit_reason == "TARGET":
                            self.telegram.send(db, f"Target Hit\nP&L: {record.pnl_percent:.2f}%")
                        elif record.exit_reason == "STOPLOSS":
                            self.telegram.send(db, f"Stop Loss Hit\nP&L: {record.pnl_percent:.2f}%")
                        elif record.exit_reason == "TIME_EXIT":
                            self.telegram.send(db, f"Time Exit\nP&L: {record.pnl_percent:.2f}%")
                self.risk.enforce_daily_loss_limit(db)

    def square_off(self) -> None:
        with SessionLocal() as db:
            before = self.trade_manager.get_active_trade()
            if before is None:
                return
            try:
                self.trade_manager.square_off_open_trade()
                settings = get_or_create_settings(db)
                rows = self.trade_logger.read_all()
                if rows:
                    record = sync_trade_row(db, rows[-1], settings.trading_mode)
                    if record is not None:
                        log_event(db, "TRADE", "Scheduled square-off completed", payload={"pnl_percent": record.pnl_percent})
                        self.telegram.send(db, f"Time Exit\nP&L: {record.pnl_percent:.2f}%")
            except Exception as exc:
                log_event(db, "ERROR", "Scheduled square-off failed", "ERROR", {"error": str(exc)})
                self.telegram.send(db, f"System Error\nScheduled square-off failed: {exc}")
                logger.exception("Scheduled square-off failed")
