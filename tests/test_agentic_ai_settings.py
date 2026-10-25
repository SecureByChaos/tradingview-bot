from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.agentic import create_agentic_settings, get_agentic_settings, update_agentic_settings
from app.dashboard_routes import update_agentic_ai_settings_page
from app.db_models import AgenticAISettings, Base


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def test_get_agentic_settings_returns_none_when_unconfigured():
    db = _make_session()
    assert get_agentic_settings(db) is None


def test_create_and_get_agentic_settings_round_trip():
    db = _make_session()
    created = create_agentic_settings(db, id=1, enabled=True, live_model="gpt-6-luna", qa_model="gpt-6-ultra")
    fetched = get_agentic_settings(db)
    assert fetched.id == created.id
    assert fetched.live_model == "gpt-6-luna"
    assert fetched.qa_model == "gpt-6-ultra"
    # Defaults persist as declared on the model, not silently blank.
    assert fetched.live_reasoning_effort == "medium"
    assert fetched.qa_reasoning_effort == "max"


def test_update_agentic_settings_changes_fields():
    db = _make_session()
    settings = create_agentic_settings(db, id=1)
    update_agentic_settings(db, settings, live_model="gpt-6-luna", qa_reasoning_effort="high")
    db.refresh(settings)
    assert settings.live_model == "gpt-6-luna"
    assert settings.qa_reasoning_effort == "high"


def test_update_agentic_settings_rejects_unknown_field():
    db = _make_session()
    settings = create_agentic_settings(db, id=1)
    with pytest.raises(ValueError):
        update_agentic_settings(db, settings, not_a_real_field=True)


def test_route_creates_settings_row_on_first_save():
    db = _make_session()
    update_agentic_ai_settings_page(
        db=db, live_model="gpt-6-luna", live_reasoning_effort="medium",
        qa_model="gpt-6-ultra", qa_reasoning_effort="max", base_url="", timeout_seconds=20,
        api_key="sk-real", enabled="on",
    )
    settings = get_agentic_settings(db)
    assert settings.enabled is True
    assert settings.live_model == "gpt-6-luna"
    assert settings.api_key == "sk-real"


def test_route_unchecked_enabled_persists_as_false():
    db = _make_session()
    update_agentic_ai_settings_page(
        db=db, live_model="", live_reasoning_effort="medium",
        qa_model="", qa_reasoning_effort="max", base_url="", timeout_seconds=20,
        enabled=None,
    )
    assert get_agentic_settings(db).enabled is False


def test_route_blank_api_key_does_not_overwrite_existing_key():
    db = _make_session()
    create_agentic_settings(db, id=1, api_key="sk-existing")
    update_agentic_ai_settings_page(
        db=db, live_model="gpt-6-luna", live_reasoning_effort="medium",
        qa_model="gpt-6-ultra", qa_reasoning_effort="max", base_url="", timeout_seconds=20,
        api_key="",
    )
    assert get_agentic_settings(db).api_key == "sk-existing"


def test_route_rejects_invalid_reasoning_effort():
    from fastapi import HTTPException

    db = _make_session()
    with pytest.raises(HTTPException):
        update_agentic_ai_settings_page(
            db=db, live_model="", live_reasoning_effort="not-a-real-level",
            qa_model="", qa_reasoning_effort="max", base_url="", timeout_seconds=20,
        )


def test_route_rejects_non_positive_timeout():
    from fastapi import HTTPException

    db = _make_session()
    with pytest.raises(HTTPException):
        update_agentic_ai_settings_page(
            db=db, live_model="", live_reasoning_effort="medium",
            qa_model="", qa_reasoning_effort="max", base_url="", timeout_seconds=0,
        )
