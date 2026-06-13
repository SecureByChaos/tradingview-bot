from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_ist(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def format_ist(value: datetime | None) -> str:
    ist_value = to_ist(value)
    if ist_value is None:
        return ""
    return ist_value.strftime("%d-%b-%Y %I:%M %p IST")


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def duration_label(start: datetime | None, end: datetime | None = None) -> str:
    if start is None:
        return ""
    start_ist = to_ist(start)
    end_ist = to_ist(end) or datetime.now(IST)
    if start_ist is None:
        return ""
    seconds = max(int((end_ist - start_ist).total_seconds()), 0)
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"
