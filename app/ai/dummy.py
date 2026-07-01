from __future__ import annotations

from app.ai.base import AIReviewer
from app.ai.models import AIContext, ReviewResult


class DummyReviewer(AIReviewer):
    def analyze_signal(self, context: AIContext) -> ReviewResult:
        return ReviewResult(
            decision="SHADOW",
            confidence=0,
            summary="AI not configured.",
            provider="dummy",
        )
