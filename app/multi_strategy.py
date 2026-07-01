from __future__ import annotations

import logging
import os
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db_models import StrategyConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, Signal, WebhookResponse
from app.option_finder import OptionFinder
from app.platform import log_event, update_strategy_stats_after_close
from app.smartapi_client import SmartAPIClient
from app.telegram_service import TelegramService
from app.time_utils import IST, format_ist, utc_now

logger = logging.getLogger(__name__)


class MultiStrategyTradeManager:
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

    def handle_signal(self, db: Session, strategy_name: str | None, signal: Signal) -> WebhookResponse:
        resolved_name = (strategy_name or self.settings.default_strategy_name).strip()
        strategy = self.get_strategy(db, resolved_name)
        if strategy is None:
            return WebhookResponse(accepted=False, message=f"Rejected: strategy '{resolved_name}' does not exist")
        if strategy.name.upper() == "V7" and signal in {Signal.SELL_CE, Signal.SELL_PE}:
            return self.handle_v7_tv_exit(db, strategy.name, signal)
        if not strategy.enabled:
            return WebhookResponse(accepted=False, message=f"Rejected: strategy '{strategy.name}' is disabled")
        if strategy.mode == TradingMode.PAPER and not strategy.paper_trade:
            return WebhookResponse(accepted=False, message=f"Rejected: paper trading is disabled for '{strategy.name}'")
        if strategy.mode == TradingMode.LIVE and not strategy.live_trade:
            return WebhookResponse(accepted=False, message=f"Rejected: live trading is disabled for '{strategy.name}'")

        active_count = self.active_trade_count(db, strategy.name)
        if active_count >= strategy.max_active_trades:
            return WebhookResponse(
                accepted=False,
                message=f"Rejected: strategy '{strategy.name}' active trade limit reached",
            )

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

        is_short = signal.value.startswith("SELL")
        order_id = None
        if mode == TradingMode.LIVE:
            order_id = self.smartapi.place_market_order(contract, "SELL" if is_short else "BUY", quantity)

        stoploss = round(entry_price * (1 + strategy.sl_percent / 100), 2) if is_short else round(entry_price * (1 - strategy.sl_percent / 100), 2)
        target = round(entry_price * (1 - strategy.tp_percent / 100), 2) if is_short else round(entry_price * (1 + strategy.tp_percent / 100), 2)
        now = utc_now()
        trade = StrategyTrade(
            trade_id=uuid4().hex,
            strategy_name=strategy.name,
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
            stoploss=stoploss,
            target=target,
            entry_time=now,
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
            f"[{strategy.name}] {signal.value} opened",
            payload={"trade_id": trade.trade_id, "entry_time_ist": format_ist(trade.entry_time)},
        )
        self.telegram.send(db, f"Trade Opened\n[{strategy.name}] {signal.value}\nEntry: {trade.entry_price}")
        return WebhookResponse(accepted=True, message="Trade opened")

    def handle_v7_tv_exit(self, db: Session, strategy_name: str, signal: Signal) -> WebhookResponse:
        option_type = "CE" if signal == Signal.SELL_CE else "PE"
        log_event(db, "WEBHOOK", f"[V7] TradingView exit signal received: {signal.value}")
        trade = db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == strategy_name,
                StrategyTrade.option_type == option_type,
                StrategyTrade.status == TradeStatus.OPEN,
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )
        if trade is None:
            return WebhookResponse(accepted=True, message=f"No active {option_type} trade to close")
        premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
        self.close_trade(db, trade, premium, ExitReason.TV_EXIT)
        log_event(db, "TRADE", f"[V7] Closed active trade: {trade.trade_id}")
        return WebhookResponse(accepted=True, message=f"Closed active {option_type} trade")

    def monitor_open_trades(self, db: Session) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    func.upper(StrategyTrade.strategy_name) != "V7",
                )
            )
        )
        now_ist = datetime.now(IST)
        for trade in trades:
            try:
                premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
                trade.current_premium = round(premium, 2)
                trade.pnl_percent = round(((premium - trade.entry_price) / trade.entry_price) * 100, 2)
                is_short = trade.signal.startswith("SELL")
                activation_threshold = round(trade.entry_price * (float(os.getenv("TRAILING_ACTIVATION_PERCENT", "10")) / 100), 2)
                trailing_offset = round(trade.entry_price * (float(os.getenv("TRAILING_OFFSET_PERCENT", "5")) / 100), 2)
                reason: ExitReason | None = None

                if is_short:
                    trade.lowest_price = premium if trade.lowest_price is None else min(trade.lowest_price, premium)
                    if not trade.trailing_active and premium <= trade.entry_price - activation_threshold:
                        trade.trailing_active = True
                    if trade.trailing_active:
                        trade.trailing_stop = round(trade.lowest_price + trailing_offset, 2)
                    if trade.trailing_active and trade.trailing_stop is not None and premium >= trade.trailing_stop:
                        reason = ExitReason.STOPLOSS
                    elif premium >= trade.stoploss:
                        reason = ExitReason.STOPLOSS
                    elif premium <= trade.target:
                        reason = ExitReason.TARGET
                else:
                    trade.highest_price = premium if trade.highest_price is None else max(trade.highest_price, premium)
                    if not trade.trailing_active and premium >= trade.entry_price + activation_threshold:
                        trade.trailing_active = True
                    if trade.trailing_active:
                        trade.trailing_stop = round(trade.highest_price - trailing_offset, 2)
                    if trade.trailing_active and trade.trailing_stop is not None and premium <= trade.trailing_stop:
                        reason = ExitReason.STOPLOSS
                    elif premium <= trade.stoploss:
                        reason = ExitReason.STOPLOSS
                    elif premium >= trade.target:
                        reason = ExitReason.TARGET

                if reason is None and (now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 15)):
                    reason = ExitReason.TIME_EXIT
                if reason is not None:
                    self.close_trade(db, trade, premium, reason)
                    closed.append(trade)
            except Exception as exc:
                logger.exception("Multi-strategy monitor failed for trade %s", trade.trade_id)
                log_event(db, "ERROR", f"[{trade.strategy_name}] monitor failed", "ERROR", {"error": str(exc)})
                self.telegram.send(db, f"System Error\n[{trade.strategy_name}] monitor failed: {exc}")
        db.commit()
        return closed

    def close_trade(self, db: Session, trade: StrategyTrade, exit_price: float, reason: ExitReason) -> StrategyTrade:
        if trade.status != TradeStatus.OPEN:
            return trade
        if trade.mode == TradingMode.LIVE:
            from app.models import OptionContract

            contract = OptionContract(
                exchange=trade.exchange,
                tradingsymbol=trade.tradingsymbol,
                symboltoken=trade.symboltoken,
                strike=trade.strike,
                expiry=trade.expiry,
                option_type=trade.option_type,
                lot_size=max(trade.quantity, 1),
            )
            trade.exit_order_id = self.smartapi.place_market_order(
                contract,
                "BUY" if trade.signal.startswith("SELL") else "SELL",
                trade.quantity,
            )
        trade.exit_price = round(exit_price, 2)
        trade.current_premium = round(exit_price, 2)
        trade.exit_time = utc_now()
        direction = -1 if trade.signal.startswith("SELL") else 1
        trade.profit_loss = round((exit_price - trade.entry_price) * trade.quantity * direction, 2)
        trade.pnl_percent = round(((exit_price - trade.entry_price) / trade.entry_price) * 100 * direction, 2)
        trade.result = self.result_for_pnl(trade.pnl_percent)
        trade.status = TradeStatus.CLOSED
        trade.exit_reason = reason.value
        stats = update_strategy_stats_after_close(db, trade.strategy_name, trade.result)
        db.commit()
        db.refresh(trade)
        log_event(
            db,
            "TRADE",
            f"[{trade.strategy_name}] trade closed: {reason.value}",
            payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent, "exit_time_ist": format_ist(trade.exit_time)},
        )
        if stats.risk_locked:
            log_event(db, "RISK", f"Strategy {trade.strategy_name} locked due to consecutive losses", "WARNING")
            self.telegram.send(db, f"Strategy Risk Lock\n[{trade.strategy_name}] consecutive losses: {stats.consecutive_losses}")
        self.telegram.send(db, f"Trade Closed\n[{trade.strategy_name}] {reason.value}\nP&L: {trade.pnl_percent:.2f}%")
        return trade

    def square_off_all(self, db: Session) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    func.upper(StrategyTrade.strategy_name) != "V7",
                )
            )
        )
        for trade in trades:
            premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            closed.append(self.close_trade(db, trade, premium, ExitReason.TIME_EXIT))
        return closed

    def square_off_strategy(self, db: Session, strategy_name: str) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.strategy_name == strategy_name,
                )
            )
        )
        for trade in trades:
            premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            closed.append(self.close_trade(db, trade, premium, ExitReason.TIME_EXIT))
        return closed

    def get_strategy(self, db: Session, name: str) -> StrategyConfig | None:
        return db.scalar(select(StrategyConfig).where(func.lower(StrategyConfig.name) == name.lower()))

    def active_trade_count(self, db: Session, strategy_name: str) -> int:
        return int(
            db.scalar(
                select(func.count()).select_from(StrategyTrade).where(
                    StrategyTrade.strategy_name == strategy_name,
                    StrategyTrade.status == TradeStatus.OPEN,
                )
            )
            or 0
        )

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
