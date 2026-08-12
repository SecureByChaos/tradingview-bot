from __future__ import annotations

from scripts.stall_exit_backtest import Replay, _sweep_peak_floors


def _replay(mfe_at_stall: float, actual: float, counterfactual: float) -> Replay:
    return Replay(
        trade_id="t", provider="claude", index_symbol="BANKNIFTY", entry_day="2026-08-12",
        actual_pnl_percent=actual, counterfactual_reason="STOPLOSS",
        counterfactual_pnl_percent=counterfactual, adx=None, cpr=None,
        minutes_held_after=30, mfe_at_stall_percent=mfe_at_stall,
    )


def test_sweep_exempts_only_trades_at_or_above_floor():
    replays = [
        _replay(mfe_at_stall=8.0, actual=3.0, counterfactual=6.0),   # above floor
        _replay(mfe_at_stall=2.0, actual=1.0, counterfactual=-5.0),  # below floor
    ]

    results = {r.floor: r for r in _sweep_peak_floors(replays, (5.0,))}

    assert results[5.0].n_exempt == 1
    assert results[5.0].n_total == 2
    assert results[5.0].exempt_mean_actual == 3.0
    assert results[5.0].exempt_mean_counterfactual == 6.0
    assert results[5.0].exempt_mean_delta == 3.0


def test_sweep_portfolio_mean_mixes_exempt_and_non_exempt_correctly():
    replays = [
        _replay(mfe_at_stall=8.0, actual=3.0, counterfactual=6.0),   # exempted -> counterfactual used
        _replay(mfe_at_stall=2.0, actual=1.0, counterfactual=-5.0),  # not exempted -> actual used
    ]

    result = _sweep_peak_floors(replays, (5.0,))[0]

    # baseline is mean(actual) = (3.0 + 1.0) / 2 = 2.0
    assert result.portfolio_mean_baseline == 2.0
    # if adopted: exempted trade uses counterfactual (6.0), other keeps actual (1.0) -> mean 3.5
    assert result.portfolio_mean_if_adopted == 3.5
    assert abs(result.portfolio_delta - 1.5) < 1e-9


def test_sweep_floor_of_zero_exempts_everyone():
    replays = [_replay(mfe_at_stall=0.5, actual=1.0, counterfactual=2.0)]
    result = _sweep_peak_floors(replays, (0.0,))[0]
    assert result.n_exempt == 1
    assert result.portfolio_mean_if_adopted == 2.0


def test_sweep_floor_above_all_peaks_exempts_no_one():
    replays = [_replay(mfe_at_stall=3.0, actual=1.0, counterfactual=2.0)]
    result = _sweep_peak_floors(replays, (99.0,))[0]
    assert result.n_exempt == 0
    assert result.exempt_mean_actual == 0.0
    assert result.portfolio_mean_if_adopted == result.portfolio_mean_baseline


def test_sweep_reports_full_surface_for_every_requested_floor():
    replays = [_replay(mfe_at_stall=5.0, actual=1.0, counterfactual=2.0)]
    floors = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    results = _sweep_peak_floors(replays, floors)
    assert [r.floor for r in results] == list(floors)
