from __future__ import annotations

from abc import ABC, abstractmethod

from app.ai.models import AIContext, ReviewResult


class AIReviewer(ABC):
    @abstractmethod
    def analyze_signal(self, context: AIContext) -> ReviewResult:
        raise NotImplementedError
