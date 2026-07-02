from __future__ import annotations

from sqlalchemy import text

from app.health.models import CRITICAL, HEALTHY, HealthResult


class DatabaseHealth:
    def __init__(self, engine: object) -> None:
        self.engine = engine

    def check(self) -> HealthResult:
        try:
            with self.engine.connect() as connection:
                transaction = connection.begin()
                connection.execute(text("SELECT 1"))
                connection.execute(
                    text("INSERT INTO logs (event_type, level, message, payload) VALUES ('HEALTH_TEST', 'INFO', 'rollback test', '{}')")
                )
                transaction.rollback()
            return HealthResult(HEALTHY, "Connection, read, write and rollback verified")
        except Exception as exc:
            return HealthResult(CRITICAL, str(exc))

