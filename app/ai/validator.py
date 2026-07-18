from __future__ import annotations

import json
from typing import Any, List

from app.ai.models import AlternativeCall, ReviewResult


class AIResponseValidator:
    _DECISIONS = {"APPROVE", "WATCH", "REJECT", "ERROR"}
    _DECISION_ALIASES = {
        "BUY": "APPROVE",
        "TAKE": "APPROVE",
        "GO": "APPROVE",
        "ENTER": "APPROVE",
        "YES": "APPROVE",
        "HOLD": "WATCH",
        "WAIT": "WATCH",
        "NEUTRAL": "WATCH",
        "OBSERVE": "WATCH",
        "SKIP": "REJECT",
        "NO": "REJECT",
        "AVOID": "REJECT",
        "PASS": "REJECT",
        "DON'T BUY": "REJECT",
        "DO_NOT_BUY": "REJECT",
    }
    _ALT_ACTIONS = {"NONE", "ADJUST", "FLIP"}
    _ALT_ACTION_ALIASES = {
        "ADJUST_TERMS": "ADJUST",
        "SAME_SIDE": "ADJUST",
        "KEEP_SIDE": "ADJUST",
        "MODIFY": "ADJUST",
        "REVERSE": "FLIP",
        "FLIP_SIDE": "FLIP",
        "OPPOSITE": "FLIP",
        "SWITCH": "FLIP",
        "NO_ALTERNATIVE": "NONE",
        "NO_CHANGE": "NONE",
    }

    def validate(self, raw_json: Any) -> ReviewResult:
        try:
            data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            if not isinstance(data, dict):
                data = {}
            decision = self._normalize_decision(data.get("decision", "ERROR"))
            if decision not in self._DECISIONS:
                decision = "ERROR"
            confidence = 0.0 if decision == "ERROR" else self._confidence(data.get("confidence", 0))
            summary = str(data.get("summary") or "")
            if decision == "ERROR" and not summary:
                summary = "AI response validation failed."
            return ReviewResult(
                decision=decision,
                confidence=confidence,
                market_type=str(data.get("market_type") or ""),
                risk=str(data.get("risk") or ""),
                entry_quality=str(data.get("entry_quality") or ""),
                reason_to_buy=self._list(data.get("reason_to_buy")),
                reason_not_to_buy=self._list(data.get("reason_not_to_buy")),
                summary=summary,
                expected_probability=self._optional_confidence(data.get("expected_probability")),
                received_fields=self._list(data.get("received_fields")),
                missing_fields=self._list(data.get("missing_fields")),
                context_quality=str(data.get("context_quality") or ""),
                alternative=self._alternative(data.get("alternative")),
                provider="",
            )
        except Exception:
            return ReviewResult(decision="ERROR", confidence=0, summary="Invalid AI response.", provider="")

    @classmethod
    def _alternative(cls, value: Any) -> AlternativeCall | None:
        if not isinstance(value, dict):
            return None
        action = str(value.get("action") or "NONE").strip().upper().replace(" ", "_")
        action = cls._ALT_ACTION_ALIASES.get(action, action)
        if action not in cls._ALT_ACTIONS:
            action = "NONE"
        option_type = value.get("option_type")
        option_type = str(option_type).strip().upper() if option_type else None
        if option_type not in ("CE", "PE"):
            option_type = None
        return AlternativeCall(
            action=action,
            option_type=option_type,
            sl_percent=cls._optional_percent(value.get("sl_percent")),
            target_percent=cls._optional_percent(value.get("target_percent")),
            confidence=cls._confidence(value.get("confidence", 0)),
            reasoning=str(value.get("reasoning") or ""),
        )

    @staticmethod
    def _optional_percent(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            if isinstance(value, str):
                return float(value.strip().replace("%", ""))
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            if isinstance(value, str):
                cleaned = value.strip().replace("%", "")
                numeric = float(cleaned)
            else:
                numeric = float(value)
            if numeric > 1.0:
                numeric = numeric / 100.0
            return min(1.0, max(0.0, numeric))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _normalize_decision(cls, value: Any) -> str:
        decision = str(value or "ERROR").strip().upper()
        decision = " ".join(decision.split())
        return cls._DECISION_ALIASES.get(decision, decision)

    @classmethod
    def _optional_confidence(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return cls._confidence(value)

    @staticmethod
    def _list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]