from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AIOriginationLog, Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.trend_freshness_check import (
    Entry,
    _bootstrap_mean_diff,
    _bucket_for,
    _load_entries,
    _report_bucket,
    run_check,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, decision, trend_duration_bars, trend_duration_pct, move_extent_atr,
                entry_price, pnl_percent, result, index_name="NIFTY", status=TradeStatus.CLOSED):
    db.add(AIOriginationLog(
        timestamp=utc_now(), index_name=index_name, provider="openai", provider_role="primary",
        decision=decision, trade_id=trade_id, regime="TREND", adx=25.0, setups="[]",
        context_json="{}", data_stale=False,
        trend_duration_bars=trend_duration_bars, trend_duration_pct_of_session=trend_duration_pct,
        move_extent_atr=move_extent_atr,
    ))
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name=f"AI Origination - {index_name}", signal=decision,
        index_symbol=index_name, tradingsymbol="X", symboltoken="1", strike=24150,
        expiry="28AUG2026", option_type="CE" if decision == "BUY_CE" else "PE", quantity=75,
        entry_price=entry_price, stoploss=entry_price * 0.85, target=entry_price * 1.2,
        entry_time=utc_now(), origin="AI_ORIGIN_OPENAI", status=status, result=result,
        pnl_percent=pnl_percent,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


# ---------------------------------------------------------------------------
# _bucket_for
# ---------------------------------------------------------------------------

def test_bucket_boundaries():
    assert _bucket_for(4.8) == "<10% (very fresh)"
    assert _bucket_for(9.99) == "<10% (very fresh)"
    assert _bucket_for(10.0) == "10-40% (developing)"
    assert _bucket_for(39.99) == "10-40% (developing)"
    assert _bucket_for(40.0) == "40-70% (moderately mature)"
    assert _bucket_for(62.5) == "40-70% (moderately mature)"
    assert _bucket_for(70.0) == ">=70% (fully mature)"
    assert _bucket_for(100.0) == ">=70% (fully mature)"


# ---------------------------------------------------------------------------
# _load_entries
# ---------------------------------------------------------------------------

def test_load_entries_reads_trend_age_fields(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_PE", trend_duration_bars=3, trend_duration_pct=4.8,
               move_extent_atr=-0.15, entry_price=563.25, pnl_percent=-11.35, result=TradeResult.LOSS)
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert len(entries) == 1
    assert entries[0].trend_duration_bars == 3
    assert entries[0].trend_duration_pct == 4.8
    assert entries[0].move_extent_atr == -0.15


def test_load_entries_handles_missing_trend_age_without_excluding_the_trade(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_CE", trend_duration_bars=None, trend_duration_pct=None,
               move_extent_atr=None, entry_price=100.0, pnl_percent=5.0, result=TradeResult.WIN)
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert len(entries) == 1
    assert entries[0].trend_duration_pct is None


def test_load_entries_derives_mfe_mae_from_ticks(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", decision="BUY_PE", trend_duration_bars=2, trend_duration_pct=3.2,
               move_extent_atr=-0.08, entry_price=109.8, pnl_percent=-20.77, result=TradeResult.LOSS)
    db.commit()
    _add_ticks(db, "t1", [109.8, 113.25, 87.0])
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert len(entries) == 1
    assert round(entries[0].mfe_percent, 2) == 3.14
    assert round(entries[0].mae_percent, 2) == -20.77


def test_load_entries_excludes_none_decisions_and_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    db.add(AIOriginationLog(
        timestamp=utc_now(), index_name="NIFTY", provider="openai", provider_role="primary",
        decision="NONE", trade_id=None, regime="MIXED", adx=15.0, setups="[]", context_json="{}",
        data_stale=False,
    ))
    _add_trade(db, trade_id="t-open", decision="BUY_CE", trend_duration_bars=5, trend_duration_pct=20.0,
               move_extent_atr=1.0, entry_price=100.0, pnl_percent=0.0, result=TradeResult.WIN,
               status=TradeStatus.OPEN)
    db.commit()
    db.close()

    entries = _load_entries(str(path))

    assert entries == []


# ---------------------------------------------------------------------------
# _bootstrap_mean_diff / run_check
# ---------------------------------------------------------------------------

def test_bootstrap_mean_diff_detects_a_real_gap():
    fresh_losses = [-10.0] * 25
    mature_wins = [5.0] * 25
    lo, hi = _bootstrap_mean_diff(fresh_losses, mature_wins)
    assert hi < 0  # fresh bucket reliably worse


def test_run_check_handles_empty_population(caplog):
    with caplog.at_level("INFO"):
        run_check([])
    messages = "\n".join(r.message for r in caplog.records)
    assert "No closed AI Origination trades" in messages


def test_run_check_reports_missing_trend_age_separately(caplog):
    entries = [
        Entry(trade_id="t1", index_symbol="NIFTY", trend_duration_bars=None, trend_duration_pct=None,
              move_extent_atr=None, pnl_percent=5.0, mfe_percent=None, mae_percent=None, is_win=True),
        Entry(trade_id="t2", index_symbol="NIFTY", trend_duration_bars=3, trend_duration_pct=4.8,
              move_extent_atr=-0.15, pnl_percent=-11.35, mfe_percent=-0.42, mae_percent=-11.35, is_win=False),
    ]
    with caplog.at_level("INFO"):
        run_check(entries)
    messages = "\n".join(r.message for r in caplog.records)
    assert "1 of 2 entries have no recorded trend_duration_pct_of_session" in messages


def test_run_check_smoke_run_with_mixed_freshness_buckets(caplog):
    entries = [
        Entry(trade_id="t1", index_symbol="BANKNIFTY", trend_duration_bars=3, trend_duration_pct=4.8,
              move_extent_atr=-0.15, pnl_percent=-11.35, mfe_percent=-0.42, mae_percent=-11.35, is_win=False),
        Entry(trade_id="t2", index_symbol="NIFTY", trend_duration_bars=2, trend_duration_pct=3.2,
              move_extent_atr=-0.08, pnl_percent=-20.77, mfe_percent=3.14, mae_percent=-20.77, is_win=False),
        Entry(trade_id="t3", index_symbol="BANKNIFTY", trend_duration_bars=10, trend_duration_pct=62.5,
              move_extent_atr=1.96, pnl_percent=-12.31, mfe_percent=1.32, mae_percent=-12.31, is_win=False),
        Entry(trade_id="t4", index_symbol="NIFTY", trend_duration_bars=9, trend_duration_pct=100.0,
              move_extent_atr=1.61, pnl_percent=1.20, mfe_percent=5.64, mae_percent=-6.05, is_win=True),
    ]
    with caplog.at_level("INFO"):
        run_check(entries)
    messages = "\n".join(r.message for r in caplog.records)
    assert "TREND FRESHNESS CHECK" in messages
    assert "BY FRESHNESS BUCKET" in messages
    assert "very fresh" in messages
    assert "fully mature" in messages
    assert "BELOW MIN SAMPLE" in messages  # every bucket here is well under 20


def test_report_bucket_handles_zero_entries(caplog):
    with caplog.at_level("INFO"):
        _report_bucket("empty", [])
    assert "n=0" in caplog.records[0].message
