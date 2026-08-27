from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.chop_gate_backtest import _bootstrap_mean_diff, _load_entries, run_chop_buckets


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, decision, chop_efficiency_ratio, entry_price, pnl_percent, result,
                index_name="NIFTY", status=TradeStatus.CLOSED):
    db.add(AIOriginationLog(
        timestamp=utc_now(), index_name=index_name, provider="openai", provider_role="primary",
        decision=decision, trade_id=trade_id, regime="MIXED", chop_efficiency_ratio=chop_efficiency_ratio,
        setups=json.dumps([]), context_json=json.dumps({}), data_stale=False,
    ))
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name=f"AI Origination - {index_name}", signal=decision,
        index_symbol=index_name, tradingsymbol="X", symboltoken="1", strike=24150,
        expiry="28AUG2026", option_type="CE" if decision == "BUY_CE" else "PE", quantity=75,
        entry_price=entry_price, stoploss=entry_price * 0.85, target=entry_price * 1.2,
        entry_time=utc_now(), origin="AI_ORIGIN_OPENAI", status=status, result=result,
        pnl_percent=pnl_percent if status == TradeStatus.CLOSED else None,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


def test_load_entries_reads_chop_efficiency_ratio_from_the_joined_log_row(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_PE", chop_efficiency_ratio=0.18,
               entry_price=100.0, pnl_percent=-12.47, result=TradeResult.LOSS)
    db.commit()

    entries = _load_entries(str(path))
    assert len(entries) == 1
    assert entries[0].chop_efficiency_ratio == 0.18


def test_missing_chop_efficiency_ratio_is_none_not_excluded(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", chop_efficiency_ratio=None,
               entry_price=100.0, pnl_percent=5.0, result=TradeResult.WIN)
    db.commit()

    entries = _load_entries(str(path))
    assert len(entries) == 1
    assert entries[0].chop_efficiency_ratio is None


def test_mfe_mae_derived_from_ticks_real_shape(tmp_path):
    # Matches a real 27 Aug trade's own reported figures: entry 60.95,
    # premium high 64.15 -> MFE +5.25%, premium low 53.35 -> MAE -12.47%.
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_PE", chop_efficiency_ratio=0.4,
               entry_price=60.95, pnl_percent=-12.47, result=TradeResult.LOSS)
    _add_ticks(db, "t1", [60.95, 64.15, 53.35, 58.0])
    db.commit()

    entries = _load_entries(str(path))
    assert entries[0].mfe_percent == pytest.approx((64.15 - 60.95) / 60.95 * 100.0)
    assert entries[0].mae_percent == pytest.approx((53.35 - 60.95) / 60.95 * 100.0)


def test_population_excludes_none_decisions_and_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="NONE", chop_efficiency_ratio=0.1,
               entry_price=100.0, pnl_percent=0.0, result=TradeResult.WIN)
    _add_trade(db, trade_id="t2", decision="BUY_CE", chop_efficiency_ratio=0.8,
               entry_price=100.0, pnl_percent=5.0, result=TradeResult.OPEN, status=TradeStatus.OPEN)
    _add_trade(db, trade_id="t3", decision="BUY_PE", chop_efficiency_ratio=0.6,
               entry_price=100.0, pnl_percent=3.0, result=TradeResult.WIN)
    db.commit()

    entries = _load_entries(str(path))
    assert [e.trade_id for e in entries] == ["t3"]


def test_bootstrap_mean_diff_detects_a_real_separated_gap():
    lo, hi = _bootstrap_mean_diff([-10.0, -10.0, -10.0], [10.0, 10.0, 10.0])
    assert lo == hi == -20.0


def test_bootstrap_mean_diff_no_gap_when_identical():
    lo, hi = _bootstrap_mean_diff([5.0, -5.0], [5.0, -5.0])
    assert lo <= 0.0 <= hi


def test_run_chop_buckets_runs_clean_on_a_mixed_population(caplog, tmp_path):
    path, db = _make_db(tmp_path)
    for i in range(3):
        _add_trade(db, trade_id=f"choppy-{i}", decision="BUY_PE", chop_efficiency_ratio=0.15,
                   entry_price=100.0, pnl_percent=-8.0, result=TradeResult.LOSS)
    for i in range(3):
        _add_trade(db, trade_id=f"clean-{i}", decision="BUY_CE", chop_efficiency_ratio=0.75,
                   entry_price=100.0, pnl_percent=6.0, result=TradeResult.WIN)
    _add_trade(db, trade_id="no-ratio", decision="BUY_CE", chop_efficiency_ratio=None,
               entry_price=100.0, pnl_percent=1.0, result=TradeResult.WIN)
    db.commit()

    entries = _load_entries(str(path))
    with caplog.at_level("INFO"):
        run_chop_buckets(entries)

    messages = "\n".join(r.message for r in caplog.records)
    assert "PART 1: OUTCOME BY EFFICIENCY-RATIO BUCKET" in messages
    assert "PART 2: CANDIDATE HARD-FLOOR CHECK" in messages
    assert "no efficiency ratio" in messages
    assert "BELOW MIN SAMPLE" in messages  # every bucket here is far below MIN_BUCKET_LIVE=20
