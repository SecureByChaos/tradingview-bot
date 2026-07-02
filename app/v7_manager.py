from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.ai.shadow import exit_signal_for_entry, run_shadow_review
from app.db_models import StrategyConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, OptionContract, Signal, WebhookResponse
from app.option_finder import OptionFinder
from app.platform import log_event
from app.smartapi_client import SmartAPIClient
from app.telegram_service import TelegramService
from app.time_utils import format_ist, utc_now

logger = logging.getLogger(__name__)


class V7Manager:
    strategy_name = "V7"

    def __init__(
        self,
        settings: Settings,
        smartapi: SmartAPIClient,
        option_finder: OptionFinder,
        telegram: TelegramService,
    ) -> None:
        self.settings = settings
        self.smartapi = smartapi
        self.option_finder = option_finder
        self.telegram = telegram

    def handle_signal(self, db: Session, signal: Signal) -> WebhookResponse:
        if signal in {Signal.BUY_CE, Signal.BUY_PE}:
            return self.open_trade(db, signal)
        if signal in {Signal.SELL_CE, Signal.SELL_PE}:
            return self.close_trade(db, "CE" if signal == Signal.SELL_CE else "PE")
        return WebhookResponse(accepted=False, message=f"Rejected: unsupported V7 signal {signal.value}")

    def open_trade(self, db: Session, signal: Signal) -> WebhookResponse:
        strategy = self.get_strategy(db)
        if strategy is None:
            return WebhookResponse(accepted=False, message="Rejected: strategy 'V7' does not exist")

        contract = self.option_finder.find_atm_contract(signal)
        entry_price = self.smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
        mode = self.resolve_mode(strategy)
        required_capital = round(entry_price * contract.lot_size, 2)
        available_capital = "N/A (no capital balance ledger)"
        quantity = self.calculate_quantity(strategy, entry_price, contract.lot_size)
        if mode == TradingMode.PAPER and quantity <= 0 and strategy.capital_per_trade > 0:
            quantity = contract.lot_size
        reject_reason = "capital_per_trade is insufficient" if quantity <= 0 else ""
        logger.info(
            "Mode: %s | Configured capital_per_trade: %.2f | Available capital: %s | Required capital: %.2f | Trade quantity: %s | Reject reason: %s",
            mode, strategy.capital_per_trade, available_capital, required_capital, quantity, reject_reason,
        )
        if quantity <= 0:
            return WebhookResponse(
                accepted=False,
                message=f"Rejected: capital_per_trade is insufficient for {contract.tradingsymbol}",
            )

        order_id = None
        if mode == TradingMode.LIVE:
            order_id = self.smartapi.place_market_order(contract, "BUY", quantity)

        trade = StrategyTrade(
            trade_id=uuid4().hex,
            strategy_name=self.strategy_name,
            signal=signal.value,
            exchange=contract.exchange,
            tradingsymbol=contract.tradingsymbol,
            symboltoken=contract.symboltoken,
            strike=contract.strike,
            expiry=contract.expiry,
            option_type=contract.option_type,
            quantity=quantity,
            entry_price=round(entry_price, 2),
            current_premium=round(entry_price, 2),
            stoploss=0.0,
            target=0.0,
            entry_time=utc_now(),
            mode=mode,
            status=TradeStatus.OPEN,
            result=TradeResult.OPEN,
            entry_order_id=order_id,
            highest_price=round(entry_price, 2),
            lowest_price=round(entry_price, 2),
            trailing_active=False,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        log_event(
            db,
            "TRADE",
            f"[V7] {signal.value} opened",
            payload={"trade_id": trade.trade_id, "entry_time_ist": format_ist(trade.entry_time)},
        )
        self.telegram.send(db, f"Trade Opened\n[V7] {signal.value}\nEntry: {trade.entry_price}")
        return WebhookResponse(accepted=True, message=f"V7 {signal.value} opened")

    def close_trade(self, db: Session, option_type: str) -> WebhookResponse:
        trade = db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == self.strategy_name,
                StrategyTrade.option_type == option_type,
                StrategyTrade.status == TradeStatus.OPEN,
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )
        if trade is None:
            return WebhookResponse(accepted=True, message=f"No active V7 {option_type} trade to close")

        exit_price = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
        if trade.mode == TradingMode.LIVE:
            contract = OptionContract(
                exchange=trade.exchange,
                tradingsymbol=trade.tradingsymbol,
                symboltoken=trade.symboltoken,
                strike=trade.strike,
                expiry=trade.expiry,
                option_type=trade.option_type,
                lot_size=max(trade.quantity, 1),
            )
            trade.exit_order_id = self.smartapi.place_market_order(contract, "SELL", trade.quantity)

        trade.exit_price = round(exit_price, 2)
        trade.current_premium = round(exit_price, 2)
        trade.exit_time = utc_now()
        trade.profit_loss = round((exit_price - trade.entry_price) * trade.quantity, 2)
        trade.pnl_percent = round(((exit_price - trade.entry_price) / trade.entry_price) * 100, 2)
        trade.result = self.result_for_pnl(trade.pnl_percent)
        trade.status = TradeStatus.CLOSED
        trade.exit_reason = ExitReason.TV_EXIT.value
        db.commit()
        db.refresh(trade)
        log_event(
            db,
            "TRADE",
            f"[V7] {option_type} closed by TradingView",
            payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent, "exit_time_ist": format_ist(trade.exit_time)},
        )
        self.telegram.send(db, f"Trade Closed\n[V7] {option_type} TV_EXIT\nP&L: {trade.pnl_percent:.2f}%")
        run_shadow_review(trade.strategy_name, exit_signal_for_entry(trade.signal), utc_now(), trade.trade_id)
        return WebhookResponse(accepted=True, message=f"V7 {option_type} trade closed")

    def get_strategy(self, db: Session) -> StrategyConfig | None:
        return db.scalar(select(StrategyConfig).where(func.upper(StrategyConfig.name) == self.strategy_name))

    def calculate_quantity(self, strategy: StrategyConfig, entry_price: float, lot_size: int) -> int:
        if entry_price <= 0 or lot_size <= 0:
            return 0
        lots = int(strategy.capital_per_trade // (entry_price * lot_size))
        return lots * lot_size

    def resolve_mode(self, strategy: StrategyConfig) -> str:
        if strategy.mode == TradingMode.LIVE and strategy.live_trade and self.settings.live_trading:
            return TradingMode.LIVE
        return TradingMode.PAPER

    def result_for_pnl(self, pnl_percent: float) -> str:
        if pnl_percent > 0:
            return TradeResult.WIN
        if pnl_percent < 0:
            return TradeResult.LOSS
        return TradeResult.BREAKEVEN
