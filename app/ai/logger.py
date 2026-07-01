from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from app.ai.models import ReviewResult


class AILogger:
    def __init__(self) -> None:
        self._logger = logging.getLogger("app.ai.audit")

    def log_request(self, provider: str, model: str, prompt_version: str, system_prompt: str, user_prompt: str) -> str:
        request_id = str(uuid4())
        prompt_hash = hashlib.sha256((system_prompt + "\n" + user_prompt).encode("utf-8")).hexdigest()
        self._write(
            logging.INFO,
            request_id=request_id,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            latency_ms=0,
            status="REQUEST",
        )
        return request_id

    def log_response(self, request_id: str, provider: str, model: str, prompt_version: str, latency_ms: float, result: ReviewResult) -> None:
        self._write(
            logging.INFO,
            request_id=request_id,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            status="SUCCESS",
            decision=result.decision,
            confidence=result.confidence,
            summary=result.summary,
        )

    def log_error(self, request_id: str, provider: str, model: str, prompt_version: str, latency_ms: float, error: str) -> None:
        self._write(
            logging.ERROR,
            request_id=request_id,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            status="ERROR",
            summary=error,
        )

    def _write(self, level: int, **values: Any) -> None:
        try:
            record: Dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
            record.update(values)
            self._logger.log(level, json.dumps(record, default=str, separators=(",", ":")))
        except Exception:
            pass
