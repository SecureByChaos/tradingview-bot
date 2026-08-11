from __future__ import annotations

import csv

from scripts.scalp_breakeven import _cost_pct, _forward_moves, compute_breakeven


def _write_contract(candle_dir, filename: str, rows: list[tuple[str, float]]) -> None:
    candle_dir.mkdir(parents=True, exist_ok=True)
    path = candle_dir / filename
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_ist", "open", "high", "low", "close", "volume"])
        for ts, price in rows:
            writer.writerow([ts, price, price, price, price, 0])


def test_forward_moves_matches_within_tolerance():
    series = [
        (__import__("datetime").datetime(2026, 8, 10, 9, 15, 0), 100.0),
        (__import__("datetime").datetime(2026, 8, 10, 9, 18, 0), 103.0),  # 3 min later
        (__import__("datetime").datetime(2026, 8, 10, 9, 25, 0), 90.0),   # far away, new entry point
    ]
    moves = _forward_moves(series, holding_minutes=3)
    assert moves == [3.0]  # |103-100|/100 * 100


def test_forward_moves_skips_unmatched_gap():
    series = [
        (__import__("datetime").datetime(2026, 8, 10, 9, 15, 0), 100.0),
        (__import__("datetime").datetime(2026, 8, 10, 9, 30, 0), 200.0),  # way outside tolerance for a 3-min hold
    ]
    assert _forward_moves(series, holding_minutes=3) == []


def test_forward_moves_non_overlapping():
    # Four evenly-spaced points, 3 minutes apart, holding period 3 min:
    # windows should NOT overlap -- entry at t0 consumes t0->t3, next entry
    # starts at t6 (the point after the matched t3), not at t1/t2.
    from datetime import datetime, timedelta
    base = datetime(2026, 8, 10, 9, 15, 0)
    series = [(base + timedelta(minutes=3 * i), 100.0 + i) for i in range(6)]
    moves = _forward_moves(series, holding_minutes=3)
    # 6 points at 3-min spacing -> windows (0->1),(2->3),(4->5): 3 non-overlapping moves
    assert len(moves) == 3


def test_cost_pct_positive_for_real_premium():
    assert 0.0 < _cost_pct(100.0, 35) < 2.0


def test_compute_breakeven_reports_full_clear_for_large_moves(tmp_path):
    candle_dir = tmp_path / "option_candles"
    # Every 3-minute window moves 50% -- should clear breakeven cost easily.
    rows = [
        ("2026-08-10T09:15:00", 100.0),
        ("2026-08-10T09:18:00", 150.0),
        ("2026-08-10T09:21:00", 100.0),
        ("2026-08-10T09:24:00", 150.0),
    ]
    _write_contract(candle_dir, "BANKNIFTY28AUG2655900CE_1.csv", rows)

    results = compute_breakeven(candle_dir, [3])
    assert len(results) == 1
    r = results[0]
    assert r.index_symbol == "BANKNIFTY"
    assert r.n_windows == 2
    assert r.fraction_clearing_breakeven == 1.0
    assert r.median_abs_move_pct == 50.0


def test_compute_breakeven_reports_zero_clear_for_tiny_moves(tmp_path):
    candle_dir = tmp_path / "option_candles"
    rows = [
        ("2026-08-10T09:15:00", 100.0),
        ("2026-08-10T09:18:00", 100.01),
        ("2026-08-10T09:21:00", 100.0),
        ("2026-08-10T09:24:00", 100.01),
    ]
    _write_contract(candle_dir, "BANKNIFTY28AUG2655900CE_1.csv", rows)

    results = compute_breakeven(candle_dir, [3])
    r = results[0]
    assert r.fraction_clearing_breakeven == 0.0
    assert r.breakeven_move_pct > 0.0


def test_compute_breakeven_separates_indexes(tmp_path):
    candle_dir = tmp_path / "option_candles"
    _write_contract(candle_dir, "BANKNIFTY28AUG2655900CE_1.csv", [
        ("2026-08-10T09:15:00", 100.0), ("2026-08-10T09:18:00", 105.0),
    ])
    _write_contract(candle_dir, "NIFTY28AUG2624500CE_2.csv", [
        ("2026-08-10T09:15:00", 50.0), ("2026-08-10T09:18:00", 52.0),
    ])

    results = {r.index_symbol: r for r in compute_breakeven(candle_dir, [3])}
    assert set(results) == {"BANKNIFTY", "NIFTY"}
    assert results["BANKNIFTY"].n_contracts == 1
    assert results["NIFTY"].n_contracts == 1


def test_compute_breakeven_missing_archive_raises(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        compute_breakeven(tmp_path / "does_not_exist", [3])
