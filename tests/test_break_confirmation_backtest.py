from __future__ import annotations

import json

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, StrategyTrade, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.backtest.data import IndexArrays
from scripts.backtest.setups import LONG, SHORT
from scripts.break_confirmation_backtest import (
    _bootstrap_mean_diff,
    _break_confirmed_mask,
    _load_live_entries,
)

NAN = float("nan")


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_pair(db, *, trade_id, decision, setups, option_type, entry_price, highest_price,
              pnl_percent, result, index_name="BANKNIFTY"):
    db.add(AIOriginationLog(
        timestamp=utc_now(), index_name=index_name, provider="claude", provider_role="primary",
        decision=decision, trade_id=trade_id, regime="TREND", setups=json.dumps(setups),
        context_json="{}", data_stale=False,
    ))
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination - Bank Nifty", signal=decision,
        index_symbol=index_name, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type=option_type, quantity=35,
        entry_price=entry_price, highest_price=highest_price,
        stoploss=entry_price * 0.9, target=entry_price * 1.2, entry_time=utc_now(),
        origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED, result=result,
        pnl_percent=pnl_percent,
    ))


def test_load_live_entries_classifies_confirmed_vs_unconfirmed(tmp_path):
    path, db = _make_db(tmp_path)
    _add_pair(db, trade_id="t1", decision="BUY_CE", setups=["ORB_BREAK_UP", "TREND_REGIME"],
              option_type="CE", entry_price=100.0, highest_price=110.0,
              pnl_percent=8.0, result=TradeResult.WIN)
    _add_pair(db, trade_id="t2", decision="BUY_CE", setups=["TREND_REGIME"],
              option_type="CE", entry_price=100.0, highest_price=101.8,
              pnl_percent=-10.61, result=TradeResult.LOSS)
    db.commit()
    db.close()

    entries = _load_live_entries(str(path))

    assert len(entries) == 2
    by_id = {e.pnl_percent: e for e in entries}
    confirmed = [e for e in entries if e.confirmed]
    unconfirmed = [e for e in entries if not e.confirmed]
    assert len(confirmed) == 1 and confirmed[0].pnl_percent == 8.0
    assert len(unconfirmed) == 1 and unconfirmed[0].pnl_percent == -10.61


def test_load_live_entries_pe_uses_down_break_setups(tmp_path):
    path, db = _make_db(tmp_path)
    _add_pair(db, trade_id="t1", decision="BUY_PE", setups=["PDL_BREAK"],
              option_type="PE", entry_price=100.0, highest_price=105.0,
              pnl_percent=5.0, result=TradeResult.WIN)
    # A PE with only the UP-direction break setups active must NOT count as confirmed.
    _add_pair(db, trade_id="t2", decision="BUY_PE", setups=["ORB_BREAK_UP", "PDH_BREAK"],
              option_type="PE", entry_price=100.0, highest_price=100.5,
              pnl_percent=-8.0, result=TradeResult.LOSS)
    db.commit()
    db.close()

    entries = _load_live_entries(str(path))

    assert {e.confirmed for e in entries if e.pnl_percent == 5.0} == {True}
    assert {e.confirmed for e in entries if e.pnl_percent == -8.0} == {False}


def test_load_live_entries_computes_mfe_from_highest_price(tmp_path):
    path, db = _make_db(tmp_path)
    _add_pair(db, trade_id="t1", decision="BUY_CE", setups=[],
              option_type="CE", entry_price=100.0, highest_price=101.78,
              pnl_percent=-10.61, result=TradeResult.LOSS)
    db.commit()
    db.close()

    entries = _load_live_entries(str(path))

    assert len(entries) == 1
    assert entries[0].mfe_percent is not None
    assert abs(entries[0].mfe_percent - 1.78) < 0.01


def test_load_live_entries_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    db.add(AIOriginationLog(
        timestamp=utc_now(), index_name="BANKNIFTY", provider="claude", provider_role="primary",
        decision="BUY_CE", trade_id="t1", regime="TREND", setups="[]",
        context_json="{}", data_stale=False,
    ))
    db.add(StrategyTrade(
        trade_id="t1", strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=100.0, highest_price=101.0,
        stoploss=90.0, target=120.0, entry_time=utc_now(),
        origin="AI_ORIGIN_CLAUDE", status=TradeStatus.OPEN, result=TradeResult.OPEN,
    ))
    db.commit()
    db.close()

    entries = _load_live_entries(str(path))

    assert entries == []


def test_bootstrap_mean_diff_straddles_zero_for_identical_distributions():
    a = [1.0, 2.0, 3.0, -1.0, -2.0] * 10
    b = list(a)
    lo, hi = _bootstrap_mean_diff(a, b)
    assert lo <= 0 <= hi


def test_bootstrap_mean_diff_detects_separated_distributions():
    worse = [-10.0] * 30
    better = [10.0] * 30
    lo, hi = _bootstrap_mean_diff(worse, better)
    assert hi < 0  # worse - better is reliably negative


def test_break_confirmed_mask_matches_same_direction_only():
    direction = np.array([LONG, LONG, SHORT, SHORT, 0], dtype=np.int8)
    orb_dir = np.array([LONG, 0, SHORT, LONG, LONG], dtype=np.int8)
    pdhpdl_dir = np.array([0, SHORT, 0, 0, 0], dtype=np.int8)

    mask = _break_confirmed_mask(direction, orb_dir, pdhpdl_dir)

    # bar 0: direction LONG, orb LONG -> confirmed
    # bar 1: direction LONG, neither break agrees -> not confirmed
    # bar 2: direction SHORT, orb SHORT -> confirmed
    # bar 3: direction SHORT, orb LONG (opposite) -> not confirmed
    # bar 4: direction 0 -> never confirmed regardless of break state
    assert list(mask) == [True, False, True, False, False]
