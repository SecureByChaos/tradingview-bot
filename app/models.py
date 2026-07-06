from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


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


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    strategy: str | None = None
    signal: Signal
    market_data: "TradingViewMarketData | None" = None
    indicators: "TradingViewIndicators | None" = None
    trade_state: "TradingViewTradeState | None" = None


class TradingViewIndicators(BaseModel):
    model_config = ConfigDict(extra="allow")
    ema9: float | None = None
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
    trend_direction: str | None = None
    breakout_status: str | None = None
    strong_candle: str | None = None
    sideways_filter: str | None = None
    htf_confirmation: str | None = None
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
    timestamp: str | None = None


class TradingViewTradeState(BaseModel):
    model_config = ConfigDict(extra="allow")
    trade_number: int | None = None
    daily_trade_count: int | None = None
    position: str | None = None
    session: str | None = None
    market_condition: str | None = None


class OptionContract(BaseModel):
    exchange: str = "NFO"
    tradingsymbol: str
    symboltoken: str
    strike: int
    expiry: str
    option_type: str
    lot_size: int


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
