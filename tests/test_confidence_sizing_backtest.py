from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.confidence_sizing_backtest import (
    HEDGE_KEYWORDS,
    Entry,
    _bootstrap_correlation,
    _bootstrap_mean_diff,
    _is_hedged,
    _load_entries,
    _pearson,
    run_confidence_buckets,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, confidence, reasoning, pnl_percent, entry_price=100.0,
                result=TradeResult.LOSS, origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED,
                index_symbol="BANKNIFTY"):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, stoploss=entry_price * 0.9, target=entry_price * 1.2,
        entry_time=utc_now(), origin=origin, status=status, result=result,
        pnl_percent=pnl_percent, ai_confidence=confidence, ai_reasoning=reasoning,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


def test_load_entries_includes_closed_ai_origination_trades_with_confidence(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", confidence=0.66, reasoning="cautious read", pnl_percent=-0.42,
               entry_price=100.0)
    _add_ticks(db, "t1", [100.0, 103.84, 101.5, 96.98, 99.0])
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    assert len(entries) == 1
    e = entries[0]
    assert e.trade_id == "t1"
    assert e.confidence == 0.66
    assert abs(e.mfe_percent - 3.84) < 1e-6
    assert abs(e.mae_percent - (-3.02)) < 1e-6


def test_load_entries_ignores_stale_highest_lowest_price_columns(tmp_path):
    # The confirmed 14 Aug bug: highest_price/lowest_price are only maintained
    # on the side monitor_open_trades needs for the trailing-stop engine -- for
    # a long trade (every AI Origination trade) lowest_price stays pinned at
    # its entry-time seed forever. MFE/MAE must come from strategy_trade_ticks
    # instead, and must NOT be pulled from these two columns even when they're
    # populated with a misleading value.
    path, db = _make_db(tmp_path)
    db.add(StrategyTrade(
        trade_id="t1", strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35, entry_price=100.0,
        highest_price=100.0, lowest_price=100.0,  # the misleading seeded value
        stoploss=90.0, target=120.0, entry_time=utc_now(), origin="AI_ORIGIN_CLAUDE",
        status=TradeStatus.CLOSED, result=TradeResult.LOSS, pnl_percent=-0.42,
        ai_confidence=0.66, ai_reasoning="",
    ))
    _add_ticks(db, "t1", [100.0, 103.84, 96.98])
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    assert abs(entries[0].mfe_percent - 3.84) < 1e-6
    assert abs(entries[0].mae_percent - (-3.02)) < 1e-6


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


def test_load_entries_handles_missing_ticks_gracefully(tmp_path):
    # No strategy_trade_ticks rows for this trade -- e.g. it closed before the
    # 30s monitor ever recorded a sample, or predates tick recording entirely.
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", confidence=0.7, reasoning="", pnl_percent=2.0)
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


def _entry(confidence, pnl_percent):
    return Entry(
        trade_id="t", index_symbol="BANKNIFTY", confidence=confidence, reasoning="",
        pnl_percent=pnl_percent, mfe_percent=None, mae_percent=None, is_win=(pnl_percent > 0),
    )


def test_run_confidence_buckets_flags_a_real_floor_effect(caplog):
    # 14 Aug production motivation: the <0.60 bucket stood out sharply on
    # point estimates alone (n=28, mean -5.35% vs roughly breakeven above it)
    # but the script had no bootstrap comparison to say whether that gap was
    # reliable rather than eyeballed. This is the added comparison.
    below = [_entry(0.55, -6.0) for _ in range(30)]
    above = [_entry(c, 0.5) for c in [0.65, 0.70, 0.78, 0.82, 0.90] for _ in range(6)]
    with caplog.at_level("INFO"):
        run_confidence_buckets(below + above)
    messages = "\n".join(r.message for r in caplog.records)
    assert "reliably WORSE below 0.60" in messages


def test_run_confidence_buckets_reports_no_reliable_difference_when_there_is_none(caplog):
    below = [_entry(0.55, pnl) for pnl in [-2.0, 1.0, -1.0, 2.0, -3.0] * 6]
    above = [_entry(0.7, pnl) for pnl in [-2.0, 1.0, -1.0, 2.0, -3.0] * 6]
    with caplog.at_level("INFO"):
        run_confidence_buckets(below + above)
    messages = "\n".join(r.message for r in caplog.records)
    assert "no reliable difference at this sample size" in messages
