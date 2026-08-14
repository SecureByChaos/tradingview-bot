from __future__ import annotations

from datetime import datetime

from app.signal_validation import check_market_hours, trading_day_reason
from app.time_utils import IST


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_trading_day_reason_none_on_an_ordinary_weekday():
    assert trading_day_reason(_ist(2026, 8, 13)) is None  # a Thursday


def test_trading_day_reason_flags_saturday():
    reason = trading_day_reason(_ist(2026, 8, 15))  # a Saturday
    assert reason is not None
    assert "Saturday" in reason


def test_trading_day_reason_flags_sunday():
    reason = trading_day_reason(_ist(2026, 8, 16))  # a Sunday
    assert reason is not None
    assert "Sunday" in reason


def test_trading_day_reason_flags_known_2026_holiday():
    reason = trading_day_reason(_ist(2026, 1, 26))  # Republic Day, in NSE_HOLIDAYS
    assert reason is not None
    assert "holiday" in reason
    assert "2026-01-26" in reason


def test_trading_day_reason_unknown_year_skips_holiday_check():
    # NSE_HOLIDAYS only covers 2026 -- a future year with no entry must not be
    # treated as every day being a holiday, or every day being fine is at
    # least the documented, safer failure mode (false negative, not false
    # positive) per the module's own comment.
    reason = trading_day_reason(_ist(2027, 1, 26))  # a Tuesday, no 2027 calendar
    assert reason is None


def test_check_market_hours_reproduces_original_weekday_wording():
    # Exact wording preserved across the trading_day_reason refactor --
    # nothing downstream should see this string change.
    reason = check_market_hours(_ist(2026, 8, 15))
    assert reason == "Signal received on a Saturday (market closed)"


def test_check_market_hours_reproduces_original_holiday_wording():
    reason = check_market_hours(_ist(2026, 1, 26))
    assert reason == "Signal received on an NSE trading holiday (2026-01-26)"


def test_check_market_hours_still_flags_outside_intraday_window_on_a_weekday():
    reason = check_market_hours(_ist(2026, 8, 13, 20, 0))  # Thursday evening
    assert reason is not None
    assert "outside NSE trading hours" in reason


def test_check_market_hours_allows_an_ordinary_trading_moment():
    assert check_market_hours(_ist(2026, 8, 13, 11, 0)) is None
