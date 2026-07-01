from __future__ import annotations

from typing import Callable, Dict, Optional

from app.ai.base import AIReviewer
from app.ai.dummy import DummyReviewer
from app.ai.openai import OpenAIReviewer
from app.db_models import AISettings

ReviewerFactory = Callable[..., AIReviewer]
_PROVIDERS: Dict[str, ReviewerFactory] = {
    "dummy": lambda **_: DummyReviewer(),
    "openai": OpenAIReviewer,
}


def register_provider(name: str, factory: ReviewerFactory) -> None:
    _PROVIDERS[name.strip().lower()] = factory


def create_reviewer(settings: Optional[AISettings] = None, **kwargs: object) -> AIReviewer:
    provider = settings.provider if settings is not None else "dummy"
    factory = _PROVIDERS.get(provider.strip().lower(), DummyReviewer)
    if factory is DummyReviewer:
        return DummyReviewer()
    return factory(settings=settings, **kwargs)
