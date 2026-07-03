from __future__ import annotations

import json
from datetime import UTC, datetime

from app.ai.context_builder import SignalContextBuilder
from app.ai.context_repository import get_latest_context_log
from app.ai.factory import create_reviewer
from app.ai.repository import get_settings
from app.health.models import DISABLED, HEALTHY, WARNING, HealthResult


class AIHealth:
    def check(self, db: object) -> HealthResult:
        settings = get_settings(db)
        if settings is None or not settings.enabled:
            return HealthResult(DISABLED, "AI disabled")
        if settings.provider != "dummy" and not settings.api_key:
            return HealthResult(WARNING, "AI API key missing")
        if settings.provider != "dummy" and not settings.model:
            return HealthResult(WARNING, "AI model missing")
        latest_context = get_latest_context_log(db)
        details = {}
        if latest_context is not None:
            details = {
                "latest_context_completeness": latest_context.completeness_percent,
                "latest_payload_size": latest_context.payload_size,
                "latest_ai_latency": latest_context.latency_ms,
                "latest_missing_fields": _json_list(latest_context.missing_fields),
            }
        result = create_reviewer(settings).analyze_signal(
            SignalContextBuilder().build("HEALTH_CHECK", "TEST", datetime.now(UTC))
        )
        if result.decision == "ERROR":
            suffix = " (Shadow mode; trading unaffected)" if settings.mode.upper() == "SHADOW" else ""
            return HealthResult(WARNING, result.summary + suffix, result.latency_ms, details)
        return HealthResult(HEALTHY, "AI provider reachable", result.latency_ms, details)


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []
