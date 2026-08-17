from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.same_direction_entries_backtest import (
    BUCKET_LABELS,
    Entry,
    _bootstrap_mean_diff,
    _bucket_label,
    _load_entries,
    run_buckets,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, same_direction_count, pnl_percent, signal="BUY_CE",
                entry_price=100.0, result=TradeResult.LOSS, origin="AI_ORIGIN_CLAUDE",
                status=TradeStatus.CLOSED, index_symbol="BANKNIFTY", context_override=None):
    if context_override is not None:
        context_json = context_override
    else:
        context_json = json.dumps({"same_direction_entries_today": {signal: same_direction_count}})
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination - Bank Nifty", signal=signal,
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, stoploss=entry_price * 0.9, target=entry_price * 1.2,
        entry_time=utc_now(), origin=origin, status=status, result=result,
        pnl_percent=pnl_percent, market_context_json=context_json,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


def test_bucket_label_caps_at_three_plus():
    assert _bucket_label(0) == "0"
    assert _bucket_label(1) == "1"
    assert _bucket_label(2) == "2"
    assert _bucket_label(3) == "3+"
    assert _bucket_label(7) == "3+"


def test_load_entries_extracts_own_signal_count_and_ticks(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", same_direction_count=1, pnl_percent=3.84, signal="BUY_CE")
    _add_ticks(db, "t1", [100.0, 103.84, 101.5, 96.98, 99.0])
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    assert len(entries) == 1
    e = entries[0]
    assert e.same_direction_count == 1
    assert abs(e.mfe_percent - 3.84) < 1e-6
    assert abs(e.mae_percent - (-3.02)) < 1e-6


def test_load_entries_uses_own_signal_not_the_other_direction(tmp_path):
    # Context stores counts per direction ({"BUY_CE": n, "BUY_PE": n}); a
    # BUY_PE trade must read its own key, not BUY_CE's.
    path, db = _make_db(tmp_path)
    context = json.dumps({"same_direction_entries_today": {"BUY_CE": 5, "BUY_PE": 1}})
    _add_trade(db, trade_id="t1", same_direction_count=0, pnl_percent=1.0, signal="BUY_PE",
               context_override=context)
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    assert entries[0].same_direction_count == 1


def test_load_entries_excludes_trades_without_the_field(tmp_path):
    # Predates the field entirely -- context JSON present but no
    # same_direction_entries_today key. Must be excluded, not defaulted to 0.
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", same_direction_count=0, pnl_percent=1.0,
               context_override=json.dumps({"regime": "TREND"}))
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_excludes_trades_with_no_context_json(tmp_path):
    path, db = _make_db(tmp_path)
    db.add(StrategyTrade(
        trade_id="t1", strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35, entry_price=100.0,
        stoploss=90.0, target=120.0, entry_time=utc_now(), origin="AI_ORIGIN_CLAUDE",
        status=TradeStatus.CLOSED, result=TradeResult.LOSS, pnl_percent=-1.0,
        market_context_json=None,
    ))
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_excludes_non_ai_origination(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", same_direction_count=0, pnl_percent=1.0, origin="SIGNAL")
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", same_direction_count=0, pnl_percent=1.0, status=TradeStatus.OPEN)
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_handles_missing_ticks_gracefully(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", same_direction_count=0, pnl_percent=2.0)
    db.commit()
    db.close()
    entries = _load_entries(str(path))
    assert entries[0].mfe_percent is None
    assert entries[0].mae_percent is None


def test_bootstrap_mean_diff_detects_a_real_gap():
    zero = [3.0, 4.0, 2.0, 5.0, 3.5] * 5
    two_plus = [-5.0, -6.0, -4.0, -7.0, -5.5] * 5
    lo, hi = _bootstrap_mean_diff(zero, two_plus)
    assert lo > 0  # bucket-0 reliably better


def _entry(same_direction_count, pnl_percent):
    return Entry(
        trade_id="t", index_symbol="BANKNIFTY", same_direction_count=same_direction_count,
        pnl_percent=pnl_percent, mfe_percent=None, mae_percent=None, is_win=(pnl_percent > 0),
    )


def test_run_buckets_covers_all_four_labels(caplog):
    entries = [_entry(0, 1.0), _entry(1, -1.0), _entry(2, -2.0), _entry(3, -3.0), _entry(5, -4.0)]
    with caplog.at_level("INFO"):
        run_buckets(entries)
    messages = "\n".join(r.message for r in caplog.records)
    for label in BUCKET_LABELS:
        assert label in messages


def test_run_buckets_flags_a_real_gate_threshold_effect(caplog):
    below_gate = [_entry(0, 2.0) for _ in range(15)] + [_entry(1, 1.5) for _ in range(15)]
    at_or_above_gate = [_entry(2, -6.0) for _ in range(10)] + [_entry(3, -7.0) for _ in range(10)]
    with caplog.at_level("INFO"):
        run_buckets(below_gate + at_or_above_gate)
    messages = "\n".join(r.message for r in caplog.records)
    assert "reliably BETTER" in messages


def test_run_buckets_reports_no_reliable_difference_when_there_is_none(caplog):
    entries = [_entry(0, pnl) for pnl in [-2.0, 1.0, -1.0, 2.0, -3.0] * 6] + \
              [_entry(1, pnl) for pnl in [-2.0, 1.0, -1.0, 2.0, -3.0] * 6]
    with caplog.at_level("INFO"):
        run_buckets(entries)
    messages = "\n".join(r.message for r in caplog.records)
    assert "no reliable difference at this sample size" in messages
