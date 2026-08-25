from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.market_data import Bar
from app.time_utils import utc_now
from scripts.adx_gate_backtest import (
    _bootstrap_mean_diff,
    _di_agrees,
    _edge_index,
    _eligible_index,
    _load_entries,
    run_di_direction_check,
)
from scripts.backtest.data import build_arrays


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, decision, adx, entry_price, pnl_percent, result, index_name="NIFTY",
               plus_di=None, minus_di=None):
    context = {}
    if plus_di is not None:
        context["plus_di"] = plus_di
    if minus_di is not None:
        context["minus_di"] = minus_di
    db.add(AIOriginationLog(
        timestamp=utc_now(), index_name=index_name, provider="openai", provider_role="primary",
        decision=decision, trade_id=trade_id, regime="MIXED", adx=adx, setups=json.dumps([]),
        context_json=json.dumps(context), data_stale=False,
    ))
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name=f"AI Origination - {index_name}", signal=decision,
        index_symbol=index_name, tradingsymbol="X", symboltoken="1", strike=24150,
        expiry="28AUG2026", option_type="CE" if decision == "BUY_CE" else "PE", quantity=75,
        entry_price=entry_price, stoploss=entry_price * 0.85, target=entry_price * 1.2,
        entry_time=utc_now(), origin="AI_ORIGIN_OPENAI", status=TradeStatus.CLOSED, result=result,
        pnl_percent=pnl_percent,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


def test_load_entries_reads_adx_from_the_joined_log_row(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_PE", adx=19.6, entry_price=100.0,
               pnl_percent=-0.74, result=TradeResult.LOSS)
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert len(entries) == 1
    assert entries[0].adx == 19.6


def test_load_entries_handles_missing_adx_without_excluding_the_trade(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", adx=None, entry_price=100.0,
               pnl_percent=5.0, result=TradeResult.WIN)
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert len(entries) == 1
    assert entries[0].adx is None


def test_load_entries_derives_mfe_mae_from_ticks_not_stored_columns(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", adx=28.0, entry_price=100.0,
               pnl_percent=-2.0, result=TradeResult.LOSS)
    _add_ticks(db, "t1", [105.0, 92.0, 98.0])
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert entries[0].mfe_percent == 5.0
    assert entries[0].mae_percent == -8.0


def test_load_entries_excludes_open_trades_and_non_ai_origination(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", adx=25.0, entry_price=100.0,
               pnl_percent=1.0, result=TradeResult.WIN)
    db.add(StrategyTrade(
        trade_id="t2", strategy_name="BNV7", signal="BUY_CE", index_symbol="NIFTY",
        tradingsymbol="X", symboltoken="1", strike=24150, expiry="28AUG2026", option_type="CE",
        quantity=75, entry_price=100.0, stoploss=90.0, target=120.0, entry_time=utc_now(),
        origin="SIGNAL", status=TradeStatus.CLOSED, result=TradeResult.WIN, pnl_percent=2.0,
    ))
    db.add(StrategyTrade(
        trade_id="t3", strategy_name="AI Origination - Nifty", signal="BUY_PE", index_symbol="NIFTY",
        tradingsymbol="X", symboltoken="1", strike=24150, expiry="28AUG2026", option_type="PE",
        quantity=75, entry_price=100.0, stoploss=90.0, target=120.0, entry_time=utc_now(),
        origin="AI_ORIGIN_CLAUDE", status=TradeStatus.OPEN, result=TradeResult.OPEN, pnl_percent=None,
    ))
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    # t2 has no ai_origination_logs row to join against (SIGNAL trades never
    # get one); t3 is still OPEN. Only t1 should come back.
    assert len(entries) == 1
    assert entries[0].trade_id == "t1"


def test_bootstrap_mean_diff_detects_a_real_synthetic_gap():
    below_floor = [-8.0] * 30
    at_or_above = [1.0] * 30

    lo, hi = _bootstrap_mean_diff(below_floor, at_or_above)

    assert hi < 0  # below-floor population is reliably worse


def test_bootstrap_mean_diff_no_effect_when_populations_are_identical():
    a = [1.0, -1.0, 2.0, -2.0, 0.5] * 5
    b = [1.0, -1.0, 2.0, -2.0, 0.5] * 5

    lo, hi = _bootstrap_mean_diff(a, b)

    assert lo <= 0 <= hi


