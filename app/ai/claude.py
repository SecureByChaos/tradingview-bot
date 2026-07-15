from __future__ import annotations

import logging

from app.ai.base import AIReviewer
from app.ai.client import AIClient
from app.ai.logger import AILogger
from app.ai.models import AIContext, ReviewResult
from app.ai.prompt_builder import PromptBuilder
from app.ai.validator import AIResponseValidator

logger = logging.getLogger(__name__)

_JSON_ONLY_SUFFIX = (
    "\n\nRespond with a single valid JSON object only. Do not include markdown, "
    "code fences, prose, or any text outside the JSON object."
)


class ClaudeReviewer(AIReviewer):
    """AIReviewer implementation backed by Anthropic's Messages API.

    Mirrors OpenAIReviewer's structure (same AIClient, AILogger, PromptBuilder,
    AIResponseValidator) so the two providers can be compared apples-to-apples on
    identical context/prompts. Any object exposing .provider/.model/.api_key/
    .base_url/.temperature/.timeout_seconds/.system_prompt works as `settings`
    (not necessarily the AISettings ORM row itself -- see shadow.py's secondary
    review path, which builds a lightweight view over the secondary_* columns).
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.client = AIClient()
        self.logger = AILogger()
        self.prompt_builder = PromptBuilder()
        self.validator = AIResponseValidator()

    def analyze_signal(self, context: AIContext) -> ReviewResult:
        request_id = ""
        prompt_version = "v1"
        try:
            prompt = self.prompt_builder.build_signal_prompt(context, self.settings.system_prompt, prompt_version)
            request_id = self.logger.log_request(
                "claude", self.settings.model, prompt["version"], prompt["system_prompt"], prompt["user_prompt"]
            )
            if not self.settings.api_key:
                return self._logged_error(request_id, prompt_version, "Claude API key is not configured.")
            if not self.settings.model:
                return self._logged_error(request_id, prompt_version, "Claude model is not configured.")

            endpoint = (self.settings.base_url or "https://api.anthropic.com/v1").rstrip("/") + "/messages"
            logger.info("[AI] Calling Claude")
            response = self.client.send(
                endpoint=endpoint,
                headers={
                    "x-api-key": self.settings.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                payload={
                    "model": self.settings.model,
                    "max_tokens": 1024,
                    "temperature": self.settings.temperature,
                    "system": prompt["system_prompt"] + _JSON_ONLY_SUFFIX,
                    "messages": [
                        {"role": "user", "content": prompt["user_prompt"]},
                    ],
                },
                timeout=self.settings.timeout_seconds,
            )
            logger.info("[AI] Claude response received")
            if response.error:
                return self._logged_error(request_id, prompt_version, response.error, response.latency_ms)

            text = self._extract_text(response.response_body)
            result = self.validator.validate(text).model_copy(
                update={
                    "provider": "claude",
                    "model": self.settings.model,
                    "latency_ms": response.latency_ms,
                }
            )
            if result.decision == "ERROR":
                self.logger.log_error(request_id, "claude", self.settings.model, prompt_version, response.latency_ms, result.summary)
                return result
            self.logger.log_response(request_id, "claude", self.settings.model, prompt_version, response.latency_ms, result)
            return result
        except Exception as exc:
            return self._logged_error(request_id, prompt_version, str(exc))

    @staticmethod
    def _extract_text(response_body: object) -> str:
        if not isinstance(response_body, dict):
            return ""
        blocks = response_body.get("content") or []
        if not isinstance(blocks, list):
            return ""
        parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)

    def _logged_error(self, request_id: str, prompt_version: str, reason: str, latency_ms: float = 0) -> ReviewResult:
        result = self._error(reason, latency_ms)
        self.logger.log_error(request_id, "claude", self.settings.model, prompt_version, latency_ms, reason)
        return result

    def _error(self, reason: str, latency_ms: float = 0) -> ReviewResult:
        return ReviewResult(
            decision="ERROR",
            confidence=0,
            summary=reason,
            provider="claude",
            model=self.settings.model,
            latency_ms=latency_ms,
        )
