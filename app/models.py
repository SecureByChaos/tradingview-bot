from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Signal(str, Enum):
    BUY_CE = "BUY_CE"
    BUY_PE = "BUY_PE"


class ExitReason(str, Enum):
    TARGET = "TARGET"
    STOPLOSS = "STOPLOSS"
    TIME_EXIT = "TIME_EXIT"


class WebhookPayload(BaseModel):
    signal: Signal


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
