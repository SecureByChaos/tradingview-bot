from __future__ import annotations

import logging

import requests
from sqlalchemy.orm import Session

from app.platform import get_or_create_settings, log_event

logger = logging.getLogger(__name__)


class TelegramService:
    def send(self, db: Session, message: str) -> None:
        settings = get_or_create_settings(db)
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.exception("Telegram notification failed")
            log_event(db, "ERROR", "Telegram notification failed", "ERROR", {"error": str(exc)})
