"""Trend-age computation.

These four numbers exist to answer a question ADX and Supertrend structurally
cannot: not "is there a trend" but "how much of it is already spent". The
5 Aug 13:48 Nifty 24500 PE entries -- both providers, both -16% to -18%, at the
reversal of a multi-hour decline -- were taken on indicators that read exactly
the same as they had at 10:00.

The failure mode worth testing against is a plausible wrong number rather than
a crash. A trend age of 0 on a five-hour move, or a duration that keeps
counting across a Supertrend flip, would both render as ordinary prompt text
and quietly tell the model the opposite of the truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.indicators import SupertrendPoint
from app.market_context import compute_trend_age
from app.market_data import Bar


def _bars(closes: list[float], start: datetime) -> list[Bar]:
    return [
        Bar(ts_ist=start + timedelta(minutes=5 * i), open=c, high=c, low=c, close=c)
        for i, c in enumerate(closes)
    ]


def _st(directions: list[int]) -> list[SupertrendPoint | None]:
    return [SupertrendPoint(value=0.0, direction=d) for d in directions]


def test_counts_only_the_current_unbroken_run():
    """A flip resets the count. Carrying it across would describe a trend that
    has already ended as the longest-running one on the chart."""
    start = datetime(2026, 8, 5, 9, 15)
    directions = [1, 1, 1, -1, -1, -1, -1]
    bars, atr = _bars([100, 101, 102, 101, 100, 99, 98], start), 1.0
    duration, _, _ = compute_trend_age(bars, _st(directions), atr, start + timedelta(minutes=30))
    assert duration == 4


def test_move_extent_is_cumulative_travel_not_last_bar_position():
    """The distinction that makes this field worth having.

    distance_from_ema21_atr already reports where the last bar sits. This must
    report how far price has come since the trend began -- a trend can have run
    6 ATR and still sit near its own EMA once the average catches up.
    """
    start = datetime(2026, 8, 5, 9, 15)
    bars = _bars([100, 102, 104, 106, 108], start)
    _, _, move_atr = compute_trend_age(bars, _st([1] * 5), 2.0, start + timedelta(minutes=20))
    assert move_atr == 4.0  # (108 - 100) / 2.0


def test_move_extent_is_signed_by_trend_direction():
    """Positive always means "travelled as far as the trend claims".

    A negative value means the Supertrend direction and the actual price change
    disagree, which is information rather than a bug, and would be hidden by
    taking an absolute value.
    """
    start = datetime(2026, 8, 5, 9, 15)
    bars = _bars([108, 106, 104, 102, 100], start)
    _, _, move_atr = compute_trend_age(bars, _st([-1] * 5), 2.0, start + timedelta(minutes=20))
    assert move_atr == 4.0


def test_percent_of_session_uses_wall_clock_not_bar_count():
    """The bar list is a rolling window sized by a load limit, so its length
    says nothing about how much of the session has passed. Two hours into a
    move means something different at 10:00 than at 14:00, and that is the
    entire point of this field."""
    start = datetime(2026, 8, 5, 9, 15)
    bars = _bars([100 + i for i in range(12)], start)
    # 12 bars of trend, evaluated at 10:15 -- one hour in, so 12 five-minute
    # bars is the whole session so far.
    _, pct_early, _ = compute_trend_age(bars, _st([1] * 12), 1.0, datetime(2026, 8, 5, 10, 15))
    # Same 12 bars, evaluated at 14:15 -- five hours in, so far less of it.
    _, pct_late, _ = compute_trend_age(bars, _st([1] * 12), 1.0, datetime(2026, 8, 5, 14, 15))
    assert pct_early == 100.0
    assert pct_late is not None and pct_late < 25.0


def test_returns_none_rather_than_zero_when_undetermined():
    """Zero would read as "brand new trend", which is the opposite of unknown
    and the more dangerous of the two to get wrong in a prompt."""
    start = datetime(2026, 8, 5, 9, 15)
    assert compute_trend_age([], [], 1.0, start) == (None, None, None)
    assert compute_trend_age(_bars([100], start), [None], 1.0, start) == (None, None, None)


def test_missing_atr_omits_move_extent_but_keeps_duration():
    """Partial data yields the part that is known, not nothing. Duration does
    not depend on ATR and should survive its absence."""
    start = datetime(2026, 8, 5, 9, 15)
    bars = _bars([100, 101, 102], start)
    duration, _, move_atr = compute_trend_age(bars, _st([1, 1, 1]), None, start + timedelta(minutes=10))
    assert duration == 3
    assert move_atr is None
