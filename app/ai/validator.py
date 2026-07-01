from __future__ import annotations

import json
from typing import Any, List

from app.ai.models import ReviewResult


class AIResponseValidator:
    _DECISIONS = {"APPROVE", "WATCH", "REJECT", "ERROR"}

    def validate(self, raw_json: Any) -> ReviewResult:
        try:
            data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            if not isinstance(data, dict):
                data = {}
            decision = str(data.get("decision", "ERROR")).upper()
            if decision not in self._DECISIONS:
                decision = "ERROR"
            confidence = self._confidence(data.get("confidence", 0))
            return ReviewResult(
                decision=decision,
                confidence=confidence,
                market_type=str(data.get("market_type") or ""),
                risk=str(data.get("risk") or ""),
                entry_quality=str(data.get("entry_quality") or ""),
                reason_to_buy=self._list(data.get("reason_to_buy")),
                reason_not_to_buy=self._list(data.get("reason_not_to_buy")),
                summary=str(data.get("summary") or ""),
                provider="",
            )
        except Exception:
            return ReviewResult(decision="ERROR", confidence=0, summary="Invalid AI response.", provider="")

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return min(100.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]
