from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from app.ai.context_repository import create_context_log, finalize_context_log
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
_COMPLETENESS_FIELDS = (
    "strategy",
    "signal",
    "event_type",
    "paper_live",
    "trade_id",
    "trade_number",
    "session",
    "spot_price",
    "option_price",
    "strike",
    "expiry",
    "premium",
    "ema9",
    "ema21",
    "ema_gap",
    "vwap",
    "rsi",
    "atr",
    "adx",
    "supertrend",
    "orb_high",
    "orb_low",
    "volume_ratio",
    "trend_direction",
    "htf_confirmation",
    "breakout_status",
    "strong_candle",
    "sideways_filter",
    "filters",
    "entry",
    "stop_loss",
    "target",
    "rr_ratio",
    "running_pnl",
    "position",
    "holding_minutes",
    "position_state",
)


def exit_signal_for_entry(signal: str) -> str:
    return _EXIT_SIGNAL_MAP.get(signal, signal)


def run_shadow_review(
    strategy: str,
    signal: str,
    timestamp: datetime,
    trade_id: Optional[str],
    market_data_override: Optional[dict[str, object]] = None,
    indicators_override: Optional[dict[str, object]] = None,
    trade_state_override: Optional[dict[str, object]] = None,
) -> None:
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
            if settings.mode == "DISABLED":
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
            _merge_context_data(market_data, market_data_override)
            _merge_context_data(indicators, indicators_override)
            _merge_context_data(trade_state, trade_state_override)
            context = SignalContextBuilder().build(strategy, signal, timestamp, market_data, indicators, trade_state)
            logger.info("[AI] Context built")
            logger.info("[AI] Exit review" if event_type.startswith("CLOSE_") else "[AI] Entry review")
            prompt = PromptBuilder().build_signal_prompt(context, settings.system_prompt, PROMPT_VERSION)
            context_data = _context_snapshot(
                context=context,
                trade_id=trade_id,
                trade_number_today=trade_number_today,
                session=session,
            )
            missing_fields = _missing_context_fields(context_data)
            completeness = round((1 - (len(missing_fields) / len(_COMPLETENESS_FIELDS))) * 100, 1)
            request_data = {
                "provider": settings.provider,
                "model": settings.model,
                "prompt_version": prompt.get("version", PROMPT_VERSION),
                "messages": [
                    {"role": "system", "content": prompt.get("system_prompt", "")},
                    {"role": "user", "content": prompt.get("user_prompt", "")},
                ],
            }
            payload_size = len(json.dumps(request_data, default=str).encode("utf-8"))
            logger.info("[AI] Context completeness %.0f%%", completeness)
            logger.info("[AI] Payload size %.1f KB", payload_size / 1024 if payload_size else 0.0)
            logger.info(
                "[AI] Missing fields: %s",
                "None" if not missing_fields else ", ".join(missing_fields),
            )
            context_log = create_context_log(
                db,
                timestamp=timestamp,
                strategy=strategy,
                signal=signal,
                event_type=event_type,
                paper_live=str(market_data.get("paper_live") or ""),
                trade_id=trade_id,
                trade_number=trade_number_today,
                session=session,
                context_data=context_data,
                request_data=request_data,
                payload_size=payload_size,
                context_version=CONTEXT_VERSION,
                prompt_version=prompt.get("version", PROMPT_VERSION),
                model=settings.model,
                completeness_percent=completeness,
                missing_fields=missing_fields,
            )
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
            finalize_context_log(
                db,
                context_log.id,
                latency_ms=result.latency_ms,
                decision=result.decision,
                confidence=None if result.decision == "ERROR" else result.confidence,
                reason_to_buy=result.reason_to_buy,
                reason_not_to_buy=result.reason_not_to_buy,
                summary=result.summary,
            )
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


