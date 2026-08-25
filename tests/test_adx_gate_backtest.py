from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.adx_gate_backtest import _bootstrap_mean_diff, _load_entries


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, decision, adx, entry_price, pnl_percent, result, index_name="NIFTY"):
    db.add(AIOriginationLog(
        timestamp=utc_now(), index_name=index_name, provider="openai", provider_role="primary",
        decision=decision, trade_id=trade_id, regime="MIXED", adx=adx, setups=json.dumps([]),
        context_json="{}", data_stale=False,
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
