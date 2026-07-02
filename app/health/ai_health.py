from __future__ import annotations

from datetime import UTC, datetime

from app.ai.context_builder import SignalContextBuilder
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
        result = create_reviewer(settings).analyze_signal(
            SignalContextBuilder().build("HEALTH_CHECK", "TEST", datetime.now(UTC))
        )
        if result.decision == "ERROR":
            suffix = " (Shadow mode; trading unaffected)" if settings.mode.upper() == "SHADOW" else ""
            return HealthResult(WARNING, result.summary + suffix, result.latency_ms)
        return HealthResult(HEALTHY, "AI provider reachable", result.latency_ms)

