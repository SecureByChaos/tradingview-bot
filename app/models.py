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
    market_data: dict[str, Any] | None = None
    indicators: dict[str, Any] | None = None
    trade_state: dict[str, Any] | None = None


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
