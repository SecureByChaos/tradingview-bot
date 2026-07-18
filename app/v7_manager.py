from __future__ import annotations

import logging
import os
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.ai.shadow import exit_signal_for_entry, run_shadow_review
from app.db_models import StrategyConfig, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus, TradingMode
from app.models import ExitReason, OptionContract, Signal, WebhookResponse
from app.option_finder import OptionFinder
from app.platform import get_index_config, log_event
from app.smartapi_client import SmartAPIClient
from app.telegram_service import TelegramService
from app.time_utils import IST, format_ist, utc_now

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
        state = self.current_state(db)
        if signal in {Signal.BUY_CE, Signal.BUY_PE}:
            if state != "FLAT":
                event = "OPEN_CE" if signal == Signal.BUY_CE else "OPEN_PE"
                message = f"[STATE] {event} ignored"
                log_event(db, "STATE", message, "WARNING")
                return WebhookResponse(accepted=False, message=message)
            log_event(db, "STATE", "[STATE] OPEN_CE accepted" if signal == Signal.BUY_CE else "[STATE] OPEN_PE accepted")
            return self.open_trade(db, signal)
        if signal in {Signal.SELL_CE, Signal.SELL_PE}:
            option_type = "CE" if signal == Signal.SELL_CE else "PE"
            event = "CLOSE_CE" if option_type == "CE" else "CLOSE_PE"
            expected_state = "LONG_CE" if option_type == "CE" else "LONG_PE"
            observation_reason = ""
            if state != expected_state:
                if state == "FLAT":
                    observation_reason = f"Ignored {event} because no active {option_type} position exists."
                elif state == "LONG_CE":
                    observation_reason = f"Ignored {event} because active CE position exists."
                elif state == "LONG_PE":
                    observation_reason = f"Ignored {event} because active PE position exists."
                else:
                    observation_reason = f"Ignored {event} due to invalid state {state}."
                log_event(db, "STATE", f"[STATE] {event} ignored", "WARNING", {"reason": observation_reason})
            return self.record_exit_suggestion(db, signal, observation_reason)
        return WebhookResponse(accepted=False, message=f"Rejected: unsupported V7 signal {signal.value}")

    def open_trade(self, db: Session, signal: Signal) -> WebhookResponse:
        strategy = self.get_strategy(db)
        if strategy is None:
            return WebhookResponse(accepted=False, message="Rejected: strategy 'V7' does not exist")

        index = get_index_config(db, strategy.index_symbol)
        if index is None or not index.enabled:
            message = f"Rejected: index '{strategy.index_symbol}' is not configured/enabled. Configure it in Settings > Instruments."
            log_event(db, "STATE", f"[STATE] FAILED_ENTRY {signal.value}", "WARNING", {"strategy": self.strategy_name, "reason": message})
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
                {"strategy": self.strategy_name, "error": str(exc)},
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
                {"strategy": self.strategy_name, "reason": "lots_per_trade is invalid"},
            )
            return WebhookResponse(
                accepted=False,
                message=f"Rejected: lots_per_trade is invalid for {contract.tradingsymbol}",
            )

        order_id = None
        if mode == TradingMode.LIVE:
            order_id = self.smartapi.place_market_order(contract, "BUY", quantity)

        trade = StrategyTrade(
            trade_id=uuid4().hex,
            strategy_name=self.strategy_name,
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
            trailing_stop=round(entry_price - self._initial_sl(strategy), 2) if signal == Signal.BUY_CE else round(entry_price + self._initial_sl(strategy), 2),
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
        logger.debug(
            "[V7] Entry Premium=%.2f Highest Premium=%.2f Lowest Premium=%.2f Trailing Active=%s Current Trailing SL=%.2f",
            trade.entry_price,
            trade.highest_price or trade.entry_price,
            trade.lowest_price or trade.entry_price,
            trade.trailing_active,
            trade.trailing_stop or 0.0,
        )
        return WebhookResponse(accepted=True, message=f"V7 {signal.value} opened")

    def close_trade(self, db: Session, option_type: str, reason: ExitReason = ExitReason.TIME_EXIT) -> WebhookResponse:
        # origin == "SIGNAL" only -- a TradingView exit must always act on the
        # real trade, never an AI_ALT_* evaluation side trade.
        trade = db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == self.strategy_name,
                StrategyTrade.option_type == option_type,
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == "SIGNAL",
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )
        if trade is None:
            message = f"Ignored CLOSE_{option_type} because no active {option_type} position exists."
            log_event(db, "STATE", f"[STATE] CLOSE_{option_type} ignored", "WARNING", {"reason": message})
            return WebhookResponse(accepted=False, message=message)
        self._close_trade(db, trade, self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken), reason)
        return WebhookResponse(accepted=True, message=f"V7 {option_type} trade closed")

    def monitor_open_trades(self, db: Session) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        strategy = self.get_strategy(db)
        trail_trigger = self._trail_trigger(strategy)
        trail_offset = self._trail_offset(strategy)
        initial_sl = self._initial_sl(strategy)
        now_ist = utc_now().astimezone(IST)
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.strategy_name == self.strategy_name,
                    StrategyTrade.status == TradeStatus.OPEN,
                )
            )
        )
        for trade in trades:
            premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            trade.current_premium = round(premium, 2)
            trade.pnl_percent = round(((premium - trade.entry_price) / trade.entry_price) * 100, 2)
            trade.profit_loss = round((premium - trade.entry_price) * trade.quantity, 2)
            db.add(StrategyTradeTick(trade_id=trade.trade_id, premium=trade.current_premium))
            reason: ExitReason | None = None

            trail_activated_now = False
            trade.highest_price = premium if trade.highest_price is None else max(trade.highest_price, premium)
            if trade.lowest_price is None:
                trade.lowest_price = premium
            else:
                trade.lowest_price = min(trade.lowest_price, premium)

            if not trade.trailing_active and premium >= (trade.entry_price * (1 + (trail_trigger / 100.0))):
                trade.trailing_active = True
                trail_activated_now = True

            initial_stop = trade.entry_price * (1 - (initial_sl / 100.0))
            if trade.trailing_active:
                current_trailing_sl = trade.highest_price * (1 - (trail_offset / 100.0))
            else:
                current_trailing_sl = initial_stop

            previous_trailing_stop = trade.trailing_stop
            trade.trailing_stop = round(current_trailing_sl, 2)
            if previous_trailing_stop is not None and trade.trailing_stop < previous_trailing_stop:
                trade.trailing_stop = previous_trailing_stop

            if premium <= trade.trailing_stop:
                reason = ExitReason.STOPLOSS

            logger.debug(
                "[V7] Entry Premium=%.2f Current Premium=%.2f Highest Premium=%.2f Initial Stop=%.2f Trailing Activated=%s Trailing Stop=%.2f Exit Reason=%s",
                trade.entry_price,
                premium,
                trade.highest_price or premium,
                round(initial_stop, 2),
                trade.trailing_active if not trail_activated_now else True,
                trade.trailing_stop or 0.0,
                reason.value if reason is not None else "",
            )
            if reason is None and (now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 15)):
                reason = ExitReason.TIME_EXIT
            if reason is not None:
                self._close_trade(db, trade, premium, reason)
                closed.append(trade)
                logger.debug("[V7] Actual Exit Reason=%s trade_id=%s", reason.value, trade.trade_id)
        if trades:
            db.commit()
        return closed

    def square_off_all(self, db: Session) -> list[StrategyTrade]:
        closed: list[StrategyTrade] = []
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.strategy_name == self.strategy_name,
                    StrategyTrade.status == TradeStatus.OPEN,
                )
            )
        )
        for trade in trades:
            premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            self._close_trade(db, trade, premium, ExitReason.TIME_EXIT)
            closed.append(trade)
        return closed

    def record_exit_suggestion(self, db: Session, signal: Signal, observation_reason: str = "") -> WebhookResponse:
        option_type = "CE" if signal == Signal.SELL_CE else "PE"
        # origin == "SIGNAL" only -- this reports on the real position's state.
        trade = db.scalar(
            select(StrategyTrade)
            .where(
                StrategyTrade.strategy_name == self.strategy_name,
                StrategyTrade.option_type == option_type,
                StrategyTrade.status == TradeStatus.OPEN,
                StrategyTrade.origin == "SIGNAL",
            )
            .order_by(StrategyTrade.entry_time.desc())
            .limit(1)
        )
        spot = None
        premium = None
        pnl_unrealized = None
        trailing_active = False
        trailing_sl = None
        trade_id = None
        if trade is not None:
            trade_id = trade.trade_id
            premium = self.smartapi.get_ltp(trade.exchange, trade.tradingsymbol, trade.symboltoken)
            pnl_unrealized = round((premium - trade.entry_price) * trade.quantity, 2)
            trailing_active = trade.trailing_active
            trailing_sl = trade.trailing_stop
        index_symbol = trade.index_symbol if trade is not None else None
        if index_symbol is None:
            strategy = self.get_strategy(db)
            index_symbol = strategy.index_symbol if strategy is not None else "BANKNIFTY"
        try:
            index = get_index_config(db, index_symbol)
            spot = round(self.smartapi.get_index_spot(index) if index is not None else self.smartapi.get_banknifty_spot(), 2)
        except Exception:
            spot = None
        payload = {
            "timestamp": utc_now().isoformat(),
            "trade_id": trade_id,
            "signal": signal.value,
            "index_symbol": index_symbol,
            "index_price": spot,
            "banknifty_price": spot if index_symbol == "BANKNIFTY" else None,
            "option_premium": round(premium, 2) if premium is not None else None,
            "unrealized_pnl": pnl_unrealized,
            "trailing_active": trailing_active,
            "current_trailing_sl": trailing_sl,
            "observation_reason": observation_reason,
        }
        log_event(db, "TRADE", "TradingView Exit Suggestion", payload=payload)
        logger.debug(
            "[V7] TradingView Exit Suggestion signal=%s trade_id=%s premium=%s pnl=%s trailing_active=%s trailing_sl=%s",
            signal.value,
            trade_id,
            payload["option_premium"],
            pnl_unrealized,
            trailing_active,
            trailing_sl,
        )
        message = "TradingView Exit Suggestion recorded"
        if observation_reason:
            message = f"{message}: {observation_reason}"
        return WebhookResponse(accepted=True, message=message)

    def _close_trade(self, db: Session, trade: StrategyTrade, exit_price: float, reason: ExitReason) -> None:
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
        trade.exit_reason = reason.value
        db.commit()
        db.refresh(trade)
        log_event(
            db,
            "TRADE",
            f"[V7] {trade.option_type} trade closed: {reason.value}",
            payload={"trade_id": trade.trade_id, "pnl_percent": trade.pnl_percent, "exit_time_ist": format_ist(trade.exit_time), "origin": trade.origin},
        )
        # AI_ALT_* trades are evaluation-only side-by-side comparisons -- no
        # Telegram noise and no further AI review of their own exits.
        if trade.origin != "SIGNAL":
            return
        self.telegram.send(db, f"Trade Closed\n[V7] {trade.option_type} {reason.value}\nP&L: {trade.pnl_percent:.2f}%")
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
        logger.debug("[V7] Actual Exit Reason=%s trade_id=%s", reason.value, trade.trade_id)

    def current_state(self, db: Session) -> str:
        # origin == "SIGNAL" only: AI_ALT_* paper trades are evaluation-only side
        # trades and must never influence the real signal's state machine.
        trades = list(
            db.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.strategy_name == self.strategy_name,
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

    def get_strategy(self, db: Session) -> StrategyConfig | None:
        return db.scalar(select(StrategyConfig).where(func.upper(StrategyConfig.name) == self.strategy_name))

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

    def _trail_trigger(self, strategy: StrategyConfig | None) -> float:
        value = self._first_env("V7_TRAIL_TRIGGER", "TRAIL_TRIGGER", "trailTrigger", "TRAILING_ACTIVATION_PERCENT")
        try:
            return float(value)
        except Exception:
            return float(strategy.sl_percent) if strategy is not None else 10.0

    def _trail_offset(self, strategy: StrategyConfig | None) -> float:
        value = self._first_env("V7_TRAIL_OFFSET", "TRAIL_OFFSET", "trailOffset", "TRAILING_OFFSET_PERCENT")
        try:
            return float(value)
        except Exception:
            return float(strategy.tp_percent) if strategy is not None else 5.0

    def _initial_sl(self, strategy: StrategyConfig | None) -> float:
        value = self._first_env("V7_INITIAL_SL", "INITIAL_SL", "initialSL")
        if value is not None and value.strip():
            try:
                return float(value)
            except Exception:
                pass
        return float(strategy.sl_percent) if strategy is not None else 10.0

    @staticmethod
    def _first_env(*keys: str) -> str | None:
        for key in keys:
            value = os.getenv(key)
            if value is not None and value.strip():
                return value
        return None