def test_load_entries_reads_plus_minus_di_from_context_json(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", adx=28.0, entry_price=100.0,
               pnl_percent=3.0, result=TradeResult.WIN, plus_di=32.0, minus_di=14.0)
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert entries[0].plus_di == 32.0
    assert entries[0].minus_di == 14.0


def test_load_entries_handles_missing_di_in_context_json(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", adx=28.0, entry_price=100.0,
               pnl_percent=3.0, result=TradeResult.WIN)
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert entries[0].plus_di is None
    assert entries[0].minus_di is None


def test_di_agrees_buy_ce_wants_plus_di_greater():
    assert _di_agrees("BUY_CE", 30.0, 15.0) is True
    assert _di_agrees("BUY_CE", 15.0, 30.0) is False


def test_di_agrees_buy_pe_wants_minus_di_greater():
    assert _di_agrees("BUY_PE", 15.0, 30.0) is True
    assert _di_agrees("BUY_PE", 30.0, 15.0) is False


def test_di_agrees_returns_none_when_either_value_missing():
    assert _di_agrees("BUY_CE", None, 15.0) is None
    assert _di_agrees("BUY_CE", 30.0, None) is None


def test_run_di_direction_check_does_not_crash_on_a_realistic_mixed_population(tmp_path, caplog):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", adx=28.0, entry_price=100.0,
               pnl_percent=5.0, result=TradeResult.WIN, plus_di=32.0, minus_di=14.0)
    _add_trade(db, trade_id="t2", decision="BUY_PE", adx=22.0, entry_price=100.0,
               pnl_percent=-6.0, result=TradeResult.LOSS, plus_di=25.0, minus_di=18.0)
    _add_trade(db, trade_id="t3", decision="BUY_CE", adx=19.0, entry_price=100.0,
               pnl_percent=1.0, result=TradeResult.WIN)  # no DI recorded
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    with caplog.at_level("INFO"):
        run_di_direction_check(entries)

    text = "\n".join(r.message for r in caplog.records)
    assert "no DI recorded" in text
    assert "DI agrees with direction" in text
    assert "DI disagrees with direction" in text


# ---------------------------------------------------------------------------
# PART 4 (2-year index-level fallback) helpers
# ---------------------------------------------------------------------------

def _make_bars(num_sessions: int, bars_per_session: int = 78) -> list[Bar]:
    """5-min bars from 09:15 IST, one session per day, with a small
    deterministic oscillation so ATR/ADX/EMA all warm up to real (non-NaN,
    non-degenerate) values rather than a flat zero-range series."""
    rng = np.random.default_rng(20260825)
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


def test_eligible_index_excludes_bars_before_indicators_warm_up():
    bars = _make_bars(num_sessions=3)
    arrays = build_arrays("NIFTY", bars)

    eligible = _eligible_index(arrays)

    # The first handful of bars of the whole series cannot have a real
    # ATR14/ADX14 yet, regardless of time-of-day.
    assert not eligible[0]
    assert not eligible[5]


def test_eligible_index_excludes_bars_outside_the_trading_window():
    bars = _make_bars(num_sessions=3)
    arrays = build_arrays("NIFTY", bars)

    eligible = _eligible_index(arrays)

    # Session 3 (index >= 2*78), bar 0 is 09:15 -- before INDEX_TRADING_START
    # (09:45), even though indicators are long since warm by then.
    third_session_first_bar = 2 * 78
    assert not eligible[third_session_first_bar]

    # A bar comfortably inside 09:45-15:15 in a later session should be
    # eligible (indicators warm, inside the window).
    mid_session_bar = 2 * 78 + 20  # 09:15 + 100 min = 10:55 IST
    assert eligible[mid_session_bar]


def test_edge_index_matches_hand_computed_value():
    # 10 wins, 0 losses, all long, base rate 50% (5 up / 10) -> edge = 100% - 50% = +50pp
    edge = _edge_index(wins=10.0, ups=5.0, longs=10.0, n=10.0)
    assert edge == 50.0


def test_edge_index_returns_zero_for_empty_population():
    assert _edge_index(wins=0.0, ups=0.0, longs=0.0, n=0.0) == 0.0
