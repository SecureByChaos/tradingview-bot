"""compute_efficiency_ratio -- a live, short-window read of whether price is
moving cleanly right now, as distinct from trend_duration_pct_of_session's
"how long has the overall bias held" (which can span the whole session).

ADX and Supertrend are both lagging by design (app/indicators.py's adx()
docstring) -- they can keep reading "trending" well after the last hour has
gone choppy. This is Kaufman's Efficiency Ratio: net displacement over the
window divided by the total bar-to-bar path length covering it. 1.0 = a
dead-straight move; near 0 = as much back-and-forth as net progress.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.market_context import CHOP_EFFICIENCY_LOOKBACK_BARS, compute_efficiency_ratio
from app.market_data import Bar


def _bars(closes: list[float], start: datetime) -> list[Bar]:
    return [
        Bar(ts_ist=start + timedelta(minutes=5 * i), open=c, high=c, low=c, close=c)
        for i, c in enumerate(closes)
    ]


def test_a_straight_move_reads_as_fully_efficient():
    start = datetime(2026, 8, 27, 9, 15)
    closes = [100 + i for i in range(CHOP_EFFICIENCY_LOOKBACK_BARS + 1)]  # strictly monotonic
    bars = _bars(closes, start)
    assert compute_efficiency_ratio(bars) == 1.0


def test_pure_back_and_forth_with_zero_net_progress_reads_as_zero():
    # Oscillates every bar and ends exactly where it started -- real chop,
    # not "no data": path_length is nonzero even though net_change is zero.
    start = datetime(2026, 8, 27, 9, 15)
    closes = [100.0]
    for i in range(CHOP_EFFICIENCY_LOOKBACK_BARS):
        closes.append(closes[-1] + (1 if i % 2 == 0 else -1))
    assert closes[0] == closes[-1]  # sanity: net change really is zero
    bars = _bars(closes, start)
    assert compute_efficiency_ratio(bars) == 0.0


def test_flat_bars_with_zero_movement_at_all_return_none_not_zero():
    # Distinct from the case above: nothing happened at all (path_length=0),
    # which is "no information", not "a real zero-efficiency reading".
    start = datetime(2026, 8, 27, 9, 15)
    closes = [100.0] * (CHOP_EFFICIENCY_LOOKBACK_BARS + 1)
    bars = _bars(closes, start)
    assert compute_efficiency_ratio(bars) is None


def test_insufficient_bars_returns_none():
    start = datetime(2026, 8, 27, 9, 15)
    bars = _bars([100, 101, 102], start)  # far fewer than lookback + 1
    assert compute_efficiency_ratio(bars) is None


def test_hand_computed_mixed_case():
    # 3 bars up, 1 bar down, net +2 over a path length of 4+2+2+... let's be
    # concrete: closes [100, 101, 102, 103, 101] over a 4-bar lookback.
    # net_change = |101 - 100| = 1
    # path_length = |101-100| + |102-101| + |103-102| + |101-103| = 1+1+1+2 = 5
    # ER = 1 / 5 = 0.2
    start = datetime(2026, 8, 27, 9, 15)
    closes = [100.0, 101.0, 102.0, 103.0, 101.0]
    bars = _bars(closes, start)
    assert compute_efficiency_ratio(bars, lookback=4) == 0.2


def test_only_the_recent_window_is_considered():
    # A long choppy history followed by a clean recent move must read as
    # efficient -- earlier bars outside the lookback window must not dilute
    # the reading. This is the entire point of using a short window instead
    # of the whole trend-duration span. Disjoint value ranges (100s vs 200s)
    # so the two segments can't accidentally overlap into one another.
    start = datetime(2026, 8, 27, 9, 15)
    choppy_history = [100.0, 105.0, 98.0, 106.0, 97.0, 104.0]  # noisy, entirely before the window
    clean_recent = [200 + i for i in range(CHOP_EFFICIENCY_LOOKBACK_BARS + 1)]  # straight, exactly the window
    bars = _bars(choppy_history + clean_recent, start)
    assert compute_efficiency_ratio(bars) == 1.0


def test_rounds_to_three_decimal_places():
    start = datetime(2026, 8, 27, 9, 15)
    closes = [100.0, 101.0, 102.0]
    bars = _bars(closes, start)
    # net=2, path=1+1=2 -> exactly 1.0, not a useful rounding case on its own,
    # so use a lookback that produces a repeating decimal instead.
    closes = [100.0, 101.0, 99.0, 100.5]
    bars = _bars(closes, start)
    result = compute_efficiency_ratio(bars, lookback=3)
    assert result == round(result, 3)
