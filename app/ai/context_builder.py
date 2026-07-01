from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from app.ai.models import AIContext


class SignalContextBuilder:
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
                "pcr",
                "india_vix",
                "oi",
                "volume",
                "support",
                "resistance",
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
            for key in ("ema", "supertrend", "adx", "atr", "vwap", "rsi")
        }
        mapped_state = {
            "current_position": state.get("position", state.get("current_position")),
            "trade_number": state.get("trade_number"),
        }

        return AIContext(
            strategy_name=str(getattr(strategy, "name", strategy) or ""),
            signal=str(getattr(signal, "value", signal) or ""),
            timestamp=timestamp,
            symbol=market.get("symbol"),
            spot_price=market.get("banknifty_price", market.get("spot_price")),
            option_price=market.get("option_price"),
            market_data=mapped_market,
            indicators=mapped_indicators,
            account_state=mapped_state,
        )
