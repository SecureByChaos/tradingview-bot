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
            ("Event Type", context.event_type),
            ("Timestamp", context.timestamp.isoformat()),
            ("Paper/Live", account.get("paper_live") or market.get("paper_live")),
            ("Trade Number Today", account.get("trade_number_today")),
            ("Session", market.get("session")),
            ("Position State", account.get("position_state")),
            ("BankNifty price", context.spot_price if context.spot_price is not None else market.get("banknifty_price")),
            ("Option Premium", context.option_price if context.option_price is not None else market.get("option_price")),
            ("Strike", market.get("strike")),
            ("Expiry", market.get("expiry")),
            ("ATM Distance", market.get("atm_distance")),
            ("Previous Close", market.get("previous_close")),
            ("Day High", market.get("day_high")),
            ("Day Low", market.get("day_low")),
            ("EMA", indicators.get("ema")),
            ("EMA9", indicators.get("ema9")),
            ("EMA21", indicators.get("ema21")),
            ("EMA Gap", indicators.get("ema_gap")),
            ("Supertrend", indicators.get("supertrend")),
            ("ADX", indicators.get("adx")),
            ("ATR", indicators.get("atr")),
            ("VWAP", indicators.get("vwap")),
            ("RSI", indicators.get("rsi")),
            ("Volume", market.get("volume")),
            ("Volume Ratio", indicators.get("volume_ratio") or market.get("volume_ratio")),
            ("ORB High", indicators.get("orb_high") or market.get("orb_high")),
            ("ORB Low", indicators.get("orb_low") or market.get("orb_low")),
            ("Breakout Status", indicators.get("breakout_status") or market.get("breakout_status")),
            ("HTF Confirmation", indicators.get("htf_confirmation") or market.get("htf_confirmation")),
            ("Trend Direction", indicators.get("trend_direction") or market.get("trend_direction")),
            ("Strong Candle", indicators.get("strong_candle") or market.get("strong_candle")),
            ("Sideways Filter", indicators.get("sideways_filter") or market.get("sideways_filter")),
            ("Filter States", indicators.get("filters") or market.get("filters")),
            ("Current candle", market.get("current_candle")),
            ("Last candles", market.get("last_candles")),
            ("Support", market.get("support")),
            ("Resistance", market.get("resistance")),
            ("Time remaining", market.get("time_remaining")),
            ("Current position", account.get("current_position")),
            ("Trade number", account.get("trade_number")),
            ("Entry Price", account.get("entry_price")),
            ("Stop Loss", account.get("stop_loss")),
            ("Target", account.get("target")),
            ("RR Ratio", account.get("rr_ratio")),
            ("Current Premium", account.get("current_premium")),
            ("Running PnL", account.get("running_pnl")),
            ("Holding Minutes", account.get("holding_minutes")),
            ("Daily trend", market.get("daily_trend")),
            ("Previous day high", market.get("previous_day_high")),
            ("Previous day low", market.get("previous_day_low")),
        )
        user_prompt = "\n".join("{}: {}".format(label, self._render(value)) for label, value in values)
        event_type = (context.event_type or "").upper()
        review_instruction = (
            "This is an EXIT review. Evaluate whether exiting now is appropriate. "
            "Treat SELL_CE/SELL_PE as exit actions, not bearish entry setups."
            if event_type.startswith("CLOSE_")
            else "This is an ENTRY review. Evaluate whether this entry should be taken."
        )
        user_prompt += (
            "\n\nReview Type: " + ("EXIT" if event_type.startswith("CLOSE_") else "ENTRY") +
            "\n" + review_instruction +
            "\nReturn ONLY valid JSON with these keys: decision, confidence, market_type, "
            "risk, entry_quality, reason_to_buy, reason_not_to_buy, summary, expected_probability, "
            "received_fields, missing_fields, context_quality. "
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
