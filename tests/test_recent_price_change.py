"""compute_recent_price_change_percent -- a much shorter (~15 min) directional
read than compute_efficiency_ratio's ~1 hour window. Kaufman's Efficiency
Ratio can still read CLEAN/MIXED for a while after a reversal begins, because
the reversal hasn't yet consumed enough of the hour's total path length to
move the ratio. This answers a narrower, more current question: which way
has price actually moved in just the last few candles.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.market_context import RECENT_MOVE_LOOKBACK_BARS, compute_recent_price_change_percent
from app.market_data import Bar


def _bars(closes: list[float], start: datetime) -> list[Bar]:
    return [
        Bar(ts_ist=start + timedelta(minutes=5 * i), open=c, high=c, low=c, close=c)
        for i, c in enumerate(closes)
    ]


def test_rising_move_over_the_window_is_positive():
    start = datetime(2026, 9, 11, 9, 15)
    closes = [100.0, 101.0, 102.0, 105.0]  # exactly lookback + 1 = 4 bars
    bars = _bars(closes, start)
    assert compute_recent_price_change_percent(bars) == 5.0


def test_falling_move_over_the_window_is_negative():
    start = datetime(2026, 9, 11, 9, 15)
    closes = [100.0, 99.0, 98.0, 95.0]
    bars = _bars(closes, start)
    assert compute_recent_price_change_percent(bars) == -5.0


def test_only_the_recent_window_is_considered_not_a_longer_prior_trend():
    # A long prior decline followed by a short recent bounce must read
    # positive -- earlier bars outside the lookback must not mask the
    # reversal the way compute_efficiency_ratio's hourly window can.
    start = datetime(2026, 9, 11, 9, 15)
    long_decline = [200.0, 190.0, 180.0, 170.0, 160.0, 150.0, 140.0, 130.0, 120.0]
    recent_bounce = [120.0, 121.0, 122.0, 123.0]  # exactly the window, net up
    bars = _bars(long_decline + recent_bounce, start)
    result = compute_recent_price_change_percent(bars)
    assert result is not None
    assert result > 0


def test_insufficient_bars_returns_none():
    start = datetime(2026, 9, 11, 9, 15)
    bars = _bars([100.0, 101.0], start)  # fewer than lookback + 1
    assert compute_recent_price_change_percent(bars) is None


def test_zero_reference_close_returns_none_rather_than_dividing_by_zero():
    start = datetime(2026, 9, 11, 9, 15)
    closes = [0.0, 1.0, 2.0, 3.0]
    bars = _bars(closes, start)
    assert compute_recent_price_change_percent(bars) is None


def test_default_lookback_is_three_bars():
    assert RECENT_MOVE_LOOKBACK_BARS == 3


def test_custom_lookback_is_honoured():
    start = datetime(2026, 9, 11, 9, 15)
    closes = [100.0, 105.0, 90.0, 95.0, 99.0]
    # lookback=1 -> just the last two bars: (99-95)/95 * 100
    result = compute_recent_price_change_percent(_bars(closes, start), lookback=1)
    assert result == round((99.0 - 95.0) / 95.0 * 100, 3)


def test_rounds_to_three_decimal_places():
    start = datetime(2026, 9, 11, 9, 15)
    closes = [100.0, 101.0, 99.0, 100.7]
    result = compute_recent_price_change_percent(_bars(closes, start))
    assert result == round(result, 3)
