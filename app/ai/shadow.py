from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from app.ai.context_builder import SignalContextBuilder
from app.ai.factory import create_reviewer
from app.ai.prompt_builder import PromptBuilder
from app.ai.repository import get_settings
from app.ai.review_repository import save_review
from app.ai.validator import AIResponseValidator
from app.database import SessionLocal
from app.db_models import StrategyConfig, StrategyTrade
from app.platform import today_ist
from app.time_utils import to_ist

logger = logging.getLogger(__name__)
PROMPT_VERSION = "v1"
CONTEXT_VERSION = "v1"
FRAMEWORK_VERSION = "v1"
_EXIT_SIGNAL_MAP = {
    "BUY_CE": "SELL_CE",
    "BUY_PE": "SELL_PE",
    "SELL_CE": "BUY_CE",
    "SELL_PE": "BUY_PE",
}


def exit_signal_for_entry(signal: str) -> str:
    return _EXIT_SIGNAL_MAP.get(signal, signal)


def run_shadow_review(strategy: str, signal: str, timestamp: datetime, trade_id: Optional[str]) -> None:
    try:
        with SessionLocal() as db:
            logger.info("[AI] Shadow started")
            if not trade_id:
                logger.info("[AI] Shadow skipped: trade_id missing")
                return
            settings = get_settings(db)
            if settings is None:
                logger.info("[AI] Shadow skipped: ai_settings row not found")
                return
            if not settings.enabled:
                logger.info("[AI] Shadow skipped: AI disabled")
                return
            if settings.mode != "SHADOW":
                logger.info("[AI] Shadow skipped: mode is %s", settings.mode)
                return
            trade = db.scalar(select(StrategyTrade).where(StrategyTrade.trade_id == trade_id)) if trade_id else None
            strategy_cfg = db.scalar(select(StrategyConfig).where(StrategyConfig.name == strategy))
            event_type = SignalContextBuilder._EVENT_TYPE_MAP.get(signal, "")
            trade_number_today = int(
                db.scalar(
                    select(func.count())
                    .select_from(StrategyTrade)
                    .where(
                        StrategyTrade.strategy_name == strategy,
                        func.date(StrategyTrade.entry_time) == today_ist().isoformat(),
                    )
                )
                or 0
            )
            ts_ist = to_ist(timestamp)
            hour = ts_ist.hour if ts_ist is not None else 0
            minute = ts_ist.minute if ts_ist is not None else 0
            if hour < 9 or (hour == 9 and minute < 15):
                session = "PREOPEN"
            elif hour < 15 or (hour == 15 and minute <= 30):
                session = "REGULAR"
            else:
                session = "POSTMARKET"
            position_state = "NONE"
            if event_type.startswith("OPEN_"):
                position_state = "OPENING"
            elif trade is not None:
                position_state = trade.status
            rr_ratio = None
            if trade is not None and trade.entry_price and trade.stoploss and trade.target:
                risk = abs(trade.entry_price - trade.stoploss)
                reward = abs(trade.target - trade.entry_price)
                rr_ratio = round(reward / risk, 2) if risk > 0 else None
            holding_minutes = None
            if trade is not None and trade.entry_time is not None:
                end_time = trade.exit_time or timestamp
                holding_minutes = max(int((end_time - trade.entry_time).total_seconds() // 60), 0)
            market_data = {
                "event_type": event_type,
                "paper_live": (trade.mode if trade is not None else (strategy_cfg.mode if strategy_cfg is not None else "")),
                "session": session,
                "banknifty_price": "",
                "option_price": (trade.current_premium if trade is not None else None),
                "strike": (trade.strike if trade is not None else None),
                "expiry": (trade.expiry if trade is not None else None),
                "atm_distance": "",
                "previous_close": "",
                "day_high": "",
                "day_low": "",
                "volume": "",
                "volume_ratio": "",
                "orb_high": "",
                "orb_low": "",
                "breakout_status": "",
                "htf_confirmation": "",
                "trend_direction": "",
                "strong_candle": "",
                "sideways_filter": "",
                "filters": {},
            }
            indicators = {
                "ema9": "",
                "ema21": "",
                "ema_gap": "",
                "vwap": "",
                "rsi": "",
                "atr": "",
                "supertrend": "",
                "adx": "",
                "volume_ratio": "",
                "orb_high": "",
                "orb_low": "",
                "breakout_status": "",
                "htf_confirmation": "",
                "trend_direction": "",
                "strong_candle": "",
                "sideways_filter": "",
                "filters": {},
            }
            trade_state = {
                "paper_live": market_data["paper_live"],
                "trade_number_today": trade_number_today,
                "position_state": position_state,
                "entry_price": (trade.entry_price if trade is not None else None),
                "stop_loss": (trade.stoploss if trade is not None else None),
                "target": (trade.target if trade is not None else None),
                "rr_ratio": rr_ratio,
                "current_premium": (trade.current_premium if trade is not None else None),
                "running_pnl": (trade.profit_loss if trade is not None else None),
                "holding_minutes": holding_minutes,
            }
            context = SignalContextBuilder().build(strategy, signal, timestamp, market_data, indicators, trade_state)
            logger.info("[AI] Context built")
            logger.info("[AI] Exit review" if event_type.startswith("CLOSE_") else "[AI] Entry review")
            PromptBuilder().build_signal_prompt(context, settings.system_prompt, PROMPT_VERSION)
            provider_result = create_reviewer(settings).analyze_signal(context)
            result = AIResponseValidator().validate(provider_result.model_dump()).model_copy(
                update={
                    "provider": provider_result.provider,
                    "model": provider_result.model,
                    "latency_ms": provider_result.latency_ms,
                }
            )
            logger.info("[AI] Review parsed")
            if result.decision == "ERROR":
                logger.error("AI shadow review failed: %s", result.summary)
            try:
                logger.info("[AI] Saving review trade_id=%s", trade_id)
                review = save_review(
                    db,
                    trade_id,
                    strategy,
                    signal,
                    result,
                    PROMPT_VERSION,
                    CONTEXT_VERSION,
                    FRAMEWORK_VERSION,
                )
                logger.info("[AI] Review saved id=%s", review.id)
            except Exception as exc:
                logger.exception("[AI] Review save failed: %s", exc)
    except Exception:
        logger.exception("AI shadow review failed")
