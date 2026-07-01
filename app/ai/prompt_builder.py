from __future__ import annotations

import json
from typing import Any, Dict

from app.ai.models import AIContext
from app.ai.prompts import SIGNAL_REVIEW_PROMPT


class PromptBuilder:
    def build_signal_prompt(
        self,
        context: AIContext,
        system_prompt: str = "",
        prompt_version: str = "v1",
    ) -> Dict[str, str]:
        market = context.market_data or {}
        indicators = context.indicators or {}
        account = context.account_state or {}

        values = (
            ("Strategy", context.strategy_name),
            ("Signal", context.signal),
            ("Timestamp", context.timestamp.isoformat()),
            ("BankNifty price", context.spot_price if context.spot_price is not None else market.get("banknifty_price")),
            ("EMA", indicators.get("ema")),
            ("Supertrend", indicators.get("supertrend")),
            ("ADX", indicators.get("adx")),
            ("ATR", indicators.get("atr")),
            ("VWAP", indicators.get("vwap")),
            ("RSI", indicators.get("rsi")),
            ("Volume", market.get("volume")),
            ("Current candle", market.get("current_candle")),
            ("Last candles", market.get("last_candles")),
            ("Support", market.get("support")),
            ("Resistance", market.get("resistance")),
            ("Time remaining", market.get("time_remaining")),
            ("Current position", account.get("current_position")),
            ("Trade number", account.get("trade_number")),
            ("Daily trend", market.get("daily_trend")),
            ("Previous day high", market.get("previous_day_high")),
            ("Previous day low", market.get("previous_day_low")),
        )
        user_prompt = "\n".join("{}: {}".format(label, self._render(value)) for label, value in values)
        user_prompt += (
            "\n\nReturn ONLY valid JSON with these keys: decision, confidence, market_type, "
            "risk, entry_quality, reason_to_buy, reason_not_to_buy, summary. "
            "Do not include markdown, code fences, or additional text."
        )
        return {
            "system_prompt": system_prompt.strip() or SIGNAL_REVIEW_PROMPT,
            "user_prompt": user_prompt,
            "version": prompt_version,
        }

    @staticmethod
    def _render(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str, separators=(",", ":"))
        return str(value)
