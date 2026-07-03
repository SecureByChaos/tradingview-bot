from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.db_models import SystemHealthLog
from app.health.ai_health import AIHealth
from app.health.broker_health import BrokerHealth
from app.health.database_health import DatabaseHealth
from app.health.models import CRITICAL, DISABLED, HEALTHY, WARNING, HealthResult
from app.health.system_health import ServerHealth, TradingEngineHealth
from app.health.webhook_health import WebhookHealth

logger = logging.getLogger(__name__)


class HealthManager:
    def __init__(self, smartapi: object, engine: object, telegram: object) -> None:
        self.smartapi = smartapi
        self.engine = engine
        self.telegram = telegram
        self.app: object | None = None
        self.scheduler: object | None = None

    def run(self, notify: bool = True) -> dict[str, object]:
        logger.info("[HEALTH] Starting health check")
        with SessionLocal() as db:
            broker, authentication, ltp = self._broker()
            database = self._check("Database", lambda: DatabaseHealth(self.engine).check())
            webhook = self._check("Webhook", lambda: WebhookHealth(self.app, self.scheduler).check())
            trading = self._check("Trading Engine", lambda: TradingEngineHealth().check(db))
            ai = self._check("AI", lambda: AIHealth().check(db), critical=False)
            server = self._check("Server", lambda: ServerHealth().check(), critical=False)
            components = {
                "broker": broker,
                "authentication": authentication,
                "ltp": ltp,
                "database": database,
                "webhook": webhook,
                "trading": trading,
                "ai": ai,
                "server": server,
            }
            core = (broker, database, webhook, trading)
            overall = "NOT_READY" if any(item.status == CRITICAL for item in core) else (
                "WARNING" if any(item.status in {WARNING, CRITICAL} for item in components.values()) else "READY"
            )
            scores = {HEALTHY: 100, DISABLED: 100, WARNING: 70, CRITICAL: 0}
            score = round(sum(scores[item.status] for item in components.values()) / len(components))
            report = {
                "overall_status": overall,
                "health_score": score,
                "run_time": datetime.now(UTC).isoformat(),
                "components": {name: result.as_dict() for name, result in components.items()},
            }
            resources = server.details
            row = SystemHealthLog(
                run_time=datetime.now(UTC), overall_status=overall, health_score=score,
                broker_status=broker.status, database_status=database.status,
                webhook_status=webhook.status, trading_status=trading.status,
                ai_status=ai.status, server_status=server.status,
                ltp_latency_ms=ltp.latency_ms, cpu_percent=resources.get("cpu_percent"),
                ram_percent=resources.get("ram_percent"), disk_percent=resources.get("disk_percent"),
                message=json.dumps(report, default=str),
            )
            db.add(row)
            db.execute(SystemHealthLog.__table__.delete().where(SystemHealthLog.run_time < datetime.now(UTC) - timedelta(days=30)))
            db.commit()
            logger.info("[HEALTH] Overall %s", overall)
            logger.info("[HEALTH] Complete report: %s", json.dumps(report, default=str))
            if notify:
                self.telegram.send(db, self._notification(report))
            return report

    def latest(self, db: object) -> dict[str, object] | None:
        row = db.scalar(select(SystemHealthLog).order_by(SystemHealthLog.run_time.desc()).limit(1))
        if row is None:
            return None
        report = json.loads(row.message)
        broker_state = self.smartapi.get_broker_health() if hasattr(self.smartapi, "get_broker_health") else {}
        report["broker_state"] = broker_state
        report["recovery_count"] = int(broker_state.get("recovery_count", getattr(self.smartapi, "_recovery_count", 0)))
        messages = [item["message"] for item in report["components"].values() if item["status"] in {WARNING, CRITICAL}]
        report["last_error"] = str(broker_state.get("last_error") or (messages[0] if messages else ""))
        return report

    def _broker(self) -> tuple[HealthResult, HealthResult, HealthResult]:
        try:
            results = BrokerHealth(self.smartapi).check()
            if results[0].status == HEALTHY:
                logger.info("[HEALTH] Broker OK")
            return results
        except Exception as exc:
            logger.exception("[HEALTH] Broker failed")
            failed = HealthResult(CRITICAL, str(exc))
            return failed, failed, failed

    @staticmethod
    def _check(name: str, check: object, critical: bool = True) -> HealthResult:
        try:
            result = check()
            if result.status in {HEALTHY, DISABLED}:
                logger.info("[HEALTH] %s OK", name)
            else:
                logger.warning("[HEALTH] %s %s: %s", name, result.status, result.message)
            return result
        except Exception as exc:
            logger.exception("[HEALTH] %s failed", name)
            return HealthResult(CRITICAL if critical else WARNING, str(exc))

    @staticmethod
    def _notification(report: dict[str, object]) -> str:
        components = report["components"]
        lines = ["BankNifty Trading Bot", "", "Pre-Market Health", ""]
        for name, result in components.items():
            lines.append(f"{name.title():15} {result['status']}")
        lines.extend(["", "Overall", str(report["overall_status"]), "", "Health Score", f"{report['health_score']}%"])
        if report["overall_status"] == "NOT_READY":
            failures = [name for name in ("broker", "database", "webhook", "trading") if components[name]["status"] == CRITICAL]
            lines.extend(["", "Trading NOT Ready", "Reason: " + ", ".join(failures)])
        return "\n".join(lines)
