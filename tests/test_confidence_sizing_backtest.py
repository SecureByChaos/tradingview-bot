from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.confidence_sizing_backtest import (
    HEDGE_KEYWORDS,
    _bootstrap_correlation,
    _bootstrap_mean_diff,
    _is_hedged,
    _load_entries,
    _pearson,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, confidence, reasoning, pnl_percent, entry_price=100.0,
                highest_price=None, lowest_price=None, result=TradeResult.LOSS,
                origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED, index_symbol="BANKNIFTY"):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, highest_price=highest_price, lowest_price=lowest_price,
        stoploss=entry_price * 0.9, target=entry_price * 1.2, entry_time=utc_now(),
        origin=origin, status=status, result=result, pnl_percent=pnl_percent,
        ai_confidence=confidence, ai_reasoning=reasoning,
    ))


def test_load_entries_includes_closed_ai_origination_trades_with_confidence(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", confidence=0.66, reasoning="cautious read", pnl_percent=-0.42,
               entry_price=100.0, highest_price=103.84, lowest_price=96.98)
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    assert len(entries) == 1
    e = entries[0]
    assert e.trade_id == "t1"
    assert e.confidence == 0.66
    assert abs(e.mfe_percent - 3.84) < 1e-6
    assert abs(e.mae_percent - (-3.02)) < 1e-6


def test_load_entries_excludes_non_ai_origination(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", confidence=0.7, reasoning="", pnl_percent=1.0, origin="SIGNAL")
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", confidence=0.7, reasoning="", pnl_percent=1.0, status=TradeStatus.OPEN)
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_excludes_trades_without_confidence(tmp_path):
    path, db = _make_db(tmp_path)
    db.add(StrategyTrade(
        trade_id="t1", strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35, entry_price=100.0,
        stoploss=90.0, target=120.0, entry_time=utc_now(), origin="AI_ORIGIN_CLAUDE",
        status=TradeStatus.CLOSED, result=TradeResult.LOSS, pnl_percent=-1.0,
        ai_confidence=None,
    ))
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_handles_missing_mfe_mae_gracefully(tmp_path):
    # highest_price/lowest_price null for trades predating those columns.
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", confidence=0.7, reasoning="", pnl_percent=2.0,
               highest_price=None, lowest_price=None)
    db.commit()
    db.close()
    entries = _load_entries(str(path))
    assert entries[0].mfe_percent is None
    assert entries[0].mae_percent is None


def test_is_hedged_matches_each_documented_keyword():
    for keyword in HEDGE_KEYWORDS:
        assert _is_hedged(f"This is a {keyword} read on the setup.") is True
        assert _is_hedged(f"THIS IS A {keyword.upper()} READ.") is True  # case-insensitive


def test_is_hedged_false_when_no_keyword_present():
    assert _is_hedged("Confirmed breakout, developing ADX, no conflicting signals.") is False


def test_is_hedged_false_on_empty_reasoning():
    assert _is_hedged("") is False


def test_pearson_perfect_positive_correlation():
    xs = [0.5, 0.6, 0.7, 0.8, 0.9]
    ys = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert abs(_pearson(xs, ys) - 1.0) < 1e-9


def test_pearson_perfect_negative_correlation():
    xs = [0.5, 0.6, 0.7, 0.8, 0.9]
    ys = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert abs(_pearson(xs, ys) - (-1.0)) < 1e-9


def test_pearson_zero_variance_returns_zero_not_a_crash():
    assert _pearson([0.7, 0.7, 0.7], [1.0, 2.0, 3.0]) == 0.0


def test_pearson_too_few_points_returns_zero():
    assert _pearson([0.7], [1.0]) == 0.0


def test_bootstrap_mean_diff_detects_a_real_gap():
    hedged = [-5.0, -6.0, -4.0, -7.0, -5.5] * 5
    not_hedged = [3.0, 4.0, 2.0, 5.0, 3.5] * 5
    lo, hi = _bootstrap_mean_diff(hedged, not_hedged)
    assert hi < 0  # hedged reliably worse


def test_bootstrap_correlation_detects_a_real_positive_relationship():
    xs = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95] * 3
    ys = [-3.0, -2.5, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0] * 3
    lo, hi = _bootstrap_correlation(xs, ys)
    assert lo > 0
