from __future__ import annotations

import json
import logging
from datetime import datetime
from threading import RLock

from zoneinfo import ZoneInfo

from app.config import Settings
from app.logger import TradeCSVLogger
from app.models import ActiveTrade, ExitReason, Signal, WebhookResponse
from app.option_finder import OptionFinder
from app.smartapi_client import SmartAPIClient

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class TradeManager:
    def __init__(
        self,
        settings: Settings,
        smartapi: SmartAPIClient,
        option_finder: OptionFinder,
        trade_logger: TradeCSVLogger,
    ) -> None:
        self.settings = settings
        self.smartapi = smartapi
        self.option_finder = option_finder
        self.trade_logger = trade_logger
        self._lock = RLock()
        self._active_trade = self._load_active_trade()

    def get_active_trade(self) -> ActiveTrade | None:
        with self._lock:
            return self._active_trade

    def handle_signal(self, signal: Signal) -> WebhookResponse:
        with self._lock:
            if self._active_trade is not None:
                return WebhookResponse(
                    accepted=False,
                    message="Ignored: an active trade already exists",
                    active_trade=self._active_trade,
                )
            restriction = self._daily_restriction_message()
            if restriction:
                return WebhookResponse(accepted=False, message=restriction)

            contract = self.option_finder.find_atm_contract(signal)
            entry_price = self.smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
            stoploss = round(entry_price * 0.90, 2)
            target = round(entry_price * 1.20, 2)
            quantity = self.settings.order_quantity
            order_id = self.smartapi.place_market_order(contract, "BUY", quantity)
            active_trade = ActiveTrade(
                signal=signal,
                contract=contract,
                entry_price=round(entry_price, 2),
                stoploss=stoploss,
                target=target,
                quantity=quantity,
                entry_time=datetime.now(IST),
                order_id=order_id,
            )
            self._active_trade = active_trade
            self._persist_active_trade()
            logger.info("Opened trade: %s", active_trade.model_dump())
            return WebhookResponse(
                accepted=True,
                message="Trade opened",
                active_trade=active_trade,
            )

    def evaluate_exit(self) -> ActiveTrade | None:
        with self._lock:
            trade = self._active_trade
            if trade is None:
                return None
            now = datetime.now(IST)
            premium = self.smartapi.get_ltp(
                trade.contract.exchange,
                trade.contract.tradingsymbol,
                trade.contract.symboltoken,
            )
            if premium <= trade.stoploss:
                self.close_active_trade(premium, ExitReason.STOPLOSS)
            elif premium >= trade.target:
                self.close_active_trade(premium, ExitReason.TARGET)
            elif now.hour > 15 or (now.hour == 15 and now.minute >= 15):
                self.close_active_trade(premium, ExitReason.TIME_EXIT)
            return self._active_trade

    def square_off_open_trade(self) -> None:
        with self._lock:
            trade = self._active_trade
            if trade is None:
                return
            premium = self.smartapi.get_ltp(
                trade.contract.exchange,
                trade.contract.tradingsymbol,
                trade.contract.symboltoken,
            )
            self.close_active_trade(premium, ExitReason.TIME_EXIT)

    def close_active_trade(self, exit_price: float, reason: ExitReason) -> None:
        trade = self._active_trade
        if trade is None:
            return
        self.smartapi.close_position(trade.contract, trade.quantity)
        exit_time = datetime.now(IST)
        pnl_percent = round(((exit_price - trade.entry_price) / trade.entry_price) * 100, 2)
        self.trade_logger.append(
            {
                "date": trade.entry_time.date().isoformat(),
                "signal": trade.signal.value,
                "strike": trade.contract.strike,
                "entry_price": trade.entry_price,
                "exit_price": round(exit_price, 2),
                "stoploss": trade.stoploss,
                "target": trade.target,
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": exit_time.isoformat(),
                "exit_reason": reason.value,
                "pnl_percent": pnl_percent,
            }
        )
        logger.info("Closed trade reason=%s pnl=%s%%", reason.value, pnl_percent)
        self._active_trade = None
        self._clear_active_trade()

    def _daily_restriction_message(self) -> str | None:
        today = datetime.now(IST).date().isoformat()
        todays_rows = list(self.trade_logger.rows_for_date(today))
        if len(todays_rows) >= 2 and all(
            row.get("exit_reason") == ExitReason.STOPLOSS.value for row in todays_rows[-2:]
        ):
            return "Ignored: stopped for the day after 2 consecutive losses"
        if len(todays_rows) >= 2:
            return "Ignored: maximum 2 trades reached for the day"
        return None

    def _load_active_trade(self) -> ActiveTrade | None:
        path = self.settings.active_trade_path
        if not path.exists() or path.stat().st_size == 0:
            return None
        try:
            return ActiveTrade.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load active trade state from %s", path)
            return None

    def _persist_active_trade(self) -> None:
        self.settings.active_trade_path.parent.mkdir(parents=True, exist_ok=True)
        if self._active_trade is None:
            self._clear_active_trade()
            return
        self.settings.active_trade_path.write_text(
            self._active_trade.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _clear_active_trade(self) -> None:
        self.settings.active_trade_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.active_trade_path.write_text("", encoding="utf-8")
