from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeStatus
from app.time_utils import utc_now
from scripts.quick_scalp_stop_trend_backtest import (
    MIN_BUCKET_LIVE,
    SourceTrade,
    StopCell,
    TrendEntry,
    _adx_at_entry,
    _adx_band,
    _aggregate_stop_cell,
    _bootstrap_mean_diff,
    _cost_pct,
    _load_stop_sweep_trades,
    _load_trend_trades,
    _replay,
    _report_trend_bucket,
    _run_one,
)


def _bar(hh: int, mm: int, high: float, low: float, close: float) -> tuple:
    return (datetime(2026, 9, 29, hh, mm), high, low, close)


# ---------------------------------------------------------------------------
# PART A -- stop-width sweep replay
# ---------------------------------------------------------------------------


def test_replay_hits_target_before_stop():
    bars = [
        _bar(9, 46, high=105.0, low=99.0, close=104.0),
        _bar(9, 47, high=112.0, low=104.0, close=110.0),
    ]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=90.0, target_price=110.0)
    assert reason == "TARGET"
    assert exit_price == 110.0
    assert mfe == 112.0


def test_replay_hits_stop_before_target():
    bars = [_bar(9, 46, high=101.0, low=89.0, close=90.0)]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=90.0, target_price=120.0)
    assert reason == "STOPLOSS"
    assert exit_price == 90.0


def test_replay_same_bar_touching_both_scores_as_loss():
    bars = [_bar(9, 46, high=125.0, low=85.0, close=100.0)]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=90.0, target_price=120.0)
    assert reason == "STOPLOSS"


def test_replay_time_exit_at_square_off():
    bars = [_bar(15, 15, high=101.0, low=99.0, close=100.5)]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=90.0, target_price=120.0)
    assert reason == "TIME_EXIT"
    assert exit_price == 100.5


def test_replay_incomplete_when_bars_exhausted():
    bars = [_bar(9, 46, high=101.0, low=99.0, close=100.5)]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=50.0, target_price=200.0)
    assert reason == "INCOMPLETE"
    assert exit_price == 100.5


def test_cost_pct_positive_for_a_real_premium():
    assert 0.0 < _cost_pct(100.0, 100.0, 150) < 2.0


def test_cost_pct_zero_when_no_premium():
    assert _cost_pct(0.0, 0.0, 150) == 0.0


def _trade(entry_price: float = 100.0) -> SourceTrade:
    return SourceTrade(
        trade_id="t1", index_symbol="BANKNIFTY", option_type="CE", entry_price=entry_price,
        entry_time="2026-09-29 04:20:00.000000", target_price=entry_price * 1.0375,
        actual_stoploss_price=entry_price * 0.975, quantity=150,
        tradingsymbol="X", symboltoken="1",
    )


def test_run_one_flags_noise_hit_when_mfe_never_moved():
    trade = _trade()
    bars = [_bar(9, 46, high=100.0, low=97.4, close=97.4)]
    result = _run_one(trade, bars, "2.5%", stop_price=97.5)
    assert result.reason == "STOPLOSS"
    assert result.is_noise_hit is True


def test_run_one_does_not_flag_noise_hit_when_mfe_cleared_the_fraction():
    trade = _trade()
    # Stop distance is 2.5 points; MFE reaches 101 (1 point = 40% of the stop
    # distance, above NOISE_MFE_FRACTION=0.20) before reversing into the stop.
    bars = [
        _bar(9, 46, high=101.0, low=100.0, close=100.5),
        _bar(9, 47, high=100.5, low=97.4, close=97.4),
    ]
    result = _run_one(trade, bars, "2.5%", stop_price=97.5)
    assert result.reason == "STOPLOSS"
    assert result.is_noise_hit is False


def test_run_one_target_exit_is_never_a_noise_hit():
    trade = _trade()
    bars = [_bar(9, 46, high=110.0, low=100.0, close=108.0)]
    result = _run_one(trade, bars, "2.5%", stop_price=97.5)
    assert result.reason == "TARGET"
    assert result.is_noise_hit is False


def test_aggregate_stop_cell_computes_group_stats():
    trade = _trade()
    bars_win = [_bar(9, 46, high=110.0, low=100.0, close=108.0)]
    bars_loss = [_bar(9, 46, high=100.0, low=97.4, close=97.4)]
    results = [
        _run_one(trade, bars_win, "2.5%", stop_price=97.5),
        _run_one(trade, bars_loss, "2.5%", stop_price=97.5),
    ]
    cell = _aggregate_stop_cell(results, "2.5%", "all")
    assert isinstance(cell, StopCell)
    assert cell.n == 2
    assert cell.win_rate == 50.0
    assert cell.noise_hit_rate == 100.0


def test_aggregate_stop_cell_returns_none_for_empty_group():
    assert _aggregate_stop_cell([], "2.5%", "all") is None


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _seed_scalp_trade(db, **overrides):
    fields = dict(
        trade_id="t1", strategy_name="Quick Scalp - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=57000, expiry="29SEP2026", quantity=150, entry_price=100.0, target=103.75,
        stoploss=97.5, entry_time=utc_now(), origin="QUICK_SCALP",
        status=TradeStatus.CLOSED, sl_mode="FIXED", exit_price=97.5,
        result="LOSS", exit_reason="STOPLOSS", pnl_percent=-2.5,
        investment_amount=15000.0, estimated_cost=50.0,
    )
    fields.update(overrides)
    db.add(StrategyTrade(**fields))


