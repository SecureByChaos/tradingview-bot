from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.ai.context_builder import SignalContextBuilder
from app.ai.factory import create_reviewer
from app.ai.prompt_builder import PromptBuilder
from app.ai.repository import get_settings
from app.ai.review_repository import save_review
from app.ai.validator import AIResponseValidator
from app.database import SessionLocal

logger = logging.getLogger(__name__)
PROMPT_VERSION = "v1"
CONTEXT_VERSION = "v1"
FRAMEWORK_VERSION = "v1"


def run_shadow_review(strategy: str, signal: str, timestamp: datetime, trade_id: Optional[str]) -> None:
    try:
        with SessionLocal() as db:
            settings = get_settings(db)
            if settings is None or not settings.enabled or settings.mode != "SHADOW":
                return
            context = SignalContextBuilder().build(strategy, signal, timestamp)
            PromptBuilder().build_signal_prompt(context, settings.system_prompt, PROMPT_VERSION)
            provider_result = create_reviewer(settings).analyze_signal(context)
            result = AIResponseValidator().validate(provider_result.model_dump()).model_copy(
                update={
                    "provider": provider_result.provider,
                    "model": provider_result.model,
                    "latency_ms": provider_result.latency_ms,
                }
            )
            if result.decision == "ERROR":
                logger.error("AI shadow review failed: %s", result.summary)
            save_review(
                db,
                trade_id,
                strategy,
                signal,
                result,
                PROMPT_VERSION,
                CONTEXT_VERSION,
                FRAMEWORK_VERSION,
            )
    except Exception:
        logger.exception("AI shadow review failed")
