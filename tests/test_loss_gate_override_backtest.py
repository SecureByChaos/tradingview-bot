from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.db_models import AIOriginationLog, AISettings, Candle, StrategyTrade
from scripts.loss_gate_override_backtest import (
    BlockedDecision,
    _bootstrap_mean_diff,
    _forward_index_return,
    _load_blocked_decisions,
    _losing_streak_own_readings,
    _max_same_direction_losses,
    _reconstruct_loss_streak,
    db_timestamp_to_ist,
    run_backtest,
)


def _make_db(tmp_path):
    path = str(tmp_path / "trading.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _utc(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _add_trade(db, *, trade_id, index_symbol, signal, entry_time, result):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination", signal=signal, index_symbol=index_symbol,
        tradingsymbol="X", symboltoken="1", strike=24000, expiry="28AUG2026", option_type="PE",
        quantity=75, entry_price=100.0, stoploss=90.0, target=120.0, entry_time=entry_time,
        origin="AI_ORIGIN_OPENAI", status="CLOSED", result=result,
    ))


def _add_log(db, *, timestamp, index_symbol, decision, confidence, chop, trade_id=None):
    db.add(AIOriginationLog(
        timestamp=timestamp, index_name=index_symbol, provider="openai", provider_role="primary",
        decision=decision, confidence=confidence, chop_efficiency_ratio=chop, trade_id=trade_id,
        regime="TREND", setups="[]", context_json="{}", data_stale=False,
    ))


def _add_candle(db, *, index_symbol, ts_ist, close):
    db.add(Candle(
        index_symbol=index_symbol, interval="ONE_MINUTE", ts_ist=ts_ist,
        open=close, high=close, low=close, close=close, volume=0,
    ))


def test_db_timestamp_to_ist_shifts_by_5_30():
    raw = "2026-08-27 08:40:08.000000"  # the offset-less shape plain sqlite3 actually returns
    assert db_timestamp_to_ist(raw) == datetime(2026, 8, 27, 14, 10, 8)


def test_reconstruct_loss_streak_stops_at_first_win(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 4, 0, 0), result="WIN")
    _add_trade(db, trade_id="t2", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 5, 0, 0), result="LOSS")
    _add_trade(db, trade_id="t3", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 6, 0, 0), result="LOSS")
    db.commit()
    conn = sqlite3.connect(path)

    streak = _reconstruct_loss_streak(conn, "NIFTY", "BUY_PE", "2026-08-27 07:00:00.000000")

    assert streak == ["t3", "t2"]


def test_reconstruct_loss_streak_ignores_trades_after_the_decision(tmp_path):
    # The explicit look-ahead guard this reconstruction needs but the live
    # gate never does (it only ever runs in real time).
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 5, 0, 0), result="LOSS")
    _add_trade(db, trade_id="t2", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 9, 0, 0), result="LOSS")  # after the decision
    db.commit()
    conn = sqlite3.connect(path)

    streak = _reconstruct_loss_streak(conn, "NIFTY", "BUY_PE", "2026-08-27 06:00:00.000000")

    assert streak == ["t1"]


def test_reconstruct_loss_streak_excludes_yesterdays_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 26, 5, 0, 0), result="LOSS")
    db.commit()
    conn = sqlite3.connect(path)

    streak = _reconstruct_loss_streak(conn, "NIFTY", "BUY_PE", "2026-08-27 06:00:00.000000")

    assert streak == []


def test_max_same_direction_losses_reads_admin_setting(tmp_path):
    path, db = _make_db(tmp_path)
    db.add(AISettings(id=1, ai_origination_max_same_direction_losses=3))
    db.commit()
    conn = sqlite3.connect(path)

    assert _max_same_direction_losses(conn) == 3


def test_max_same_direction_losses_falls_back_without_a_row(tmp_path):
    path, _db = _make_db(tmp_path)
    conn = sqlite3.connect(path)
    assert _max_same_direction_losses(conn) == 2


