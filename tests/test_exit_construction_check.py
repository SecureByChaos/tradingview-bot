from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.exit_construction_check import (
    Trade,
    _bootstrap_mean_diff,
    _load_trades,
    _report_shape,
    run_check,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, pnl_percent, result, exit_reason, origin="AI_ORIGIN_OPENAI",
                status=TradeStatus.CLOSED):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", tradingsymbol="X", symboltoken="1", strike=24150,
        expiry="28AUG2026", option_type="CE", quantity=75,
        entry_price=100.0, stoploss=88.0, target=120.0, entry_time=utc_now(),
        origin=origin, status=status, result=result, pnl_percent=pnl_percent, exit_reason=exit_reason,
    ))


# ---------------------------------------------------------------------------
# _load_trades
# ---------------------------------------------------------------------------

def test_load_trades_only_reads_closed_ai_origination_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t-ai", pnl_percent=5.0, result=TradeResult.WIN, exit_reason="TARGET")
    _add_trade(db, trade_id="t-signal", pnl_percent=5.0, result=TradeResult.WIN, exit_reason="TARGET",
               origin="SIGNAL")
    _add_trade(db, trade_id="t-open", pnl_percent=0.0, result=TradeResult.WIN, exit_reason=None,
               status=TradeStatus.OPEN)
    db.commit()
    db.close()

    trades = _load_trades(str(path))

    assert [t.trade_id for t in trades] == ["t-ai"]


def test_load_trades_reads_exit_reason_and_result(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", pnl_percent=-9.5, result=TradeResult.LOSS, exit_reason="STOPLOSS")
    db.commit()
    db.close()

    trades = _load_trades(str(path))

    assert len(trades) == 1
    assert trades[0].exit_reason == "STOPLOSS"
    assert trades[0].pnl_percent == -9.5
    assert trades[0].is_win is False


# ---------------------------------------------------------------------------
# _report_shape
# ---------------------------------------------------------------------------

def test_report_shape_computes_win_loss_ratio(caplog):
    trades = [
        Trade(trade_id="t1", exit_reason="TARGET", pnl_percent=6.0, is_win=True),
        Trade(trade_id="t2", exit_reason="TARGET", pnl_percent=8.0, is_win=True),
        Trade(trade_id="t3", exit_reason="STOPLOSS", pnl_percent=-10.0, is_win=False),
        Trade(trade_id="t4", exit_reason="STOPLOSS", pnl_percent=-12.0, is_win=False),
    ]
    with caplog.at_level("INFO"):
        _report_shape("all", trades, len(trades))
    message = caplog.records[0].message
    assert "mean_win=+7.00%" in message
    assert "mean_loss=-11.00%" in message
    assert "win/loss_ratio=0.64" in message


def test_report_shape_handles_zero_entries(caplog):
    with caplog.at_level("INFO"):
        _report_shape("empty", [], 0)
    assert "n=0" in caplog.records[0].message


# ---------------------------------------------------------------------------
# _bootstrap_mean_diff / run_check
# ---------------------------------------------------------------------------

def test_bootstrap_mean_diff_detects_losses_bigger_than_wins():
    losses = [10.0] * 25
    wins = [3.0] * 25
    lo, hi = _bootstrap_mean_diff(losses, wins)
    assert lo > 0


def test_run_check_handles_empty_population(caplog):
    with caplog.at_level("INFO"):
        run_check([])
    messages = "\n".join(r.message for r in caplog.records)
    assert "No closed AI Origination trades found" in messages


def test_run_check_smoke_run_with_mixed_exit_reasons(caplog):
    trades = [
        Trade(trade_id="t1", exit_reason="TARGET", pnl_percent=6.0, is_win=True),
        Trade(trade_id="t2", exit_reason="STOPLOSS", pnl_percent=-9.0, is_win=False),
        Trade(trade_id="t3", exit_reason="TRAIL_EXIT", pnl_percent=4.0, is_win=True),
        Trade(trade_id="t4", exit_reason="STALL_EXIT", pnl_percent=-0.5, is_win=False),
        Trade(trade_id="t5", exit_reason="TIME_EXIT", pnl_percent=1.0, is_win=True),
    ]
    with caplog.at_level("INFO"):
        run_check(trades)
    messages = "\n".join(r.message for r in caplog.records)
    assert "EXIT CONSTRUCTION CHECK" in messages
    assert "BY EXIT REASON" in messages
    assert "STOPLOSS" in messages
    assert "TARGET" in messages
    assert "BELOW MIN SAMPLE" in messages  # every bucket here is well under 20
