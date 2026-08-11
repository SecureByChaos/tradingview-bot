from __future__ import annotations

import numpy as np

from scripts.backtest.data import IndexArrays
from scripts.trend_age_gate_backtest import (
    _run_family,
    _same_direction_count_today,
    _split_masks,
    _trend_duration_pct,
)

NAN = float("nan")


def _make_arrays(n: int, st_5m_dir: list[int], session_id: list[int]) -> IndexArrays:
    zeros = np.zeros(n, dtype=np.float32)
    ones = np.ones(n, dtype=np.float32)
    nans = np.full(n, NAN, dtype=np.float32)
    zeros_i8 = np.zeros(n, dtype=np.int8)
    zeros_i32 = np.zeros(n, dtype=np.int32)
    close = 100.0 + np.arange(n, dtype=np.float32)

    return IndexArrays(
        index_symbol="TEST",
        # Starts at TRADING_START (09:45) so every bar in a test array falls
        # inside the eligible window regardless of n.
        ts=(np.datetime64("2026-08-10T09:45") + np.arange(n)).astype("datetime64[m]"),
        session_id=np.array(session_id, dtype=np.int32),
        open=close.copy(), high=close.copy(), low=close.copy(), close=close,
        volume=zeros,
        ema9=nans.copy(), ema20=nans.copy(), ema21=ones.copy(), ema50=nans.copy(),
        htf_ema20=nans.copy(), htf_ema50=nans.copy(), htf_ema9=nans.copy(), htf_ema21=nans.copy(),
        vwap=nans.copy(), atr14=ones.copy(), rsi14=nans.copy(), adx14=nans.copy(),
        st_5m_dir=np.array(st_5m_dir, dtype=np.int8), st_15m_dir=zeros_i8.copy(),
        or_high=nans.copy(), or_low=nans.copy(), pdh=nans.copy(), pdl=nans.copy(),
        prev_close=nans.copy(), cpr_width_pct=nans.copy(), extension_atr=nans.copy(),
        range_percentile=nans.copy(), minutes_since_open=zeros_i32.copy(),
        bars_held_above_or=zeros_i32.copy(), bars_held_below_or=zeros_i32.copy(),
    )


def test_trend_duration_pct_full_run_is_100_percent():
    arrays = _make_arrays(5, st_5m_dir=[1, 1, 1, 1, 1], session_id=[0, 0, 0, 0, 0])
    pct = _trend_duration_pct(arrays)
    assert np.allclose(pct, [100.0, 100.0, 100.0, 100.0, 100.0])


def test_trend_duration_pct_resets_on_direction_flip():
    arrays = _make_arrays(5, st_5m_dir=[1, 1, 1, -1, -1], session_id=[0, 0, 0, 0, 0])
    pct = _trend_duration_pct(arrays)
    # bar 3: direction just flipped, run=1 out of 4 bars elapsed -> 25%
    assert pct[3] == 25.0
    # bar 4: run=2 out of 5 bars elapsed -> 40%
    assert pct[4] == 40.0


def test_trend_duration_pct_resets_at_session_boundary():
    arrays = _make_arrays(4, st_5m_dir=[1, 1, 1, 1], session_id=[0, 0, 1, 1])
    pct = _trend_duration_pct(arrays)
    # session 1's first bar starts a fresh count regardless of session 0's
    # accumulated run length.
    assert pct[2] == 100.0
    assert pct[3] == 100.0


def test_trend_duration_pct_zero_when_no_trend():
    arrays = _make_arrays(3, st_5m_dir=[0, 0, 0], session_id=[0, 0, 0])
    pct = _trend_duration_pct(arrays)
    assert np.allclose(pct, [0.0, 0.0, 0.0])


def test_same_direction_count_today_tracks_each_direction_independently():
    session = np.array([0, 0, 0, 0], dtype=np.int32)
    direction = np.array([1, 1, -1, 1], dtype=np.int8)
    counts = _same_direction_count_today(session, direction)
    assert list(counts) == [0, 1, 0, 2]


def test_same_direction_count_today_resets_at_session_boundary():
    session = np.array([0, 0, 1, 1], dtype=np.int32)
    direction = np.array([1, 1, 1, 1], dtype=np.int8)
    counts = _same_direction_count_today(session, direction)
    assert list(counts) == [0, 1, 0, 1]


def test_same_direction_count_today_zero_direction_not_counted():
    session = np.array([0, 0, 0], dtype=np.int32)
    direction = np.array([1, 0, 1], dtype=np.int8)
    counts = _same_direction_count_today(session, direction)
    assert list(counts) == [0, 0, 1]


def test_split_masks_partitions_by_session_chronologically():
    arrays = _make_arrays(10, st_5m_dir=[1] * 10, session_id=[0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
    in_sample, out_of_sample = _split_masks(arrays, split_fraction=0.6)
    # 5 sessions, cutoff_idx = int(5*0.6) = 3 -> cutoff session = session 2
    assert list(in_sample) == [True, True, True, True, True, True, False, False, False, False]
    assert list(out_of_sample) == [False, False, False, False, False, False, True, True, True, True]


def test_split_masks_never_empty_with_small_fraction():
    arrays = _make_arrays(4, st_5m_dir=[1] * 4, session_id=[0, 1, 2, 3])
    in_sample, out_of_sample = _split_masks(arrays, split_fraction=0.01)
    assert in_sample.sum() >= 1  # max(int(n*fraction), 1) guard


def test_run_family_buckets_by_feature_threshold():
    # 80 bars, one session, alternating direction so every bar is a signal.
    # feature ramps 0..79 so a threshold of 40 cleanly splits ~40/40, both
    # comfortably clearing MIN_SIGNALS=30.
    n = 80
    arrays = _make_arrays(n, st_5m_dir=[1] * n, session_id=[0] * n)
    direction = np.array([1 if i % 2 == 0 else -1 for i in range(n)], dtype=np.int8)
    feature = np.arange(n, dtype=np.float64)
    eligible = np.ones(n, dtype=bool)
    in_sample = np.ones(n, dtype=bool)
    out_of_sample = np.zeros(n, dtype=bool)
    rng = np.random.default_rng(1)

    cells = _run_family(
        arrays, eligible, direction, feature, (40.0,), "test_family",
        in_sample, out_of_sample, "TEST", "TEST_SETUP", rng,
    )

    # out_of_sample is empty everywhere, so only in_sample cells survive
    # MIN_SIGNALS filtering -- one for "below", one for "at_or_above".
    assert {c.bucket for c in cells} == {"below", "at_or_above"}
    assert all(c.split == "in_sample" for c in cells)
    assert all(c.family == "test_family" for c in cells)
    assert all(c.n >= 30 for c in cells)
