from __future__ import annotations

from app.ai.base import AIReviewer
from app.ai.client import AIClient
from app.ai.logger import AILogger
from app.ai.models import AIContext, ReviewResult
from app.ai.prompt_builder import PromptBuilder
from app.ai.validator import AIResponseValidator
from app.db_models import AISettings


class OpenAIReviewer(AIReviewer):
    def __init__(self, settings: AISettings) -> None:
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
                "openai", self.settings.model, prompt["version"], prompt["system_prompt"], prompt["user_prompt"]
            )
            if not self.settings.api_key:
                return self._logged_error(request_id, prompt_version, "OpenAI API key is not configured.")
            if not self.settings.model:
                return self._logged_error(request_id, prompt_version, "OpenAI model is not configured.")

            endpoint = (self.settings.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
            response = self.client.send(
                endpoint=endpoint,
                headers={
                    "Authorization": "Bearer {}".format(self.settings.api_key),
                    "Content-Type": "application/json",
                },
                payload={
                    "model": self.settings.model,
                    "temperature": self.settings.temperature,
                    "messages": [
                        {"role": "system", "content": prompt["system_prompt"]},
                        {"role": "user", "content": prompt["user_prompt"]},
                    ],
                    "response_format": {"type": "json_object"},
                },
                timeout=self.settings.timeout_seconds,
            )
            if response.error:
                return self._logged_error(request_id, prompt_version, response.error, response.latency_ms)

            content = response.response_body["choices"][0]["message"]["content"]
            result = self.validator.validate(content).model_copy(
                update={
                    "provider": "openai",
                    "model": self.settings.model,
                    "latency_ms": response.latency_ms,
                }
            )
            if result.decision == "ERROR":
                self.logger.log_error(request_id, "openai", self.settings.model, prompt_version, response.latency_ms, result.summary)
                return result
            self.logger.log_response(request_id, "openai", self.settings.model, prompt_version, response.latency_ms, result)
            return result
        except Exception as exc:
            return self._logged_error(request_id, prompt_version, str(exc))

    def _logged_error(self, request_id: str, prompt_version: str, reason: str, latency_ms: float = 0) -> ReviewResult:
        result = self._error(reason, latency_ms)
        self.logger.log_error(request_id, "openai", self.settings.model, prompt_version, latency_ms, reason)
        return result

    def _error(self, reason: str, latency_ms: float = 0) -> ReviewResult:
        return ReviewResult(
            decision="ERROR",
            confidence=0,
            summary=reason,
            provider="openai",
            model=self.settings.model,
            latency_ms=latency_ms,
        )
