from __future__ import annotations

from datetime import datetime
from enum import Enum
import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Signal(str, Enum):
    BUY_CE = "BUY_CE"
    SELL_CE = "SELL_CE"
    BUY_PE = "BUY_PE"
    SELL_PE = "SELL_PE"


class ExitReason(str, Enum):
    TARGET = "TARGET"
    STOPLOSS = "STOPLOSS"
    TIME_EXIT = "TIME_EXIT"
    TV_EXIT = "TV_EXIT"
    # AI Origination only -- distinct from TIME_EXIT (end-of-day square-off) so
    # reporting can tell "this thesis never developed" apart from "market
    # closed while it was still open." See monitor_open_trades in
    # multi_strategy.py for the stall-window/stall-band check that uses this.
    STALL_EXIT = "STALL_EXIT"


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    context_version: str | None = None
    strategy: str | None = None
    signal: Signal
    market_data: "TradingViewMarketData | None" = None
    indicators: "TradingViewIndicators | None" = None
    trend: "TradingViewTrend | None" = None
    strategy_filters: "TradingViewStrategyFilters | None" = None
    trade_state: "TradingViewTradeState | None" = None

    @model_validator(mode="before")
    @classmethod
    def _unwrap_json_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            text = data.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    return json.loads(text)
                except Exception:
                    return data
        return data


class TradingViewIndicators(BaseModel):
    model_config = ConfigDict(extra="allow")
    ema9: float | None = None
    ema20: float | None = None
    ema21: float | None = None
    ema_gap: float | None = None
    vwap: float | None = None
    rsi: float | None = None
    atr: float | None = None
    adx: float | None = None
    di_plus: float | None = None
    di_minus: float | None = None
    supertrend: float | None = None
    volume_ratio: float | None = None
    orb_high: float | None = None
    orb_low: float | None = None
    filters: dict[str, Any] | None = None
    rr_ratio: float | None = None


class TradingViewMarketData(BaseModel):
    model_config = ConfigDict(extra="allow")
    banknifty_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    timeframe: str | None = None
    volume: float | None = None
    timestamp: str | int | float | None = None


class TradingViewTrend(BaseModel):
    model_config = ConfigDict(extra="allow")
    trend_direction: str | None = None
    breakout: bool | None = None
    strong_candle: bool | None = None
    sideways_filter: bool | None = None
    htf_confirmation: bool | None = None


class TradingViewStrategyFilters(BaseModel):
    model_config = ConfigDict(extra="allow")
    ema_filter: bool | None = None
    supertrend_filter: bool | None = None
    adx_filter: bool | None = None
    session_filter: bool | None = None
    trade_limit_filter: bool | None = None


class TradingViewTradeState(BaseModel):
    model_config = ConfigDict(extra="allow")
    trade_number: int | None = None
    daily_trade_count: int | None = None
    position: int | str | None = None
    session: str | None = None
    market_condition: str | None = None
    trailing_active: bool | None = None


class OptionContract(BaseModel):
    exchange: str = "NFO"
    tradingsymbol: str
    symboltoken: str
    strike: int
    expiry: str
    option_type: str
    lot_size: int
    # Spot price OptionFinder already fetched from SmartAPI while picking the
    # ATM strike -- carried here so callers can reuse it (e.g. for the
    # spot-price cross-check) instead of firing a second, redundant ltpData
    # call against Angel One's 1 req/sec /quote rate limit.
    spot_price: float | None = None


class ActiveTrade(BaseModel):
    signal: Signal
    contract: OptionContract
    entry_price: float = Field(gt=0)
    stoploss: float = Field(gt=0)
    target: float = Field(gt=0)
    quantity: int = Field(gt=0)
    entry_time: datetime
    order_id: Optional[str] = None


class WebhookResponse(BaseModel):
    accepted: bool
    message: str
    active_trade: Optional[ActiveTrade] = None
