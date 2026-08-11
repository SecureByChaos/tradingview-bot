from __future__ import annotations

import csv

import numpy as np

from scripts.backtest.data import IndexArrays
from scripts.backtest.setups import LONG, Setup
from scripts.scalp_stop_sweep import _cost_pct, _representative_premium, _sweep_one

NAN = float("nan")


def _make_arrays(close, high, low, ema9, ema21, rsi14, session_id) -> IndexArrays:
    n = len(close)
    close = np.array(close, dtype=np.float32)
    high = np.array(high, dtype=np.float32)
    low = np.array(low, dtype=np.float32)
    ema9 = np.array(ema9, dtype=np.float32)
    ema21 = np.array(ema21, dtype=np.float32)
    rsi14 = np.array(rsi14, dtype=np.float32)
    session_id = np.array(session_id, dtype=np.int32)
    zeros = np.zeros(n, dtype=np.float32)
    nans = np.full(n, NAN, dtype=np.float32)
    zeros_i8 = np.zeros(n, dtype=np.int8)
    zeros_i32 = np.zeros(n, dtype=np.int32)

    return IndexArrays(
        index_symbol="TEST",
        ts=np.array([f"2026-08-10T09:{15+i:02d}" if i < 45 else f"2026-08-10T10:{i-45:02d}" for i in range(n)], dtype="datetime64[m]"),
        session_id=session_id,
        open=close.copy(), high=high, low=low, close=close,
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


def test_cost_pct_matches_real_cost_model():
    # A representative ATM premium and a 35-lot contract -- cost_pct must be
    # a small positive percentage, roughly in the ~0.5-1% range this
    # codebase has repeatedly measured for round-trip costs.
    pct = _cost_pct(avg_premium=100.0, lot_size=35)
    assert 0.0 < pct < 2.0


def test_cost_pct_zero_when_no_premium_data():
    assert _cost_pct(avg_premium=0.0, lot_size=35) == 0.0


def test_representative_premium_reads_median_from_archive(tmp_path):
    candle_dir = tmp_path / "option_candles"
    candle_dir.mkdir()
    path = candle_dir / "BANKNIFTY28AUG2655900CE_12345.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_ist", "open", "high", "low", "close", "volume"])
        for i, price in enumerate([80.0, 100.0, 120.0]):
            writer.writerow([f"2026-08-10T09:{15+i:02d}:00", price, price, price, price, 0])

    result = _representative_premium(candle_dir, "BANKNIFTY")
    assert result == 100.0  # median of 80/100/120


def test_representative_premium_ignores_other_index(tmp_path):
    candle_dir = tmp_path / "option_candles"
    candle_dir.mkdir()
    path = candle_dir / "NIFTY28AUG2624500CE_99999.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_ist", "open", "high", "low", "close", "volume"])
        writer.writerow(["2026-08-10T09:15:00", 50.0, 50.0, 50.0, 50.0, 0])

    assert _representative_premium(candle_dir, "BANKNIFTY") == 0.0


def test_representative_premium_missing_dir_returns_zero(tmp_path):
    assert _representative_premium(tmp_path / "does_not_exist", "BANKNIFTY") == 0.0


def test_sweep_one_win_hits_target_before_stop():
    # Entry at bar 0 (close=100). By bar 2, high has risen 5% -- with
    # multiplier=1.0 that's a 5% premium move, clearing a 3% target well
    # before a 2% stop could ever trigger (low never drops).
    n = 10
    # The EMA_RSI_CROSS signal fires at bar 1 (ema9 crosses ema21 between
    # bars 0 and 1), so entry price is close[1], and the forward simulation
    # starts at bar 2 -- not bar 0.
    close = [100.0, 100.0] + [106.0] * (n - 2)
    high = [100.0, 100.0, 106.0] + [106.0] * (n - 3)
    low = [100.0, 100.0, 99.0] + [106.0] * (n - 3)
    arrays = _make_arrays(
        close=close, high=high, low=low,
        ema9=[9.0, 10.0] + [10.0] * (n - 2),
        ema21=[9.5, 9.5] + [9.5] * (n - 2),
        rsi14=[50.0, 60.0] + [60.0] * (n - 2),
        session_id=[1] * n,
    )
    setup = Setup("EMA_RSI_CROSS", {"entry_offset": 0})
    results = _sweep_one(
        arrays, np.ones(n, dtype=bool), setup, "BANKNIFTY",
        premium_multiplier=1.0, dte=6, cost_pct=0.6, holding_minutes=5, minutes_per_bar=1,
    )
    hit = [r for r in results if r.target_pct == 3.0 and r.stop_pct == 2.0][0]
    assert hit.n_trades == 1
    assert hit.win_rate == 1.0
    assert hit.mean_pnl_pct == 3.0  # capped at the target


def test_sweep_one_noise_stop_flagged_correctly():
    # Same entry-at-bar-1 mechanics as above. Price drifts DOWN immediately
    # from the entry price (close[1]=100) and never rises above it -- MFE
    # stays at zero, so the stop-out is a textbook noise hit (never moved
    # favorably at all before getting stopped).
    n = 10
    close = [100.0, 100.0] + [97.0] * (n - 2)
    high = [100.0, 100.0] + [100.0] * (n - 2)  # never exceeds entry -- MFE stays 0
    low = [100.0, 100.0, 97.5] + [97.0] * (n - 3)
    arrays = _make_arrays(
        close=close, high=high, low=low,
        ema9=[9.0, 10.0] + [10.0] * (n - 2),
        ema21=[9.5, 9.5] + [9.5] * (n - 2),
        rsi14=[50.0, 60.0] + [60.0] * (n - 2),
        session_id=[1] * n,
    )
    setup = Setup("EMA_RSI_CROSS", {"entry_offset": 0})
    results = _sweep_one(
        arrays, np.ones(n, dtype=bool), setup, "BANKNIFTY",
        premium_multiplier=1.0, dte=6, cost_pct=0.6, holding_minutes=5, minutes_per_bar=1,
    )
    hit = [r for r in results if r.target_pct == 3.0 and r.stop_pct == 2.0][0]
    assert hit.n_trades == 1
    assert hit.win_rate == 0.0
    assert hit.noise_hit_rate == 1.0  # the only stop-out never moved favorably


def test_sweep_one_no_signal_gives_zero_trades():
    n = 10
    arrays = _make_arrays(
        close=[100.0] * n, high=[100.0] * n, low=[100.0] * n,
        ema9=[NAN] * n, ema21=[NAN] * n, rsi14=[NAN] * n, session_id=[1] * n,
    )
    setup = Setup("EMA_RSI_CROSS", {"entry_offset": 0})
    results = _sweep_one(
        arrays, np.ones(n, dtype=bool), setup, "BANKNIFTY",
        premium_multiplier=1.0, dte=6, cost_pct=0.6, holding_minutes=5, minutes_per_bar=1,
    )
    assert all(r.n_trades == 0 for r in results)
    assert all(r.net_expectancy_pct == -r.cost_pct for r in results)
