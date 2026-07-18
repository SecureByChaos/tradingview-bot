from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AIContext(BaseModel):
    strategy_name: str
    signal: str
    event_type: Optional[str] = None
    timestamp: datetime
    symbol: Optional[str] = None
    spot_price: Optional[float] = None
    option_price: Optional[float] = None
    market_data: Optional[Dict[str, Any]] = None
    indicators: Optional[Dict[str, Any]] = None
    account_state: Optional[Dict[str, Any]] = None


class AlternativeCall(BaseModel):
    """Proposed alternative to a rejected signal, e.g. flip the side or keep the
    same side with different risk terms. Populated only when the reviewer's
    decision is REJECT; action="NONE" means the reviewer had nothing better to
    propose (a plain rejection with no replacement)."""

    action: str = "NONE"  # NONE | ADJUST | FLIP
    option_type: Optional[str] = None  # CE | PE -- the side the alternative takes
    sl_percent: Optional[float] = None
    target_percent: Optional[float] = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    reasoning: str = ""


class ReviewResult(BaseModel):
    decision: str
    confidence: float = Field(ge=0, le=100)
    market_type: Optional[str] = None
    risk: Optional[str] = None
    entry_quality: Optional[str] = None
    reason_to_buy: List[str] = Field(default_factory=list)
    reason_not_to_buy: List[str] = Field(default_factory=list)
    summary: str
    expected_probability: Optional[float] = None
    received_fields: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    context_quality: Optional[str] = None
    alternative: Optional[AlternativeCall] = None
    provider: str
    model: Optional[str] = None
    latency_ms: float = Field(default=0, ge=0)
    raw_response: Any = None