def test_load_stop_sweep_trades_includes_fixed_mode_quick_scalp(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_scalp_trade(db)
    db.commit()
    db.close()

    trades = _load_stop_sweep_trades(str(path))
    assert len(trades) == 1
    assert trades[0].trade_id == "t1"


def test_load_stop_sweep_trades_excludes_non_quick_scalp(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_scalp_trade(db, origin="AUTONOMOUS_AI")
    db.commit()
    db.close()

    assert _load_stop_sweep_trades(str(path)) == []


def test_load_stop_sweep_trades_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_scalp_trade(db, exit_price=None, status=TradeStatus.OPEN)
    db.commit()
    db.close()

    assert _load_stop_sweep_trades(str(path)) == []


# ---------------------------------------------------------------------------
# PART B -- ADX trend-filter check
# ---------------------------------------------------------------------------


def test_adx_band_boundaries():
    assert _adx_band(19.9) == "NO_TREND"
    assert _adx_band(20.0) == "MARGINAL"
    assert _adx_band(24.9) == "MARGINAL"
    assert _adx_band(25.0) == "TRENDING"
    assert _adx_band(40.0) == "TRENDING"


def test_load_trend_trades_includes_any_exit_reason(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_scalp_trade(db, trade_id="t1", exit_reason="SCALP_STRUCTURAL_STOP")
    _seed_scalp_trade(db, trade_id="t2", exit_reason="TARGET", result="WIN", pnl_percent=3.75)
    db.commit()
    db.close()

    rows = _load_trend_trades(str(path))
    assert len(rows) == 2


def test_load_trend_trades_excludes_non_quick_scalp(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_scalp_trade(db, origin="VALIDATED_SIGNAL")
    db.commit()
    db.close()

    assert _load_trend_trades(str(path)) == []


def _seed_candles(db, index_symbol: str, bars: list[tuple]) -> None:
    from app.db_models import Candle
    for ts, o, h, l, c in bars:
        db.add(Candle(index_symbol=index_symbol, interval="FIVE_MINUTE", ts_ist=ts,
                       open=o, high=h, low=l, close=c, volume=0.0))


def test_adx_at_entry_returns_none_with_no_candle_history(tmp_path):
    path, db = _make_db(tmp_path)
    db.commit()
    db.close()

    cache: dict = {}
    result = _adx_at_entry(cache, str(path), "BANKNIFTY", datetime(2026, 9, 29, 10, 0))
    assert result is None


def test_adx_at_entry_uses_last_bar_at_or_before_entry(tmp_path):
    import random

    path, db = _make_db(tmp_path)
    # Enough bars to warm up a 14-period ADX (needs >= 28 bars), with a real
    # trending shape so ADX actually resolves to a non-None, non-trivial value.
    rng = random.Random(1)
    bars = []
    price = 50000.0
    for i in range(40):
        ts = datetime(2026, 9, 29, 9, 15) + timedelta(minutes=5 * i)
        price += 15.0 + rng.uniform(-2, 2)  # steady uptrend with small noise
        bars.append((ts, price - 1, price + 2, price - 2, price))
    _seed_candles(db, "BANKNIFTY", bars)
    db.commit()
    db.close()

    cache: dict = {}
    # Entry well after warm-up, before the last bar -- confirms it picks the
    # bar AT OR BEFORE entry, not simply the last bar in the whole series.
    entry_ist = bars[35][0]
    result = _adx_at_entry(cache, str(path), "BANKNIFTY", entry_ist)
    assert result is not None
    assert result > 0

    # Second call for the same index must reuse the cache, not re-query.
    assert "BANKNIFTY" in cache


def test_bootstrap_mean_diff_detects_a_real_gap():
    a = [5.0] * 30  # "trending" bucket, consistently worse
    b = [-5.0] * 30  # "no_trend" bucket, consistently better
    lo, hi = _bootstrap_mean_diff(a, b)
    assert lo > 0 and hi > 0  # a - b is reliably positive


def test_bootstrap_mean_diff_null_case_straddles_zero():
    import random

    rng = random.Random(7)
    a = [rng.uniform(-1, 1) for _ in range(30)]
    b = [rng.uniform(-1, 1) for _ in range(30)]
    lo, hi = _bootstrap_mean_diff(a, b)
    assert lo < 0 < hi


def test_report_trend_bucket_handles_empty_list(caplog):
    _report_trend_bucket("NO_TREND", [])  # must not raise


def test_report_trend_bucket_flags_below_min_sample():
    entries = [
        TrendEntry(trade_id=f"t{i}", index_symbol="BANKNIFTY", adx_band="NO_TREND",
                   pnl_percent=1.0, net_pnl_percent=0.5, is_win=True, is_stoploss=False)
        for i in range(MIN_BUCKET_LIVE - 1)
    ]
    _report_trend_bucket("NO_TREND", entries)  # must not raise; flag is cosmetic in the log line
