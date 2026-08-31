from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from scripts.validated_setup_window_backtest import (
    Entry,
    _bootstrap_mean_diff,
    _is_validated,
    _load_entries,
    _report_bucket,
    db_timestamp_to_ist,
    run_backtest,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _ist_to_utc_naive(hour: int, minute: int) -> datetime:
    # 11:00 IST == 05:30 UTC. Naive, matching how utc_now()-style columns
    # are actually written by this app.
    return datetime(2026, 8, 28, hour, minute, tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)


def _add_trade(db, *, trade_id, decision, setups, hour, minute, entry_price, pnl_percent, result,
                index_name="NIFTY"):
    db.add(AIOriginationLog(
        timestamp=_ist_to_utc_naive(hour, minute), index_name=index_name, provider="openai",
        provider_role="primary", decision=decision, trade_id=trade_id, regime="TREND", adx=26.0,
        setups=json.dumps(setups), context_json="{}", data_stale=False,
    ))
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name=f"AI Origination - {index_name}", signal=decision,
        index_symbol=index_name, tradingsymbol="X", symboltoken="1", strike=24150,
        expiry="28AUG2026", option_type="CE" if decision == "BUY_CE" else "PE", quantity=75,
        entry_price=entry_price, stoploss=entry_price * 0.85, target=entry_price * 1.2,
        entry_time=_ist_to_utc_naive(hour, minute), origin="AI_ORIGIN_OPENAI",
        status=TradeStatus.CLOSED, result=result, pnl_percent=pnl_percent,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


# ---------------------------------------------------------------------------
# db_timestamp_to_ist
# ---------------------------------------------------------------------------

def test_db_timestamp_to_ist_shifts_naive_utc_string_by_five_thirty():
    # Real production rows come back from plain sqlite3 as bare strings with
    # no offset marker at all -- the shift must always apply.
    result = db_timestamp_to_ist("2026-08-28 05:30:00")
    assert result == datetime(2026, 8, 28, 11, 0, 0)


# ---------------------------------------------------------------------------
# _is_validated
# ---------------------------------------------------------------------------

def test_buy_ce_with_matching_up_setup_inside_window_is_validated():
    decision_ist = datetime(2026, 8, 28, 12, 0)
    assert _is_validated("BUY_CE", ["EMA_STACK_UP", "TREND_REGIME"], decision_ist) is True


def test_buy_ce_with_only_down_setups_is_not_validated():
    decision_ist = datetime(2026, 8, 28, 12, 0)
    assert _is_validated("BUY_CE", ["EMA_STACK_DOWN", "ORB_BREAK_DOWN"], decision_ist) is False


def test_buy_pe_with_pdl_break_inside_window_is_validated():
    decision_ist = datetime(2026, 8, 28, 13, 30)
    assert _is_validated("BUY_PE", ["PDL_BREAK"], decision_ist) is True


def test_matching_setup_outside_window_is_not_validated():
    decision_ist = datetime(2026, 8, 28, 10, 0)  # before 11:00
    assert _is_validated("BUY_CE", ["ORB_BREAK_UP"], decision_ist) is False

    decision_ist_late = datetime(2026, 8, 28, 14, 30)  # after 14:00
    assert _is_validated("BUY_PE", ["PDL_BREAK"], decision_ist_late) is False


def test_window_boundaries_are_inclusive_start_exclusive_end():
    assert _is_validated("BUY_CE", ["PDH_BREAK"], datetime(2026, 8, 28, 11, 0)) is True
    assert _is_validated("BUY_CE", ["PDH_BREAK"], datetime(2026, 8, 28, 14, 0)) is False


def test_pdh_break_matches_buy_ce_pdl_break_matches_buy_pe():
    ist = datetime(2026, 8, 28, 12, 0)
    assert _is_validated("BUY_CE", ["PDH_BREAK"], ist) is True
    assert _is_validated("BUY_PE", ["PDH_BREAK"], ist) is False
    assert _is_validated("BUY_PE", ["PDL_BREAK"], ist) is True
    assert _is_validated("BUY_CE", ["PDL_BREAK"], ist) is False


# ---------------------------------------------------------------------------
# _load_entries
# ---------------------------------------------------------------------------

def test_load_entries_marks_validated_trades_correctly(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t-validated", decision="BUY_CE", setups=["EMA_STACK_UP"],
               hour=12, minute=0, entry_price=100.0, pnl_percent=5.0, result=TradeResult.WIN)
    _add_trade(db, trade_id="t-not-validated", decision="BUY_CE", setups=["EMA_STACK_DOWN"],
               hour=12, minute=0, entry_price=100.0, pnl_percent=-3.0, result=TradeResult.LOSS)
    db.commit()
    db.close()

    entries = {e.trade_id: e for e in _load_entries(str(path))}

    assert entries["t-validated"].validated is True
    assert entries["t-not-validated"].validated is False


def test_load_entries_derives_mfe_mae_from_ticks(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", setups=["ORB_BREAK_UP"],
               hour=12, minute=0, entry_price=100.0, pnl_percent=8.0, result=TradeResult.WIN)
    db.commit()
    _add_ticks(db, "t1", [100.0, 95.0, 112.0, 108.0])
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert len(entries) == 1
    assert entries[0].mfe_percent == 12.0
    assert entries[0].mae_percent == -5.0


def test_load_entries_excludes_none_and_still_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    db.add(AIOriginationLog(
        timestamp=_ist_to_utc_naive(12, 0), index_name="NIFTY", provider="openai",
        provider_role="primary", decision="NONE", trade_id=None, regime="MIXED", adx=15.0,
        setups="[]", context_json="{}", data_stale=False,
    ))
    _add_trade(db, trade_id="t-open", decision="BUY_CE", setups=["EMA_STACK_UP"],
               hour=12, minute=0, entry_price=100.0, pnl_percent=0.0, result=TradeResult.WIN)
    db.commit()
    # The seeded trade is still OPEN (never closed) -- _load_entries requires
    # status == 'CLOSED', so this trade must be excluded even though its log
    # row otherwise matches.
    db.execute(StrategyTrade.__table__.update().where(StrategyTrade.trade_id == "t-open").values(status=TradeStatus.OPEN))
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert entries == []


# ---------------------------------------------------------------------------
# _bootstrap_mean_diff / run_backtest
# ---------------------------------------------------------------------------

def test_bootstrap_mean_diff_detects_a_real_gap():
    a = [10.0] * 25
    b = [-5.0] * 25
    lo, hi = _bootstrap_mean_diff(a, b)
    assert lo > 0


def test_run_backtest_handles_empty_population(caplog):
    with caplog.at_level("INFO"):
        run_backtest([])
    messages = "\n".join(r.message for r in caplog.records)
    assert "No closed AI Origination trades" in messages


def test_run_backtest_smoke_run_with_mixed_population(caplog):
    entries = [
        Entry(trade_id="t1", index_symbol="NIFTY", decision="BUY_CE", validated=True,
              pnl_percent=8.0, mfe_percent=10.0, mae_percent=-1.0, is_win=True),
        Entry(trade_id="t2", index_symbol="NIFTY", decision="BUY_PE", validated=False,
              pnl_percent=-4.0, mfe_percent=2.0, mae_percent=-6.0, is_win=False),
        Entry(trade_id="t3", index_symbol="BANKNIFTY", decision="BUY_CE", validated=True,
              pnl_percent=None if False else 3.0, mfe_percent=None, mae_percent=None, is_win=True),
    ]
    with caplog.at_level("INFO"):
        run_backtest(entries)
    messages = "\n".join(r.message for r in caplog.records)
    assert "VALIDATED-WINDOW BACKTEST" in messages
    assert "BELOW MIN SAMPLE" in messages  # every bucket here is well under 20


def test_report_bucket_handles_zero_entries(caplog):
    with caplog.at_level("INFO"):
        _report_bucket("empty label", [])
    messages = "\n".join(r.message for r in caplog.records)
    assert "n=0" in messages
