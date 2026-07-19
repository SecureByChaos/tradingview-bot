from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.time_utils import IST, to_ist

# NSE/BSE weekday trading holidays for 2026 (source: exchange holiday
# calendar, checked 19 Jul 2026). This list needs a yearly refresh -- if the
# current year isn't covered, the holiday portion of the check is skipped
# rather than guessed, since a log-only false negative is far safer than a
# false positive here.
NSE_HOLIDAYS = {
    2026: {
        "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
        "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
        "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
        "2026-12-25",
    },
}

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


def check_market_hours(timestamp: datetime) -> str | None:
    """Independent of whatever session TradingView's own chart thinks it's in --
    flags any signal that arrives outside real NSE trading hours/days."""
    ist = to_ist(timestamp)
    if ist is None:
        return None
    if ist.weekday() >= 5:
        return f"Signal received on a {ist:%A} (market closed)"
    holidays = NSE_HOLIDAYS.get(ist.year)
    if holidays is not None and ist.date().isoformat() in holidays:
        return f"Signal received on an NSE trading holiday ({ist.date().isoformat()})"
    open_time = ist.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_time = ist.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    if not (open_time <= ist <= close_time):
        return f"Signal received outside NSE trading hours ({ist:%H:%M} IST)"
    return None


def _parse_timestamp(raw: Any) -> datetime | None:
    try:
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 10**12:
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=IST)
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            if text.replace(".", "", 1).isdigit():
                return _parse_timestamp(float(text))
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return None


def check_webhook_staleness(
    market_data: dict[str, Any] | None,
    received_at: datetime,
    max_age_seconds: int = 120,
) -> str | None:
    """Flags a webhook whose own reported timestamp is old by the time we
    process it -- a sign of a delayed or retried TradingView alert firing on
    conditions that may no longer hold."""
    if not market_data:
        return None
    raw_ts = market_data.get("timestamp")
    if raw_ts is None:
        return None
    sent_at = _parse_timestamp(raw_ts)
    if sent_at is None:
        return None
    age = (to_ist(received_at) - to_ist(sent_at)).total_seconds()
    if age > max_age_seconds:
        return f"Webhook is {age:.0f}s old (> {max_age_seconds}s threshold) -- possible delayed/retried alert"
    return None


_DEDUPE_WINDOW_SECONDS = 10
_recent_signals: dict[str, datetime] = {}


def check_duplicate_signal(
    strategy_name: str,
    signal: str,
    market_data: dict[str, Any] | None,
    received_at: datetime,
) -> str | None:
    """In-process dedupe for exact-repeat webhook deliveries (TradingView is
    known to retry on its own network hiccups). Best-effort only -- resets on
    restart, which is fine since this is a freshness check, not a source of
    truth."""
    raw_ts = (market_data or {}).get("timestamp")
    fingerprint = f"{strategy_name}|{signal}|{raw_ts}"
    last_seen = _recent_signals.get(fingerprint)
    _recent_signals[fingerprint] = received_at
    if len(_recent_signals) > 500:
        cutoff = received_at - timedelta(seconds=_DEDUPE_WINDOW_SECONDS * 5)
        for key in [k for k, v in _recent_signals.items() if v < cutoff]:
            del _recent_signals[key]
    if last_seen is not None and raw_ts is not None and (received_at - last_seen).total_seconds() < _DEDUPE_WINDOW_SECONDS:
        return f"Duplicate signal within {_DEDUPE_WINDOW_SECONDS}s window (possible TradingView retry)"
    return None


def check_spot_price_deviation(
    claimed_price: float | None,
    real_price: float | None,
    max_deviation_percent: float = 0.5,
) -> str | None:
    """Compares TradingView's self-reported spot price against a fresh
    SmartAPI spot fetch. A large gap usually means the chart feed is lagging
    or the alert fired on stale data."""
    if claimed_price is None or real_price is None or real_price == 0:
        return None
    deviation = abs(claimed_price - real_price) / real_price * 100
    if deviation > max_deviation_percent:
        return (
            f"TradingView spot ({claimed_price}) vs SmartAPI spot ({real_price}) "
            f"differ by {deviation:.2f}% (> {max_deviation_percent}% threshold)"
        )
    return None


def check_premium_sanity(entry_price: float | None, min_premium: float = 0.5) -> str | None:
    """Flags a suspiciously cheap option premium -- usually a sign of a dead/
    near-expiry/deep-OTM contract that a strike-selection edge case picked up,
    and that would be near-impossible to fill in live mode."""
    if entry_price is None:
        return None
    if entry_price < min_premium:
        return f"Entry premium {entry_price} is below sanity floor of {min_premium} -- contract may be illiquid"
    return None