def test_losing_streak_own_readings_averages_and_ignores_missing(tmp_path):
    path, db = _make_db(tmp_path)
    _add_log(db, timestamp=_utc(2026, 8, 27, 5, 0, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.7, chop=0.2, trade_id="t1")
    _add_log(db, timestamp=_utc(2026, 8, 27, 6, 0, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.8, chop=None, trade_id="t2")  # predates chop field
    db.commit()
    conn = sqlite3.connect(path)

    chop, confidence = _losing_streak_own_readings(conn, ["t1", "t2"])

    assert chop == 0.2  # only t1 has a chop reading
    assert confidence == pytest.approx(0.75)  # mean of 0.7 and 0.8


def test_forward_index_return_computed_from_real_candles(tmp_path):
    path, db = _make_db(tmp_path)
    _add_candle(db, index_symbol="NIFTY", ts_ist=datetime(2026, 8, 27, 14, 10, 0), close=24124.10)
    _add_candle(db, index_symbol="NIFTY", ts_ist=datetime(2026, 8, 27, 15, 10, 0), close=24074.10)
    db.commit()
    conn = sqlite3.connect(path)

    ret = _forward_index_return(conn, "NIFTY", datetime(2026, 8, 27, 14, 9, 0), horizon_minutes=60)

    assert ret == pytest.approx((24074.10 - 24124.10) / 24124.10 * 100.0)


def test_forward_index_return_none_when_no_forward_candle(tmp_path):
    path, db = _make_db(tmp_path)
    _add_candle(db, index_symbol="NIFTY", ts_ist=datetime(2026, 8, 27, 14, 10, 0), close=24124.10)
    db.commit()
    conn = sqlite3.connect(path)

    ret = _forward_index_return(conn, "NIFTY", datetime(2026, 8, 27, 14, 9, 0), horizon_minutes=60)

    assert ret is None


def test_blocked_decision_diverged_on_chop_alone():
    entry = BlockedDecision(
        index_symbol="NIFTY", action="BUY_PE", decision_ist=datetime(2026, 8, 27, 14, 0),
        chop_efficiency_ratio=0.6, confidence=0.7, losses_chop=0.2, losses_confidence=0.7,
        forward_return=None,
    )
    assert entry.diverged is True


def test_blocked_decision_diverged_on_confidence_alone():
    entry = BlockedDecision(
        index_symbol="NIFTY", action="BUY_PE", decision_ist=datetime(2026, 8, 27, 14, 0),
        chop_efficiency_ratio=0.3, confidence=0.85, losses_chop=0.3, losses_confidence=0.65,
        forward_return=None,
    )
    assert entry.diverged is True


def test_blocked_decision_not_diverged_when_similar():
    entry = BlockedDecision(
        index_symbol="NIFTY", action="BUY_PE", decision_ist=datetime(2026, 8, 27, 14, 0),
        chop_efficiency_ratio=0.32, confidence=0.71, losses_chop=0.3, losses_confidence=0.70,
        forward_return=None,
    )
    assert entry.diverged is False


def test_blocked_decision_not_diverged_when_readings_missing():
    entry = BlockedDecision(
        index_symbol="NIFTY", action="BUY_PE", decision_ist=datetime(2026, 8, 27, 14, 0),
        chop_efficiency_ratio=None, confidence=0.85, losses_chop=None, losses_confidence=None,
        forward_return=None,
    )
    assert entry.diverged is False


def test_load_blocked_decisions_end_to_end(tmp_path):
    path, db = _make_db(tmp_path)
    # Two prior losses today -> a third BUY_PE decision reconstructs as blocked.
    _add_trade(db, trade_id="loss1", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 4, 0, 0), result="LOSS")
    _add_trade(db, trade_id="loss2", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 5, 0, 0), result="LOSS")
    _add_log(db, timestamp=_utc(2026, 8, 27, 4, 0, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.65, chop=0.25, trade_id="loss1")
    _add_log(db, timestamp=_utc(2026, 8, 27, 5, 0, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.70, chop=0.20, trade_id="loss2")
    _add_log(db, timestamp=_utc(2026, 8, 27, 8, 40, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.78, chop=0.80, trade_id=None)
    _add_candle(db, index_symbol="NIFTY", ts_ist=datetime(2026, 8, 27, 14, 10, 0), close=24124.10)
    _add_candle(db, index_symbol="NIFTY", ts_ist=datetime(2026, 8, 27, 15, 10, 0), close=24074.10)
    db.commit()
    conn = sqlite3.connect(path)

    entries, unexplained = _load_blocked_decisions(conn)

    assert unexplained == 0
    assert len(entries) == 1
    entry = entries[0]
    assert entry.diverged is True  # chop 0.80 vs losses' mean 0.225 clears the floor
    assert entry.forward_return == pytest.approx((24074.10 - 24124.10) / 24124.10 * 100.0 * -1)  # BUY_PE flips sign


def test_load_blocked_decisions_excludes_below_confidence_floor(tmp_path):
    path, db = _make_db(tmp_path)
    _add_log(db, timestamp=_utc(2026, 8, 27, 8, 0, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.40, chop=0.5, trade_id=None)
    db.commit()
    conn = sqlite3.connect(path)

    entries, unexplained = _load_blocked_decisions(conn)

    assert entries == []
    assert unexplained == 0  # never entered the streak-reconstruction path at all


def test_load_blocked_decisions_reports_unexplained_when_streak_too_short(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="loss1", index_symbol="NIFTY", signal="BUY_PE",
               entry_time=_utc(2026, 8, 27, 4, 0, 0), result="LOSS")
    _add_log(db, timestamp=_utc(2026, 8, 27, 4, 0, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.65, chop=0.25, trade_id="loss1")
    _add_log(db, timestamp=_utc(2026, 8, 27, 5, 0, 0), index_symbol="NIFTY",
             decision="BUY_PE", confidence=0.70, chop=0.30, trade_id=None)
    db.commit()
    conn = sqlite3.connect(path)

    entries, unexplained = _load_blocked_decisions(conn)

    assert entries == []
    assert unexplained == 1


def test_bootstrap_mean_diff_detects_a_real_separated_gap():
    lo, hi = _bootstrap_mean_diff([5.0, 5.0, 5.0], [-5.0, -5.0, -5.0])
    assert lo == hi == 10.0


def test_run_backtest_runs_clean_on_an_empty_population(caplog):
    with caplog.at_level("INFO"):
        run_backtest([], unexplained=0)
    messages = "\n".join(r.message for r in caplog.records)
    assert "No decisions reconstructed as blocked by this gate" in messages


def test_run_backtest_reports_unexplained_count(caplog):
    with caplog.at_level("INFO"):
        run_backtest([], unexplained=3)
    messages = "\n".join(r.message for r in caplog.records)
    assert "3 additional" in messages


def test_run_backtest_smoke_run_with_mixed_population(caplog):
    entries = [
        BlockedDecision(
            index_symbol="NIFTY", action="BUY_PE", decision_ist=datetime(2026, 8, 27, 14, 0),
            chop_efficiency_ratio=0.8, confidence=0.78, losses_chop=0.2, losses_confidence=0.65,
            forward_return=3.0,
        ),
        BlockedDecision(
            index_symbol="BANKNIFTY", action="BUY_PE", decision_ist=datetime(2026, 8, 27, 14, 5),
            chop_efficiency_ratio=0.31, confidence=0.66, losses_chop=0.30, losses_confidence=0.64,
            forward_return=-1.5,
        ),
    ]
    with caplog.at_level("INFO"):
        run_backtest(entries, unexplained=0)
    messages = "\n".join(r.message for r in caplog.records)
    assert "DIVERGED" in messages
    assert "SIMILAR" in messages
    assert "Too few observations" in messages  # n=1 each side
