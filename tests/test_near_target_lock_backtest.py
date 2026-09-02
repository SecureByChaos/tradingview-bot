from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, StrategyTradeTick, TradeStatus
from app.time_utils import utc_now
from scripts.near_target_lock_backtest import (
    MIN_TICKS_TO_REPLAY,
    Outcome,
    SourceTrade,
    _bootstrap_mean,
    _cost_pct,
    _load_trades,
    _load_ticks_by_trade,
    _replay_ticks,
    _run,
)


def test_replay_ticks_baseline_hits_target():
    reason, exit_price, mfe, activated = _replay_ticks(
        [102.0, 108.0, 121.0], entry_price=100.0, stop_price=90.0, target_price=120.0,
    )
    assert reason == "TARGET"
    assert exit_price == 120.0
    assert mfe == 121.0
    assert activated is False


def test_replay_ticks_baseline_hits_stoploss_with_no_lock():
    reason, exit_price, mfe, activated = _replay_ticks(
        [103.0, 95.0, 89.0], entry_price=100.0, stop_price=90.0, target_price=130.0,
    )
    assert reason == "STOPLOSS"
    assert exit_price == 90.0
    assert activated is False


def test_replay_ticks_lock_activates_and_catches_a_reversal_at_breakeven():
    # Target distance is 30 points (100 -> 130). Threshold 0.8 means the peak
    # must clear 100 + 0.8*30 = 124 to arm. Peak hits 125 (activates,
    # lock_fraction=0.0 -> locks at breakeven = 100), then reverses toward
    # the original 90 stop -- the lock must catch it at 100, not let it run
    # all the way down to 90.
    reason, exit_price, mfe, activated = _replay_ticks(
        [110.0, 125.0, 115.0, 105.0, 99.0, 91.0],
        entry_price=100.0, stop_price=90.0, target_price=130.0, lock=(0.8, 0.0),
    )
    assert activated is True
    assert reason == "LOCK_STOP"
    assert exit_price == 100.0
    assert mfe == 125.0


def test_replay_ticks_lock_never_activates_when_threshold_not_reached():
    # Peak only reaches 40% of the 30-point distance (112 of 100->130) --
    # well short of an 0.8 threshold -- so this must behave exactly like the
    # no-lock baseline: falls straight through to the original stop.
    reason, exit_price, mfe, activated = _replay_ticks(
        [105.0, 112.0, 95.0, 89.0],
        entry_price=100.0, stop_price=90.0, target_price=130.0, lock=(0.8, 0.0),
    )
    assert activated is False
    assert reason == "STOPLOSS"
    assert exit_price == 90.0


def test_replay_ticks_lock_fraction_protects_only_a_partial_share_of_the_gain():
    # Peak at activation is 125 (25 points of gain above entry). lock_fraction
    # 0.5 protects half of that: locked stop = 100 + 0.5*25 = 112.5.
    reason, exit_price, mfe, activated = _replay_ticks(
        [125.0, 118.0, 112.5, 105.0],
        entry_price=100.0, stop_price=90.0, target_price=130.0, lock=(0.8, 0.5),
    )
    assert activated is True
    assert reason == "LOCK_STOP"
    assert exit_price == 112.5


def test_replay_ticks_incomplete_when_ticks_exhausted_without_a_trigger():
    reason, exit_price, mfe, activated = _replay_ticks(
        [101.0, 103.0, 102.0], entry_price=100.0, stop_price=50.0, target_price=200.0,
    )
    assert reason == "INCOMPLETE"
    assert exit_price == 102.0
    assert activated is False


def test_replay_ticks_incomplete_with_no_ticks_at_all_falls_back_to_entry():
    reason, exit_price, mfe, activated = _replay_ticks(
        [], entry_price=100.0, stop_price=50.0, target_price=200.0,
    )
    assert reason == "INCOMPLETE"
    assert exit_price == 100.0


def test_cost_pct_positive_for_a_real_premium():
    assert 0.0 < _cost_pct(100.0, 100.0, 35) < 2.0


def test_cost_pct_zero_when_no_premium():
    assert _cost_pct(0.0, 0.0, 35) == 0.0


