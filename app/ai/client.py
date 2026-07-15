from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Dict, Optional

import requests


@dataclass(frozen=True)
class AIClientResponse:
    status: Optional[int]
    response_body: Any
    latency_ms: float
    error: Optional[str]


class AIClient:
    def send(
        self,
        endpoint: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: float,
    ) -> AIClientResponse:
        started = perf_counter()
        for attempt in range(2):
            try:
                response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                error = None if response.ok else self._build_error(response.status_code, body)
                return AIClientResponse(response.status_code, body, self._latency(started), error)
            except requests.Timeout:
                if attempt == 0:
                    continue
                return AIClientResponse(None, None, self._latency(started), "Request timed out")
            except requests.RequestException as exc:
                return AIClientResponse(None, None, self._latency(started), str(exc))
            except Exception as exc:
                return AIClientResponse(None, None, self._latency(started), str(exc))
        return AIClientResponse(None, None, self._latency(started), "Request failed")

    @staticmethod
    def _latency(started: float) -> float:
        return round((perf_counter() - started) * 1000, 2)

    @staticmethod
    def _build_error(status_code: int, body: Any) -> str:
        detail = ""
        if isinstance(body, dict):
            error_field = body.get("error")
            if isinstance(error_field, dict):
                detail = str(error_field.get("message") or error_field.get("type") or "")
            elif isinstance(error_field, str):
                detail = error_field
            elif not detail:
                detail = str(body.get("message") or "")
        elif isinstance(body, str):
            detail = body[:300]
        return "HTTP {}{}".format(status_code, ": " + detail if detail else "")
