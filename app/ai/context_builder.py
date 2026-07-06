from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from app.ai.models import AIContext


class SignalContextBuilder:
    _EVENT_TYPE_MAP = {
        "BUY_CE": "OPEN_CE",
        "BUY_PE": "OPEN_PE",
        "SELL_CE": "CLOSE_CE",
        "SELL_PE": "CLOSE_PE",
    }

    def build(
        self,
        strategy: Any,
        signal: Any,
        timestamp: datetime,
        market_data: Optional[Mapping[str, Any]] = None,
        indicators: Optional[Mapping[str, Any]] = None,
        trade_state: Optional[Mapping[str, Any]] = None,
    ) -> AIContext:
        market = market_data or {}
        indicator_data = indicators or {}
        state = trade_state or {}

        mapped_market: Dict[str, Any] = {
            key: market.get(key)
            for key in (
                "event_type",
                "paper_live",
                "session",
                "strike",
                "expiry",
                "atm_distance",
                "previous_close",
                "day_high",
                "day_low",
                "pcr",
                "india_vix",
                "oi",
                "volume",
                "volume_ratio",
                "support",
                "resistance",
                "orb_high",
                "orb_low",
                "breakout_status",
                "htf_confirmation",
                "trend_direction",
                "strong_candle",
                "sideways_filter",
                "filters",
                "current_candle",
                "previous_candle",
                "last_candles",
                "time_remaining",
                "market_type",
                "daily_trend",
                "previous_day_high",
                "previous_day_low",
            )
        }
        mapped_indicators = {
            key: indicator_data.get(key)
            for key in (
                "ema",
                "ema9",
                "ema21",
                "ema_gap",
                "supertrend",
                "adx",
                "di_plus",
                "di_minus",
                "atr",
                "vwap",
                "rsi",
                "volume_ratio",
                "orb_high",
                "orb_low",
                "breakout_status",
                "htf_confirmation",
                "trend_direction",
                "strong_candle",
                "sideways_filter",
                "filters",
            )
        }
        mapped_state = {
            "paper_live": state.get("paper_live"),
            "trade_number_today": state.get("trade_number_today"),
            "position_state": state.get("position_state"),
            "current_position": state.get("position", state.get("current_position")),
            "trade_number": state.get("trade_number"),
            "entry_price": state.get("entry_price"),
            "stop_loss": state.get("stop_loss"),
            "target": state.get("target"),
            "rr_ratio": state.get("rr_ratio"),
            "current_premium": state.get("current_premium"),
            "running_pnl": state.get("running_pnl"),
            "holding_minutes": state.get("holding_minutes"),
        }
        signal_value = str(getattr(signal, "value", signal) or "")
        event_type = str(market.get("event_type") or self._EVENT_TYPE_MAP.get(signal_value, ""))

        return AIContext(
            strategy_name=str(getattr(strategy, "name", strategy) or ""),
            signal=signal_value,
            event_type=event_type,
            timestamp=timestamp,
            symbol=market.get("symbol"),
            spot_price=market.get("banknifty_price", market.get("spot_price")),
            option_price=market.get("option_price"),
            market_data=mapped_market,
            indicators=mapped_indicators,
            account_state=mapped_state,
        )
