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
                error = None if response.ok else "HTTP {}".format(response.status_code)
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
