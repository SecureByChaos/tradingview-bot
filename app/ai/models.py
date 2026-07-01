from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AIContext(BaseModel):
    strategy_name: str
    signal: str
    timestamp: datetime
    symbol: Optional[str] = None
    spot_price: Optional[float] = None
    option_price: Optional[float] = None
    market_data: Optional[Dict[str, Any]] = None
    indicators: Optional[Dict[str, Any]] = None
    account_state: Optional[Dict[str, Any]] = None


class ReviewResult(BaseModel):
    decision: str
    confidence: float = Field(ge=0, le=100)
    market_type: Optional[str] = None
    risk: Optional[str] = None
    entry_quality: Optional[str] = None
    reason_to_buy: List[str] = Field(default_factory=list)
    reason_not_to_buy: List[str] = Field(default_factory=list)
    summary: str
    provider: str
    model: Optional[str] = None
    latency_ms: float = Field(default=0, ge=0)
    raw_response: Any = None
