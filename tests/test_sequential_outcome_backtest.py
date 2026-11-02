from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.sequential_outcome_backtest import (
    ClosedTrade,
    _bootstrap_mean_diff,
    _load_trades,
    classify_by_preceding_result,
    run_backtest,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(
    db, *, trade_id, entry_time, pnl_percent, signal="BUY_CE", result=TradeResult.LOSS,
    origin="AGENTIC_AI", status=TradeStatus.CLOSED,
):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Agentic AI - Nifty 50", signal=signal,
        index_symbol="NIFTY", tradingsymbol="X", symboltoken="1", strike=23000,
        expiry="09NOV2026", option_type="CE" if signal == "BUY_CE" else "PE", quantity=75,
        entry_price=100.0, stoploss=85.0, target=130.0,
        entry_time=entry_time, origin=origin, status=status, result=result,
        pnl_percent=pnl_percent,
    ))


def _ct(signal, result, pnl_percent, entry_time="t0") -> ClosedTrade:
    return ClosedTrade(trade_id="x", signal=signal, entry_time=entry_time, pnl_percent=pnl_percent, result=result)


# ---------------------------------------------------------------------------
# classify_by_preceding_result -- the real observation this script checks:
# "after a BUY_CE loss, the next BUY_CE won" (2 Nov 2026). This is pure logic,
# no DB involved.
# ---------------------------------------------------------------------------


def test_classify_reproduces_the_reported_shape():
    # LOSS, LOSS, WIN, LOSS -- the real 2 Nov CE sequence. First trade has no
    # predecessor and is excluded; the remaining 3 classify as:
    #   trade2 (LOSS) follows trade1 (LOSS) -> following_loss
    #   trade3 (WIN)  follows trade2 (LOSS) -> following_loss
    #   trade4 (LOSS) follows trade3 (WIN)  -> following_win
    trades = [
        _ct("BUY_CE", "LOSS", -1.93, "t1"), _ct("BUY_CE", "LOSS", -0.40, "t2"),
        _ct("BUY_CE", "WIN", 8.31, "t3"), _ct("BUY_CE", "LOSS", -0.97, "t4"),
    ]
    buckets = classify_by_preceding_result(trades)
    assert [s.pnl_percent for s in buckets["following_loss"]] == [-0.40, 8.31]
    assert [s.pnl_percent for s in buckets["following_win"]] == [-0.97]
    assert buckets["following_breakeven"] == []


def test_classify_excludes_the_first_trade_in_the_sequence():
    trades = [_ct("BUY_CE", "WIN", 5.0, "t1")]
    buckets = classify_by_preceding_result(trades)
    assert buckets["following_loss"] == []
    assert buckets["following_win"] == []


def test_classify_puts_a_breakeven_predecessor_in_its_own_bucket():
    trades = [
        _ct("BUY_PE", "BREAKEVEN", 0.0, "t1"), _ct("BUY_PE", "LOSS", -2.0, "t2"),
    ]
    buckets = classify_by_preceding_result(trades)
    assert buckets["following_loss"] == []
    assert buckets["following_win"] == []
    assert [s.pnl_percent for s in buckets["following_breakeven"]] == [-2.0]


def test_classify_is_win_matches_the_result_field_not_the_sign():
    # A positive pnl_percent with result=LOSS (e.g. a scratch that rounds to
    # a hair above zero but is still recorded LOSS) must not be miscounted.
    trades = [
        _ct("BUY_CE", "LOSS", -1.0, "t1"), _ct("BUY_CE", "LOSS", 0.01, "t2"),
    ]
    buckets = classify_by_preceding_result(trades)
    assert buckets["following_loss"][0].is_win is False


# ---------------------------------------------------------------------------
# _load_trades
# ---------------------------------------------------------------------------


def test_load_trades_orders_chronologically_and_filters_origin(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t2", entry_time=utc_now().replace(microsecond=0), pnl_percent=1.0, origin="AGENTIC_AI")
    _add_trade(db, trade_id="t1", entry_time=utc_now().replace(microsecond=0), pnl_percent=-1.0, origin="AGENTIC_AI")
    _add_trade(db, trade_id="other", entry_time=utc_now(), pnl_percent=9.0, origin="QUICK_SCALP")
    db.commit()
    db.close()

    trades = _load_trades(str(path), "AGENTIC_AI")
    assert {t.trade_id for t in trades} == {"t1", "t2"}


def test_load_trades_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", entry_time=utc_now(), pnl_percent=1.0, status=TradeStatus.OPEN)
    db.commit()
    db.close()
    assert _load_trades(str(path), "AGENTIC_AI") == []


def test_load_trades_excludes_non_directional_signals(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", entry_time=utc_now(), pnl_percent=1.0, signal="SELL_CE")
    db.commit()
    db.close()
    assert _load_trades(str(path), "AGENTIC_AI") == []


def test_load_trades_scopes_to_the_requested_origin_only(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", entry_time=utc_now(), pnl_percent=1.0, origin="AUTONOMOUS_AI")
    db.commit()
    db.close()
    assert _load_trades(str(path), "AGENTIC_AI") == []
    assert len(_load_trades(str(path), "AUTONOMOUS_AI")) == 1


# ---------------------------------------------------------------------------
# bootstrap + end-to-end smoke
# ---------------------------------------------------------------------------


def test_bootstrap_mean_diff_detects_a_real_gap():
    following_loss = [4.0, 3.0, 5.0, 4.5, 3.5] * 5
    following_win = [-5.0, -4.0, -6.0, -5.5, -4.5] * 5
    lo, hi = _bootstrap_mean_diff(following_loss, following_win)
    assert lo > 0  # following-a-loss reliably better in this synthetic case


def test_bootstrap_mean_diff_null_case_straddles_zero():
    a = [1.0, -1.0, 2.0, -2.0, 0.5] * 6
    b = [1.0, -1.0, 2.0, -2.0, 0.5] * 6
    lo, hi = _bootstrap_mean_diff(a, b)
    assert lo <= 0 <= hi


def test_run_backtest_reports_both_directions_and_flags_thin_samples(caplog):
    trades = [
        _ct("BUY_CE", "LOSS", -1.93, "t1"), _ct("BUY_CE", "LOSS", -0.40, "t2"),
        _ct("BUY_CE", "WIN", 8.31, "t3"), _ct("BUY_CE", "LOSS", -0.97, "t4"),
        _ct("BUY_PE", "LOSS", -1.61, "t5"), _ct("BUY_PE", "WIN", 2.89, "t6"),
        _ct("BUY_PE", "LOSS", -0.59, "t7"), _ct("BUY_PE", "LOSS", -2.97, "t8"),
    ]
    with caplog.at_level("INFO"):
        run_backtest(trades)
    messages = "\n".join(r.message for r in caplog.records)
    assert "BUY_CE" in messages
    assert "BUY_PE" in messages
    assert "BELOW MIN SAMPLE" in messages  # every bucket here is far under 20


def test_run_backtest_handles_a_direction_with_fewer_than_two_trades(caplog):
    trades = [_ct("BUY_CE", "WIN", 5.0, "t1")]
    with caplog.at_level("INFO"):
        run_backtest(trades)
    messages = "\n".join(r.message for r in caplog.records)
    assert "no predecessor" in messages
