from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db_models import AISettings


def get_settings(db: Session, settings_id: int = 1) -> Optional[AISettings]:
    return db.get(AISettings, settings_id)


def create_settings(db: Session, **values: Any) -> AISettings:
    settings = AISettings(**values)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def update_settings(db: Session, settings: AISettings, **values: Any) -> AISettings:
    for field, value in values.items():
        if not hasattr(AISettings, field):
            raise ValueError("Unknown AI setting: {}".format(field))
        setattr(settings, field, value)
    db.commit()
    db.refresh(settings)
    return settings


def delete_settings(db: Session, settings: AISettings) -> None:
    db.delete(settings)
    db.commit()
