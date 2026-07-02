from __future__ import annotations

from app.health.models import CRITICAL, HEALTHY, HealthResult


class WebhookHealth:
    def __init__(self, app: object, scheduler: object) -> None:
        self.app = app
        self.scheduler = scheduler

    def check(self) -> HealthResult:
        try:
            endpoint = any(getattr(route, "path", None) == "/webhook" for route in self.app.routes)
            queue = bool(getattr(self.scheduler, "running", False))
            if endpoint and queue:
                return HealthResult(HEALTHY, "Webhook endpoint and background scheduler available")
            return HealthResult(CRITICAL, f"endpoint={endpoint}, queue={queue}")
        except Exception as exc:
            return HealthResult(CRITICAL, str(exc))

