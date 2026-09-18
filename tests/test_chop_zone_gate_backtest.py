"""Does Autonomous AI's CHOP_ZONE session-phase gate block good setups along
with bad ones? See scripts/chop_zone_gate_backtest.py's own module docstring
for the full trigger and question -- 17 Sep 2026, prompted by a real Nifty
rally that never once cleared both of Autonomous AI's deterministic
pre-model gates (the ADX floor, then the CHOP_ZONE block) during its entire
visible move.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np

from app.market_data import Bar
from scripts.backtest.data import build_arrays
from scripts.chop_zone_gate_backtest import (
    ADX_HARD_FLOOR,
    AFTERNOON_TREND_START,
    CHOP_ZONE_START,
    TRADING_END,
    TRADING_START,
    _adx_floor_eligible,
    _edge_index,
    _session_phase_buckets,
    _trend_direction,
    _verdict,
)


def _make_bars(num_sessions: int, bars_per_session: int = 78) -> list[Bar]:
    """5-min bars from 09:15 IST, one session per day, with a small
    deterministic oscillation so ATR/ADX/EMA all warm to real values --
    same fixture shape tests/test_adx_gate_backtest.py already established."""
    rng = np.random.default_rng(20260917)
    bars: list[Bar] = []
    price = 24000.0
    start_date = datetime(2026, 1, 5)  # a Monday
    for session in range(num_sessions):
        ts = start_date + timedelta(days=session, hours=9, minutes=15)
        for i in range(bars_per_session):
            move = rng.normal(0, 8.0)
            price = max(price + move, 100.0)
            high = price + abs(rng.normal(0, 3.0))
            low = price - abs(rng.normal(0, 3.0))
            bars.append(Bar(ts_ist=ts + timedelta(minutes=5 * i), open=price, high=high, low=low, close=price))
    return bars


def test_boundary_constants_match_app_ai_autonomous_exactly():
    # These are transcribed by hand from app/ai/autonomous.py -- pinned here
    # so a future change to the live gate's boundaries doesn't silently
    # desync this backtest from the thing it's meant to be testing.
    assert ADX_HARD_FLOOR == 18.0
    assert TRADING_START == time(9, 45)
    assert TRADING_END == time(15, 0)
    assert CHOP_ZONE_START == time(11, 15)
    assert AFTERNOON_TREND_START == time(13, 30)


def test_session_phase_buckets_partition_the_trading_window_exactly():
    bars = _make_bars(num_sessions=2)
    arrays = build_arrays("NIFTY", bars)

    is_chop_zone, is_open_window = _session_phase_buckets(arrays)

    times = arrays.ts.astype("datetime64[m]").astype(object)
    for i, dt in enumerate(times):
        t = dt.time()
        if t < TRADING_START or t > TRADING_END:
            assert not is_chop_zone[i] and not is_open_window[i], f"bar at {t} outside trading window must be in neither bucket"
        elif CHOP_ZONE_START <= t < AFTERNOON_TREND_START:
            assert is_chop_zone[i] and not is_open_window[i]
        else:
            assert is_open_window[i] and not is_chop_zone[i]

    # The two in-window buckets are mutually exclusive and jointly exhaustive
    # of the trading window -- no bar is double-counted or dropped.
    assert not np.any(is_chop_zone & is_open_window)


def test_session_phase_buckets_exclude_pre_open_and_post_square_off():
    bars = _make_bars(num_sessions=2)
    arrays = build_arrays("NIFTY", bars)
    is_chop_zone, is_open_window = _session_phase_buckets(arrays)

    times = arrays.ts.astype("datetime64[m]").astype(object)
    opening_bar = next(i for i, dt in enumerate(times) if dt.time() == time(9, 15))
    late_bar = next((i for i, dt in enumerate(times) if dt.time() >= time(15, 5)), None)

    assert not is_chop_zone[opening_bar] and not is_open_window[opening_bar]
    if late_bar is not None:
        assert not is_chop_zone[late_bar] and not is_open_window[late_bar]


def test_trend_direction_mirrors_ema9_vs_ema21():
    bars = _make_bars(num_sessions=2)
    arrays = build_arrays("NIFTY", bars)

    direction = _trend_direction(arrays)

    warm = ~np.isnan(arrays.ema9) & ~np.isnan(arrays.ema21)
    idx = np.flatnonzero(warm)[:50]
    for i in idx:
        if arrays.ema9[i] > arrays.ema21[i]:
            assert direction[i] == 1
        elif arrays.ema9[i] < arrays.ema21[i]:
            assert direction[i] == -1
        else:
            assert direction[i] == 0


def test_trend_direction_is_zero_before_emas_warm_up():
    bars = _make_bars(num_sessions=1)
    arrays = build_arrays("NIFTY", bars)

    direction = _trend_direction(arrays)

    assert direction[0] == 0


def test_adx_floor_eligible_excludes_below_18_and_cold_bars():
    bars = _make_bars(num_sessions=2)
    arrays = build_arrays("NIFTY", bars)

    eligible = _adx_floor_eligible(arrays)

    assert not eligible[0]  # cold, ADX not warm yet
    warm = ~np.isnan(arrays.adx14)
    below_floor = warm & (arrays.adx14 < ADX_HARD_FLOOR)
    assert not np.any(eligible & below_floor)
    at_or_above = warm & (arrays.adx14 >= ADX_HARD_FLOOR)
    if np.any(at_or_above):
        assert np.array_equal(eligible, at_or_above)


def test_edge_index_matches_hand_computed_value():
    # 10 wins, 0 losses, all long, base rate 50% (5 up / 10) -> edge = +50pp
    assert _edge_index(wins=10.0, ups=5.0, longs=10.0, n=10.0) == 50.0


def test_edge_index_returns_zero_for_empty_population():
    assert _edge_index(wins=0.0, ups=0.0, longs=0.0, n=0.0) == 0.0


def test_verdict_positive_backwards_and_inconclusive():
    assert _verdict(ci_low=1.0, ci_high=5.0) == "POSITIVE"
    assert _verdict(ci_low=-5.0, ci_high=-1.0) == "BACKWARDS"
    assert _verdict(ci_low=-1.0, ci_high=1.0) == "-"
