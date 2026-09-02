from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, StrategyTradeTick, TradeStatus
from app.time_utils import utc_now
from scripts.giveback_ratio_backtest import (
    MFE_BANDS,
    Outcome,
    SourceTrade,
    _baseline_net_pnl_percent,
    _bootstrap_mean,
    _cost_pct,
    _load_trades,
    _load_ticks_by_trade,
    _mfe_band,
    _mfe_percent,
    _replay_giveback,
    _run,
)


def test_mfe_percent_from_ticks():
    assert _mfe_percent([105.0, 112.0, 108.0], entry_price=100.0) == 12.0


def test_mfe_percent_zero_when_never_traded_above_entry():
    assert _mfe_percent([98.0, 95.0], entry_price=100.0) == 0.0


def test_mfe_percent_zero_for_empty_ticks():
    assert _mfe_percent([], entry_price=100.0) == 0.0


def test_mfe_band_boundaries():
    assert _mfe_band(0.0) == "0-2%"
    assert _mfe_band(1.99) == "0-2%"
    assert _mfe_band(2.0) == "2-5%"
    assert _mfe_band(4.99) == "2-5%"
    assert _mfe_band(5.0) == "5-10%"
    assert _mfe_band(10.0) == "10-20%"
    assert _mfe_band(20.0) == "20%+"
    assert _mfe_band(500.0) == "20%+"


def test_mfe_bands_cover_zero_to_infinity_with_no_gaps():
    # Every band's high must equal the next band's low.
    for i in range(len(MFE_BANDS) - 1):
        assert MFE_BANDS[i][1] == MFE_BANDS[i + 1][0]
    assert MFE_BANDS[0][0] == 0.0
    assert MFE_BANDS[-1][1] == float("inf")


def test_replay_giveback_never_arms_below_the_floor_and_falls_to_original_stop():
    # Peak only reaches 4% (below an 8% floor) then falls to the original
    # stop -- must behave exactly like a plain stop, never arming.
    reason, exit_price, mfe, armed = _replay_giveback(
        [104.0, 95.0, 90.0], entry_price=100.0, stop_price=90.0, target_price=130.0,
        floor_pct=8.0, giveback_ratio=0.5,
    )
    assert armed is False
    assert reason == "STOPLOSS"
    assert exit_price == 90.0


def test_replay_giveback_arms_and_trails_proportionally_to_peak_gain():
    # Peak clears the 8% floor at 112 (12% MFE). giveback_ratio=0.5 means
    # the trail allows giving back half of the 12-point gain: trail stop =
    # 112 - 0.5*12 = 106. A subsequent drop to 105 must trigger there, well
    # above the original 90 stop.
    reason, exit_price, mfe, armed = _replay_giveback(
        [108.0, 112.0, 109.0, 105.0], entry_price=100.0, stop_price=90.0, target_price=130.0,
        floor_pct=8.0, giveback_ratio=0.5,
    )
    assert armed is True
    assert reason == "GIVEBACK_STOP"
    assert exit_price == 106.0
    assert mfe == 112.0


def test_replay_giveback_trail_ratchets_up_as_new_peaks_form():
    # A second, higher peak (120) must re-ratchet the trail stop upward
    # rather than leaving it pinned to the first arming peak.
    reason, exit_price, mfe, armed = _replay_giveback(
        [110.0, 120.0, 115.0, 112.0],
        entry_price=100.0, stop_price=90.0, target_price=200.0, floor_pct=8.0, giveback_ratio=0.5,
    )
    assert armed is True
    # trail stop off the 120 peak = 120 - 0.5*20 = 110 -- not yet touched by 112
    assert reason == "INCOMPLETE"
    assert mfe == 120.0


def test_replay_giveback_still_exits_at_target_once_reached():
    reason, exit_price, mfe, armed = _replay_giveback(
        [108.0, 131.0], entry_price=100.0, stop_price=90.0, target_price=130.0,
        floor_pct=8.0, giveback_ratio=0.5,
    )
    assert reason == "TARGET"
    assert exit_price == 130.0


def test_replay_giveback_trail_stays_above_the_original_stop_even_at_max_ratio():
    # trail_stop = peak - ratio*(peak-entry) ranges from entry (ratio=1) to
    # peak (ratio=0) once armed -- always >= entry, which is always above a
    # real stop set below entry. The max(stop_price, trail_stop) clamp is
    # defensive and should never actually need to bind; this pins that
    # invariant rather than assuming it.
    reason, exit_price, mfe, armed = _replay_giveback(
        [108.0, 99.5], entry_price=100.0, stop_price=90.0, target_price=200.0,
        floor_pct=5.0, giveback_ratio=0.99,
    )
    assert armed is True
    assert reason == "GIVEBACK_STOP"
    assert exit_price > 90.0
    assert exit_price >= 100.0  # never below entry at a near-1.0 ratio


def test_cost_pct_positive_for_a_real_premium():
    assert 0.0 < _cost_pct(100.0, 100.0, 35) < 2.0


def _trade(**overrides) -> SourceTrade:
    fields = dict(
        trade_id="t1", entry_price=100.0, target_price=130.0, stop_price=90.0,
        quantity=35, result="LOSS", pnl_percent=-10.0,
    )
    fields.update(overrides)
    return SourceTrade(**fields)


def test_run_marks_win_and_computes_net_pnl():
    trade = _trade()
    outcome = _run(trade, [108.0, 131.0], floor_pct=8.0, giveback_ratio=0.5)
    assert isinstance(outcome, Outcome)
    assert outcome.is_win is True


def test_baseline_never_arms_the_trail():
    trade = _trade()
    baseline = _baseline_net_pnl_percent(trade, [120.0, 95.0, 89.0])
    # Baseline falls straight through to the real stop (90), never a trail exit.
    assert baseline == (90.0 - 100.0) / 100.0 * 100.0 - _cost_pct(100.0, 90.0, 35)


def test_bootstrap_mean_detects_a_real_positive_effect():
    lo, hi = _bootstrap_mean([2.0] * 25, rounds=2000)
    assert lo > 0.0


def test_bootstrap_mean_null_case_straddles_zero():
    lo, hi = _bootstrap_mean([2.0, -2.0, 1.5, -1.5] * 6, rounds=2000)
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
        result="WIN", pnl_percent=10.0,
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


def test_load_trades_honours_origin_like_pattern(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, trade_id="t2", origin="QUICK_SCALP")
    db.commit()
    db.close()

    assert _load_trades(str(path), "AI_ORIGIN_%") == []
    trades = _load_trades(str(path), "QUICK_SCALP")
    assert len(trades) == 1


def test_load_ticks_by_trade_orders_by_recorded_at(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db)
    db.commit()
    db.add(StrategyTradeTick(trade_id="t1", premium=105.0))
    db.add(StrategyTradeTick(trade_id="t1", premium=112.0))
    db.commit()
    db.close()

    ticks = _load_ticks_by_trade(str(path), ["t1"])
    assert ticks["t1"] == [105.0, 112.0]


def test_load_ticks_by_trade_empty_for_no_ids(tmp_path):
    path, _db = _make_db(tmp_path)
    assert _load_ticks_by_trade(str(path), []) == {}
