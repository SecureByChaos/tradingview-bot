from __future__ import annotations

from sqlalchemy.orm import Session

from app.db_models import BotStatus
from app.platform import get_or_create_settings, rebuild_daily_stats, set_bot_state, log_event
from app.telegram_service import TelegramService
from app.trade_manager import TradeManager


class RiskProtectionService:
    def __init__(self, trade_manager: TradeManager, telegram: TelegramService) -> None:
        self.trade_manager = trade_manager
        self.telegram = telegram

    def enforce_daily_loss_limit(self, db: Session) -> bool:
        settings = get_or_create_settings(db)
        stats = rebuild_daily_stats(db)
        if stats.risk_locked:
            return True
        if stats.pnl_percent <= settings.daily_max_loss_percent:
            active = self.trade_manager.get_active_trade()
            if active is not None:
                self.trade_manager.square_off_open_trade()
            stats.risk_locked = True
            db.commit()
            set_bot_state(db, BotStatus.RISK_LOCKED, trading_allowed=False, risk_locked=True)
            message = f"Daily loss limit triggered: {stats.pnl_percent:.2f}% <= {settings.daily_max_loss_percent:.2f}%"
            log_event(db, "RISK", message, "WARNING")
            self.telegram.send(db, f"Daily Loss Limit Triggered\n{message}")
            return True
        return False