def _trade(entry_price: float = 100.0) -> SourceTrade:
    return SourceTrade(
        trade_id="t1", index_symbol="BANKNIFTY", option_type="CE", entry_price=entry_price,
        target_price=entry_price * 1.3, stop_price=entry_price * 0.9, quantity=35,
    )


def test_run_computes_net_pnl_and_win_flag_for_a_target_hit():
    trade = _trade()
    outcome = _run(trade, [105.0, 132.0], "baseline", None)
    assert isinstance(outcome, Outcome)
    assert outcome.reason == "TARGET"
    assert outcome.is_win is True
    assert outcome.pnl_percent > outcome.net_pnl_percent  # cost eats into the gross figure


def test_run_marks_loss_for_a_stop_hit():
    trade = _trade()
    outcome = _run(trade, [95.0, 89.0], "baseline", None)
    assert outcome.reason == "STOPLOSS"
    assert outcome.is_win is False


def test_bootstrap_mean_detects_a_real_positive_effect():
    values = [5.0] * 30
    lo, hi = _bootstrap_mean(values, rounds=2000)
    assert lo > 0.0
    assert hi > 0.0


def test_bootstrap_mean_null_case_straddles_zero():
    values = [3.0, -3.0, 2.5, -2.5, 1.0, -1.0] * 5
    lo, hi = _bootstrap_mean(values, rounds=2000)
    assert lo < 0.0 < hi


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _seed_trade(db, **overrides):
    fields = dict(
        trade_id="t1", strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=57000, expiry="28AUG2026", quantity=35, entry_price=100.0, target=130.0,
        stoploss=90.0, entry_time=utc_now(), origin="AI_ORIGIN_CLAUDE",
        status=TradeStatus.CLOSED, sl_mode="FIXED", exit_price=110.0,
    )
    fields.update(overrides)
    db.add(StrategyTrade(**fields))


def test_load_trades_includes_fixed_mode_matching_origin(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db)
    db.commit()
    db.close()

    trades = _load_trades(str(path), "AI_ORIGIN_%")
    assert len(trades) == 1
    assert trades[0].trade_id == "t1"


def test_load_trades_excludes_trailing_mode(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, sl_mode="TRAILING")
    db.commit()
    db.close()

    assert _load_trades(str(path), "AI_ORIGIN_%") == []


def test_load_trades_excludes_non_matching_origin(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, origin="SIGNAL")
    db.commit()
    db.close()

    assert _load_trades(str(path), "AI_ORIGIN_%") == []


def test_load_trades_honours_a_different_origin_like_pattern(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, trade_id="t2", origin="VALIDATED_SIGNAL")
    db.commit()
    db.close()

    assert _load_trades(str(path), "AI_ORIGIN_%") == []
    trades = _load_trades(str(path), "VALIDATED_SIGNAL")
    assert len(trades) == 1
    assert trades[0].trade_id == "t2"


def test_load_trades_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, exit_price=None)
    db.commit()
    db.close()

    assert _load_trades(str(path), "AI_ORIGIN_%") == []


def test_load_trades_excludes_target_at_or_below_entry(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, target=100.0)
    db.commit()
    db.close()

    assert _load_trades(str(path), "AI_ORIGIN_%") == []


def test_load_ticks_by_trade_orders_by_recorded_at(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db)
    db.commit()
    db.add(StrategyTradeTick(trade_id="t1", premium=105.0))
    db.add(StrategyTradeTick(trade_id="t1", premium=110.0))
    db.add(StrategyTradeTick(trade_id="t1", premium=108.0))
    db.commit()
    db.close()

    ticks = _load_ticks_by_trade(str(path), ["t1"])
    assert ticks["t1"] == [105.0, 110.0, 108.0]


def test_load_ticks_by_trade_empty_for_no_trade_ids(tmp_path):
    path, _db = _make_db(tmp_path)
    assert _load_ticks_by_trade(str(path), []) == {}


def test_load_ticks_by_trade_omits_trades_with_no_ticks(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db)
    db.commit()
    db.close()

    ticks = _load_ticks_by_trade(str(path), ["t1"])
    assert "t1" not in ticks


def test_min_ticks_to_replay_is_a_small_positive_floor():
    assert MIN_TICKS_TO_REPLAY >= 1
