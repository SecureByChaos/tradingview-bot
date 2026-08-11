from __future__ import annotations

import numpy as np
import pytest

from scripts.backtest.data import IndexArrays
from scripts.backtest.setups import LONG, SHORT, Setup, assert_causal, build_signals

NAN = float("nan")


def _make_arrays(ema9, ema21, rsi14, session_id, close=None) -> IndexArrays:
    """Minimal IndexArrays for exercising EMA_RSI_CROSS in isolation --
    every other field is a harmless placeholder of the right shape/dtype."""
    n = len(ema9)
    ema9 = np.array(ema9, dtype=np.float32)
    ema21 = np.array(ema21, dtype=np.float32)
    rsi14 = np.array(rsi14, dtype=np.float32)
    session_id = np.array(session_id, dtype=np.int32)
    close = np.array(close if close is not None else range(1, n + 1), dtype=np.float32)
    zeros = np.zeros(n, dtype=np.float32)
    nans = np.full(n, NAN, dtype=np.float32)
    zeros_i8 = np.zeros(n, dtype=np.int8)
    zeros_i32 = np.zeros(n, dtype=np.int32)

    return IndexArrays(
        index_symbol="TEST",
        ts=np.array([f"2026-08-10T09:{15+i:02d}" for i in range(n)], dtype="datetime64[m]"),
        session_id=session_id,
        open=close.copy(), high=close.copy(), low=close.copy(), close=close,
        volume=zeros,
        ema9=ema9, ema20=nans.copy(), ema21=ema21, ema50=nans.copy(),
        htf_ema20=nans.copy(), htf_ema50=nans.copy(), htf_ema9=nans.copy(), htf_ema21=nans.copy(),
        vwap=nans.copy(), atr14=nans.copy(), rsi14=rsi14, adx14=nans.copy(),
        st_5m_dir=zeros_i8.copy(), st_15m_dir=zeros_i8.copy(),
        or_high=nans.copy(), or_low=nans.copy(), pdh=nans.copy(), pdl=nans.copy(),
        prev_close=nans.copy(), cpr_width_pct=nans.copy(), extension_atr=nans.copy(),
        range_percentile=nans.copy(), minutes_since_open=zeros_i32.copy(),
        bars_held_above_or=zeros_i32.copy(), bars_held_below_or=zeros_i32.copy(),
    )


def test_no_signal_before_warmup():
    arrays = _make_arrays(
        ema9=[NAN, NAN, 10.0, 11.0],
        ema21=[NAN, NAN, 9.0, 9.5],
        rsi14=[NAN, NAN, 60.0, 60.0],
        session_id=[1, 1, 1, 1],
    )
    signals = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    assert signals[0] == 0
    assert signals[1] == 0
    assert_causal(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}), signals)


def test_bullish_cross_with_rsi_confirmation_fires_long():
    # bar0: ema9<ema21 (below). bar1: ema9>ema21 (crossed up), rsi>55 -> LONG.
    arrays = _make_arrays(
        ema9=[9.0, 10.0], ema21=[9.5, 9.5], rsi14=[50.0, 60.0], session_id=[1, 1],
    )
    signals = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    assert signals[0] == 0
    assert signals[1] == LONG


def test_bullish_cross_without_rsi_confirmation_does_not_fire():
    arrays = _make_arrays(
        ema9=[9.0, 10.0], ema21=[9.5, 9.5], rsi14=[50.0, 52.0], session_id=[1, 1],
    )
    signals = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    assert signals[1] == 0


def test_bearish_cross_with_rsi_confirmation_fires_short():
    arrays = _make_arrays(
        ema9=[10.0, 9.0], ema21=[9.5, 9.5], rsi14=[50.0, 40.0], session_id=[1, 1],
    )
    signals = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    assert signals[1] == SHORT


def test_already_above_does_not_refire_every_bar():
    # ema9 stays above ema21 for bars 1 and 2 -- only the actual crossover
    # bar (1) should fire, not bar 2 (already-crossed, not a new cross).
    arrays = _make_arrays(
        ema9=[9.0, 10.0, 10.5], ema21=[9.5, 9.5, 9.5], rsi14=[50.0, 60.0, 60.0], session_id=[1, 1, 1],
    )
    signals = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    assert signals[1] == LONG
    assert signals[2] == 0


def test_no_cross_at_session_boundary():
    # bar1 is the first bar of session 2; even though ema9/ema21 relationship
    # flipped vs bar0 (session 1), that must not read as a same-session cross.
    arrays = _make_arrays(
        ema9=[9.0, 10.0], ema21=[9.5, 9.5], rsi14=[50.0, 60.0], session_id=[1, 2],
    )
    signals = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    assert signals[1] == 0


def test_entry_offset_shifts_signal_forward_one_bar():
    arrays = _make_arrays(
        ema9=[9.0, 10.0, 10.5], ema21=[9.5, 9.5, 9.5], rsi14=[50.0, 60.0, 60.0], session_id=[1, 1, 1],
        close=[100.0, 101.0, 102.0],
    )
    same_bar = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    next_bar = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 1}))
    assert same_bar[1] == LONG and same_bar[2] == 0
    assert next_bar[1] == 0 and next_bar[2] == LONG


def test_entry_offset_drops_signal_crossing_session_boundary():
    # Cross detected on the last bar of session 1; shifting it into session
    # 2's first bar must be dropped, not silently carried across.
    arrays = _make_arrays(
        ema9=[9.0, 10.0, 10.5], ema21=[9.5, 9.5, 9.5], rsi14=[50.0, 60.0, 60.0], session_id=[1, 1, 2],
    )
    next_bar = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 1}))
    assert next_bar[2] == 0
    assert_causal(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 1}), next_bar)


def test_custom_rsi_thresholds_respected():
    arrays = _make_arrays(
        ema9=[9.0, 10.0], ema21=[9.5, 9.5], rsi14=[50.0, 58.0], session_id=[1, 1],
    )
    default = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0}))
    looser = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0, "rsi_bull": 55.0}))
    stricter = build_signals(arrays, Setup("EMA_RSI_CROSS", {"entry_offset": 0, "rsi_bull": 60.0}))
    assert default[1] == LONG  # default threshold 55, rsi=58 clears it
    assert looser[1] == LONG
    assert stricter[1] == 0    # rsi=58 does not clear 60
