from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import StrategyConfig
from app.platform import get_or_create_strategy_stats, log_event, rebuild_strategy_daily_stats
from app.telegram_service import TelegramService


class RiskProtectionService:
    def __init__(self, multi_strategy_manager: object, telegram: TelegramService) -> None:
        self.multi_strategy_manager = multi_strategy_manager
        self.telegram = telegram

    def enforce_daily_loss_limits(self, db: Session) -> list[str]:
        """Per-strategy daily loss backstop. Each strategy is checked against its own
        daily_max_loss_percent; only the offending strategy is squared off and locked -
        other strategies keep trading independently."""
        locked: list[str] = []
        for strategy in db.scalars(select(StrategyConfig).where(StrategyConfig.enabled.is_(True))):
            stats = get_or_create_strategy_stats(db, strategy.name)
            if stats.risk_locked:
                continue
            daily = rebuild_strategy_daily_stats(db, strategy.name)
            if daily.pnl_percent <= strategy.daily_max_loss_percent:
                if hasattr(self.multi_strategy_manager, "square_off_strategy"):
                    self.multi_strategy_manager.square_off_strategy(db, strategy.name)
                stats.risk_locked = True
                db.commit()
                message = (
                    f"Daily loss limit triggered for {strategy.name}: "
                    f"{daily.pnl_percent:.2f}% <= {strategy.daily_max_loss_percent:.2f}%"
                )
                log_event(db, "RISK", message, "WARNING")
                self.telegram.send(db, f"Strategy Daily Loss Limit\n{message}")
                locked.append(strategy.name)
        return locked
