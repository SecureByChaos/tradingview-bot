from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from time import perf_counter

import pyotp

from app.health.models import CRITICAL, HEALTHY, WARNING, HealthResult


class BrokerHealth:
    def __init__(self, smartapi: object) -> None:
        self.smartapi = smartapi

    def check(self) -> tuple[HealthResult, HealthResult, HealthResult]:
        state = self.smartapi.get_broker_health() if hasattr(self.smartapi, "get_broker_health") else {}
        broker_state = str(state.get("status") or "")
        client = getattr(self.smartapi, "_client", None)
        jwt = getattr(self.smartapi, "_jwt_token", None)
        feed = getattr(self.smartapi, "_feed_token", None)
        refresh = getattr(self.smartapi, "_refresh_token", None)
        auth = self._authentication(jwt, feed, refresh, state)
        if broker_state == "FAILED":
            broker = HealthResult(CRITICAL, "SmartAPI broker state is FAILED", details={"status": broker_state})
            return broker, auth, HealthResult(CRITICAL, "BANKNIFTY LTP unavailable")
        if client is None or not jwt or not feed:
            broker = HealthResult(CRITICAL, "SmartAPI session or tokens unavailable")
            return broker, auth, HealthResult(CRITICAL, "BANKNIFTY LTP unavailable")
        started = perf_counter()
        try:
            self.smartapi.get_banknifty_spot()
            latency = round((perf_counter() - started) * 1000, 2)
            ltp = HealthResult(HEALTHY, "BANKNIFTY LTP available", latency)
            websocket = self._websocket_status(client)
            status = HEALTHY if broker_state == "CONNECTED" and websocket != "disconnected" else WARNING
            broker = HealthResult(
                status,
                "SmartAPI session active",
                details={
                    "websocket": websocket,
                    "status": broker_state or "UNKNOWN",
                    "last_login": state.get("last_login"),
                    "last_refresh": state.get("last_refresh"),
                    "last_error": state.get("last_error", ""),
                    "jwt_status": state.get("jwt_status"),
                    "feed_token_status": state.get("feed_token_status"),
                    "ltp_status": state.get("ltp_status"),
                },
            )
            return broker, auth, ltp
        except Exception as exc:
            return HealthResult(CRITICAL, str(exc)), auth, HealthResult(CRITICAL, str(exc))

    def _authentication(self, jwt: str | None, feed: str | None, refresh: str | None, state: dict[str, object]) -> HealthResult:
        if not jwt or not feed:
            return HealthResult(CRITICAL, "JWT or feed token missing")
        settings = self.smartapi.settings
        credentials = all((settings.smartapi_api_key, settings.smartapi_client_id, settings.smartapi_pin, settings.smartapi_totp_secret))
        try:
            totp_ready = len(pyotp.TOTP(settings.smartapi_totp_secret).now()) == 6
        except Exception:
            totp_ready = False
        try:
            payload = jwt.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            expiry = json.loads(base64.urlsafe_b64decode(payload))["exp"]
            if datetime.fromtimestamp(expiry, UTC) <= datetime.now(UTC):
                return HealthResult(CRITICAL, "JWT expired")
        except Exception:
            return HealthResult(
                WARNING,
                "JWT expiry could not be verified",
                details={
                    "refresh_available": bool(refresh),
                    "relogin_ready": credentials and totp_ready,
                    "status": state.get("status"),
                },
            )
        if not refresh or not credentials or not totp_ready:
            return HealthResult(WARNING, "Refresh or automatic re-login capability unavailable")
        return HealthResult(HEALTHY, "JWT valid; refresh and automatic re-login ready")

    @staticmethod
    def _websocket_status(client: object) -> str:
        websocket = getattr(client, "ws", None) or getattr(client, "websocket", None)
        if websocket is None:
            return "not enabled"
        return "connected" if getattr(websocket, "connected", False) else "disconnected"
