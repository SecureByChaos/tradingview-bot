from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.ai.shadow import exit_signal_for_entry, run_shadow_review
from app.db_models import SLMode, StrategyConfig, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, Signal, WebhookResponse
from app.option_finder import OptionFinder
from app.platform import get_index_config, log_event, update_strategy_stats_after_close
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
        state = self.current_state(db, strategy.name)
        if signal in {Signal.BUY_CE, Signal.BUY_PE}:
            if state != "FLAT":
                event = "OPEN_CE" if signal == Signal.BUY_CE else "OPEN_PE"
                message = f"[STATE] {event} ignored"
                log_event(db, "STATE", message, "WARNING")
                return WebhookResponse(accepted=False, message=message)
            log_event(db, "STATE", "[STATE] OPEN_CE accepted" if signal == Signal.BUY_CE else "[STATE] OPEN_PE accepted")
        elif signal in {Signal.SELL_CE, Signal.SELL_PE}:
            option_type = "CE" if signal == Signal.SELL_CE else "PE"
            event = "CLOSE_CE" if option_type == "CE" else "CLOSE_PE"
            expected_state = "LONG_CE" if option_type == "CE" else "LONG_PE"
            if state != expected_state:
                message = f"Ignored {event} because no active {option_type} position exists."
                log_event(db, "STATE", f"[STATE] {event} ignored", "WARNING", {"reason": message})
                return WebhookResponse(accepted=False, message=message)
            trade = self.latest_open_trade_for_option(db, strategy.name, option_type)
            if trade is None:
                message = f"Ignored {event} because no active {option_type} position exists."
                log_event(db, "STATE", f"[STATE] {event} ignored", "WARNING", {"reason": message})
                return WebhookResponse(accepted=False, message=message)
            log_event(db, "STATE", f"[STATE] {event} accepted")
            premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            self.close_trade(db, trade, premium, ExitReason.TV_EXIT)
            return WebhookResponse(accepted=True, message=f"Closed active {option_type} trade")
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
        index = get_index_config(db, strategy.index_symbol)
        if index is None or not index.enabled:
            message = f"Rejected: index '{strategy.index_symbol}' is not configured/enabled. Configure it in Settings > Instruments."
            log_event(db, "STATE", f"[STATE] FAILED_ENTRY {signal.value}", "WARNING", {"strategy": strategy.name, "reason": message})
            return WebhookResponse(accepted=False, message=message)
        try:
            contract = self.option_finder.find_atm_contract(signal, index, strategy.expiry_itm_strikes)
            entry_price = self.smartapi.get_ltp(contract.exchange, contract.tradingsymbol, contract.symboltoken)
        except Exception as exc:
            log_event(
                db,
                "STATE",
                f"[STATE] FAILED_ENTRY {signal.value}",
                "WARNING",
                {"strategy": strategy.name, "error": str(exc)},
            )
            raise
        mode = self.resolve_mode(strategy)
        quantity = self.calculate_quantity(strategy, entry_price, contract.lot_size)
        required_capital = round(entry_price * quantity, 2)
        reject_reason = "lots_per_trade is invalid" if quantity <= 0 else ""
        logger.info(
            "Mode: %s | Configured lots_per_trade: %s | Lot size: %s | Required capital: %.2f | Trade quantity: %s | Reject reason: %s",
            mode, strategy.lots_per_trade, contract.lot_size, required_capital, quantity, reject_reason,
        )
        if quantity <= 0:
            log_event(
                db,
                "STATE",
                f"[STATE] FAILED_ENTRY {signal.value}",
                "WARNING",
                {"strategy": strategy.name, "reason": "lots_per_trade is invalid"},
            )
            return WebhookResponse(
                accepted=False,
                message=f"Rejected: lots_per_trade is invalid for {contract.tradingsymbol}",
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
            index_symbol=strategy.index_symbol,
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
        if not trades:
            return closed
        strategy_names = {trade.strategy_name for trade in trades}
        strategies_by_name = {
            strategy.name: strategy
            for strategy in db.scalars(select(StrategyConfig).where(StrategyConfig.name.in_(strategy_names)))
        }
        now_ist = datetime.now(IST)
        for trade in trades:
            try:
                strategy = strategies_by_name.get(trade.strategy_name)
                sl_mode = strategy.sl_mode if strategy is not None else SLMode.FIXED
                activation_percent = strategy.trailing_activation_percent if strategy is not None else 10.0
                offset_percent = strategy.trailing_offset_percent if strategy is not None else 5.0

                premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
                trade.current_premium = round(premium, 2)
                trade.pnl_percent = round(((premium - trade.entry_price) / trade.entry_price) * 100, 2)
                db.add(StrategyTradeTick(trade_id=trade.trade_id, premium=trade.current_premium))
                is_short = trade.signal.startswith("SELL")
                activation_threshold = round(trade.entry_price * (activation_percent / 100), 2)
                trailing_offset = round(trade.entry_price * (offset_percent / 100), 2)
                reason: ExitReason | None = None

                if is_short:
                    trade.lowest_price = premium if trade.lowest_price is None else min(trade.lowest_price, premium)
                    if sl_mode == SLMode.TRAILING:
                        if not trade.trailing_active and premium <= trade.entry_price - activation_threshold:
                            trade.trailing_active = True
                        if trade.trailing_active:
                            trade.trailing_stop = round(trade.lowest_price + trailing_offset, 2)
                        if trade.trailing_active and trade.trailing_stop is not None and premium >= trade.trailing_stop:
                            reason = ExitReason.STOPLOSS
                        elif not trade.trailing_active and premium >= trade.stoploss:
                            reason = ExitReason.STOPLOSS
                    else:
                        if premium >= trade.stoploss:
                            reason = ExitReason.STOPLOSS
                        elif premium <= trade.target:
                            reason = ExitReason.TARGET
                else:
                    trade.highest_price = premium if trade.highest_price is None else max(trade.highest_price, premium)
                    if sl_mode == SLMode.TRAILING:
                        if not trade.trailing_active and premium >= trade.entry_price + activation_threshold:
                            trade.trailing_active = True
                        if trade.trailing_active:
                            trade.trailing_stop = round(trade.highest_price - trailing_offset, 2)
                        if trade.trailing_active and trade.trailing_stop is not None and premium <= trade.trailing_stop:
                            reason = ExitReason.STOPLOSS
                        elif not trade.trailing_active and premium <= trade.stoploss:
                            reason = ExitReason.STOPLOSS
                    else:
                        if premium <= trade.stoploss:
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
        # AI_ALT_* trades are evaluation-only side-by-side comparisons against the
        # real signal trade -- they must never affect the real strategy's risk
        # state (consecutive losses / risk lock), send Telegram notifications, or
        # trigger further AI review. Their own P&L stays on the trade row itself
        # for later comparison.
        is_ai_alternative = trade.origin != "SIGNAL"
        if not is_ai_alternative:
            stats = update_strategy_stats_after_close(db, trade.strategy_name, trade.result)
        db.commit()
        db.refresh(trade)
        log_event(
            db,
            "TRADE",
            f"[{trade.strategy_name}] trade closed: {reason.value}",
            payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent, "exit_time_ist": format_ist(trade.exit_time), "origin": trade.origin},
        )
        if is_ai_alternative:
            return trade
        if stats.risk_locked:
            log_event(db, "RISK", f"Strategy {trade.strategy_name} locked due to consecutive losses", "WARNING")
            self.telegram.send(db, f"Strategy Risk Lock\n[{trade.strategy_name}] consecutive losses: {stats.consecutive_losses}")
        self.telegram.send(db, f"Trade Closed\n[{trade.strategy_name}] {reason.value}\nP&L: {trade.pnl_percent:.2f}%")
        shadow_market_data = {
            "strike": trade.strike,
            "expiry": trade.expiry,
            "option_price": round(exit_price, 2),
        }
        try:
            trade_index = get_index_config(db, trade.index_symbol)
            spot = round(self.smartapi.get_index_spot(trade_index) if trade_index is not None else self.smartapi.get_banknifty_spot(), 2)
            shadow_market_data["index_price"] = spot
            shadow_market_data["index_symbol"] = trade.index_symbol
            if trade.index_symbol == "BANKNIFTY":
                shadow_market_data["banknifty_price"] = spot
        except Exception as exc:
            logger.info("[AI] Shadow market fetch skipped: %s spot unavailable (%s)", trade.index_symbol, exc)
        logger.info("[AI] Exit review triggered")
        run_shadow_review(
            trade.strategy_name,
            exit_signal_for_entry(trade.signal),
            utc_now(),
            trade.trade_id,
            shadow_market_data,
        )
        return trade

    def current_state(self, db: Session, strategy_name: str) -> str:
        # origin == "SIGNAL" only: AI_ALT_* paper trades are evaluation-only side
        # trades and must never influence the real signal's state machine.
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.strategy_name == strategy_name,
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.origin == "SIGNAL",
                )
            )
        )
        if not trades:
            return "FLAT"
        if any(trade.option_type == "CE" for trade in trades):
            return "LONG_CE"
        if any(trade.option_type == "PE" for trade in trades):
            return "LONG_PE"
        return "FLAT"

    def latest_open_trade_for_option(self, db: Session, strategy_name: str, option_type: str) -> StrategyTrade | None:
        # origin == "SIGNAL" only -- a TradingView exit signal must always act on
        # the real trade, never an AI_ALT_* evaluation side trade that happens to
        # share the same option type.
        return db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == strategy_name,
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.option_type == option_type,
                StrategyTrade.origin == "SIGNAL",
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )

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
        # origin == "SIGNAL" only -- AI_ALT_* evaluation trades must not count
        # against the strategy's real max_active_trades limit.
        return int(
            db.scalar(
                select(func.count()).select_from(StrategyTrade).where(
                    StrategyTrade.strategy_name == strategy_name,
                    StrategyTrade.status == TradeStatus.OPEN,
                    StrategyTrade.origin == "SIGNAL",
                )
            )
            or 0
        )

    def calculate_quantity(self, strategy: StrategyConfig, entry_price: float, lot_size: int) -> int:
        if lot_size <= 0 or strategy.lots_per_trade <= 0:
            return 0
        return strategy.lots_per_trade * lot_size

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
