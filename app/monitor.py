from __future__ import annotations

import logging

from app.trade_manager import TradeManager

logger = logging.getLogger(__name__)


class TradeMonitor:
    def __init__(self, trade_manager: TradeManager) -> None:
        self.trade_manager = trade_manager

    def tick(self) -> None:
        try:
            self.trade_manager.evaluate_exit()
        except Exception:
            logger.exception("Trade monitor tick failed")

    def square_off(self) -> None:
        try:
            self.trade_manager.square_off_open_trade()
        except Exception:
            logger.exception("Scheduled square-off failed")
