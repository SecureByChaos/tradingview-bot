from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeStatus
from app.time_utils import utc_now
from scripts.stop_distance_backtest import (
    NOISE_MFE_FRACTION,
    Cell,
    SourceTrade,
    _aggregate,
    _cost_pct,
    _load_trades,
    _replay,
    _run_one,
)


def _bar(hh: int, mm: int, high: float, low: float, close: float) -> tuple:
    return (datetime(2026, 8, 12, hh, mm), high, low, close)


def test_replay_hits_target_before_stop():
    bars = [
        _bar(9, 46, high=105.0, low=99.0, close=104.0),
        _bar(9, 47, high=112.0, low=104.0, close=110.0),  # clears target 110
    ]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=90.0, target_price=110.0)
    assert reason == "TARGET"
    assert exit_price == 110.0
    assert mfe == 112.0


def test_replay_hits_stop_before_target():
    bars = [
        _bar(9, 46, high=101.0, low=89.0, close=90.0),  # low breaches stop 90
    ]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=90.0, target_price=120.0)
    assert reason == "STOPLOSS"
    assert exit_price == 90.0


def test_replay_same_bar_touching_both_scores_as_loss():
    # Pessimistic intrabar ordering: stop checked before target.
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


def test_replay_tracks_running_max_high_as_mfe_even_on_a_loss():
    bars = [
        _bar(9, 46, high=108.0, low=100.0, close=107.0),  # ran up first
        _bar(9, 47, high=107.0, low=89.0, close=90.0),    # then reversed into the stop
    ]
    reason, exit_price, mfe = _replay(bars, entry_price=100.0, stop_price=90.0, target_price=120.0)
    assert reason == "STOPLOSS"
    assert mfe == 108.0


def test_cost_pct_positive_for_a_real_premium():
    assert 0.0 < _cost_pct(100.0, 100.0, 35) < 2.0


def test_cost_pct_zero_when_no_premium():
    assert _cost_pct(0.0, 0.0, 35) == 0.0


def _trade(entry_price: float = 100.0) -> SourceTrade:
    return SourceTrade(
        trade_id="t1", index_symbol="BANKNIFTY", option_type="CE", entry_price=entry_price,
        entry_time="2026-08-12 04:20:00.000000", target_price=entry_price * 1.2,
        actual_stoploss_price=entry_price * 0.9, quantity=35,
        tradingsymbol="X", symboltoken="1",
    )


def test_run_one_flags_noise_hit_when_mfe_never_moved():
    trade = _trade()
    # Stop at 90 (10% distance = 10 points); MFE never exceeds entry, so the
    # stop-out never showed any favorable movement at all -- textbook noise hit.
    bars = [_bar(9, 46, high=100.0, low=89.0, close=90.0)]
    result = _run_one(trade, bars, "10%", stop_price=90.0)
    assert result.reason == "STOPLOSS"
    assert result.is_noise_hit is True


def test_run_one_does_not_flag_noise_hit_when_mfe_cleared_the_fraction():
    trade = _trade()
    # Stop distance is 10 points; MFE reaches 105 (5 points = 50% of the stop
    # distance, well above NOISE_MFE_FRACTION=0.20) before reversing into the stop.
    bars = [
        _bar(9, 46, high=105.0, low=100.0, close=104.0),
        _bar(9, 47, high=104.0, low=89.0, close=90.0),
    ]
    result = _run_one(trade, bars, "10%", stop_price=90.0)
    assert result.reason == "STOPLOSS"
    assert result.is_noise_hit is False


def test_run_one_target_exit_is_never_a_noise_hit():
    trade = _trade()
    bars = [_bar(9, 46, high=125.0, low=100.0, close=121.0)]
    result = _run_one(trade, bars, "10%", stop_price=90.0)
    assert result.reason == "TARGET"
    assert result.is_noise_hit is False


def test_aggregate_computes_group_stats():
    trade = _trade()
    bars_win = [_bar(9, 46, high=125.0, low=100.0, close=121.0)]
    bars_loss = [_bar(9, 46, high=100.0, low=89.0, close=90.0)]
    results = [
        _run_one(trade, bars_win, "10%", stop_price=90.0),
        _run_one(trade, bars_loss, "10%", stop_price=90.0),
    ]
    cell = _aggregate(results, "CE", "10%", "all")
    assert isinstance(cell, Cell)
    assert cell.n == 2
    assert cell.win_rate == 50.0
    assert cell.noise_hit_rate == 100.0  # the one STOPLOSS in the group was a noise hit


def test_aggregate_returns_none_for_empty_group():
    assert _aggregate([], "CE", "10%", "all") is None


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _seed_trade(db, **overrides):
    fields = dict(
        trade_id="t1", strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=57000, expiry="28AUG2026", quantity=35, entry_price=100.0, target=120.0,
        stoploss=90.0, entry_time=utc_now(), origin="AI_ORIGIN_CLAUDE",
        status=TradeStatus.CLOSED, sl_mode="FIXED", exit_price=110.0,
    )
    fields.update(overrides)
    db.add(StrategyTrade(**fields))


def test_load_trades_includes_fixed_mode_ai_origination_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db)
    db.commit()
    db.close()

    trades = _load_trades(str(path))
    assert len(trades) == 1
    assert trades[0].trade_id == "t1"


def test_load_trades_excludes_trailing_mode(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, sl_mode="TRAILING")
    db.commit()
    db.close()

    assert _load_trades(str(path)) == []


def test_load_trades_excludes_non_ai_origination(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, origin="SIGNAL")
    db.commit()
    db.close()

    assert _load_trades(str(path)) == []


def test_load_trades_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, exit_price=None)
    db.commit()
    db.close()

    assert _load_trades(str(path)) == []
