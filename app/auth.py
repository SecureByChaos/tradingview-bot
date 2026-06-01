from __future__ import annotations

import logging

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.config import get_settings

logger = logging.getLogger(__name__)


def verify_password(candidate: str, stored: str) -> bool:
    if not stored:
        return False
    raw = candidate.encode("utf-8")
    saved = stored.encode("utf-8")
    if stored.startswith("$2"):
        return bcrypt.checkpw(raw, saved)
    logger.warning("ADMIN_PASSWORD is not a bcrypt hash; use a bcrypt hash in production")
    hashed = bcrypt.hashpw(stored.encode("utf-8"), bcrypt.gensalt())
    return bcrypt.checkpw(raw, hashed)


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("admin_authenticated"))


def require_admin_page(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )


def require_admin_api(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def authenticate_admin(username: str, password: str) -> bool:
    settings = get_settings()
    return username == settings.admin_username and verify_password(password, settings.admin_password)