def _context_snapshot(
    *,
    context: object,
    trade_id: Optional[str],
    trade_number_today: int,
    session: str,
) -> dict[str, object]:
    context_dict = getattr(context, "model_dump")()
    market = context_dict.get("market_data") or {}
    indicators = context_dict.get("indicators") or {}
    account = context_dict.get("account_state") or {}
    return {
        "timestamp": context_dict.get("timestamp"),
        "strategy": context_dict.get("strategy_name"),
        "signal": context_dict.get("signal"),
        "event_type": context_dict.get("event_type"),
        "paper_live": account.get("paper_live") or market.get("paper_live"),
        "trade_id": trade_id,
        "trade_number": trade_number_today,
        "session": session,
        "market": {
            "spot_price": context_dict.get("spot_price"),
            "option_price": context_dict.get("option_price"),
            "strike": market.get("strike"),
            "expiry": market.get("expiry"),
            "premium": account.get("current_premium") or context_dict.get("option_price"),
            "atm_distance": market.get("atm_distance"),
            "previous_close": market.get("previous_close"),
            "day_high": market.get("day_high"),
            "day_low": market.get("day_low"),
        },
        "indicators": {
            "ema9": indicators.get("ema9"),
            "ema21": indicators.get("ema21"),
            "ema_gap": indicators.get("ema_gap"),
            "vwap": indicators.get("vwap"),
            "rsi": indicators.get("rsi"),
            "atr": indicators.get("atr"),
            "adx": indicators.get("adx"),
            "supertrend": indicators.get("supertrend"),
            "orb_high": indicators.get("orb_high"),
            "orb_low": indicators.get("orb_low"),
            "volume_ratio": indicators.get("volume_ratio") or market.get("volume_ratio"),
        },
        "trend": {
            "trend_direction": indicators.get("trend_direction") or market.get("trend_direction"),
            "htf_confirmation": indicators.get("htf_confirmation") or market.get("htf_confirmation"),
            "breakout": indicators.get("breakout_status") or market.get("breakout_status"),
            "strong_candle": indicators.get("strong_candle") or market.get("strong_candle"),
            "sideways_filter": indicators.get("sideways_filter") or market.get("sideways_filter"),
        },
        "strategy_filters": indicators.get("filters") or market.get("filters") or {},
        "risk": {
            "entry": account.get("entry_price"),
            "stop_loss": account.get("stop_loss"),
            "target": account.get("target"),
            "rr_ratio": account.get("rr_ratio"),
            "current_premium": account.get("current_premium"),
            "running_pnl": account.get("running_pnl"),
        },
        "trade_state": {
            "position": account.get("current_position"),
            "holding_minutes": account.get("holding_minutes"),
            "position_state": account.get("position_state"),
        },
    }


def _is_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


def _missing_context_fields(context_data: dict[str, object]) -> list[str]:
    market = context_data.get("market") or {}
    indicators = context_data.get("indicators") or {}
    trend = context_data.get("trend") or {}
    risk = context_data.get("risk") or {}
    trade_state = context_data.get("trade_state") or {}
    field_map = {
        "strategy": context_data.get("strategy"),
        "signal": context_data.get("signal"),
        "event_type": context_data.get("event_type"),
        "paper_live": context_data.get("paper_live"),
        "trade_id": context_data.get("trade_id"),
        "trade_number": context_data.get("trade_number"),
        "session": context_data.get("session"),
        "spot_price": market.get("spot_price"),
        "option_price": market.get("option_price"),
        "strike": market.get("strike"),
        "expiry": market.get("expiry"),
        "premium": market.get("premium"),
        "ema9": indicators.get("ema9"),
        "ema21": indicators.get("ema21"),
        "ema_gap": indicators.get("ema_gap"),
        "vwap": indicators.get("vwap"),
        "rsi": indicators.get("rsi"),
        "atr": indicators.get("atr"),
        "adx": indicators.get("adx"),
        "supertrend": indicators.get("supertrend"),
        "orb_high": indicators.get("orb_high"),
        "orb_low": indicators.get("orb_low"),
        "volume_ratio": indicators.get("volume_ratio"),
        "trend_direction": trend.get("trend_direction"),
        "htf_confirmation": trend.get("htf_confirmation"),
        "breakout_status": trend.get("breakout"),
        "strong_candle": trend.get("strong_candle"),
        "sideways_filter": trend.get("sideways_filter"),
        "filters": context_data.get("strategy_filters"),
        "entry": risk.get("entry"),
        "stop_loss": risk.get("stop_loss"),
        "target": risk.get("target"),
        "rr_ratio": risk.get("rr_ratio"),
        "running_pnl": risk.get("running_pnl"),
        "position": trade_state.get("position"),
        "holding_minutes": trade_state.get("holding_minutes"),
        "position_state": trade_state.get("position_state"),
    }
    return [name for name in _COMPLETENESS_FIELDS if not _is_present(field_map.get(name))]


def _merge_context_data(base: dict[str, object], override: Optional[dict[str, object]]) -> None:
    if not override:
        return
    for key, value in override.items():
        if key == "filters" and isinstance(value, dict) and isinstance(base.get("filters"), dict):
            merged = dict(base["filters"])
            merged.update(value)
            base["filters"] = merged
            continue
        base[key] = value
