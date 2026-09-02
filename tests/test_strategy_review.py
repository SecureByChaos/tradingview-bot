from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, StrategyTradeTick, TradeStatus
from scripts.strategy_review import (
    MIN_TICKS_FOR_EXCURSION,
    Trade,
    _bucket,
    _load_ticks_by_trade,
    _load_trades,
    _summarize,
)


def test_bucket_signal_uses_strategy_name():
    assert _bucket("SIGNAL", "BNV7") == "Signal: BNV7"


def test_bucket_ai_origination_splits_by_provider():
    assert _bucket("AI_ORIGIN_OPENAI", "AI Origination") == "AI Origination (Openai)"
    assert _bucket("AI_ORIGIN_CLAUDE", "AI Origination") == "AI Origination (Claude)"


def test_bucket_validated_signal_autonomous_ai_quick_scalp():
    assert _bucket("VALIDATED_SIGNAL", "x") == "Validated Signal"
    assert _bucket("AUTONOMOUS_AI", "x") == "Autonomous AI"
    assert _bucket("QUICK_SCALP", "x") == "Quick Scalp"


def test_bucket_excludes_ai_alt_shadow_trades():
    assert _bucket("AI_ALT_OPENAI", "x") is None
    assert _bucket("AI_ALT_CLAUDE", "x") is None


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _seed_trade(db, **overrides):
    fields = dict(
        trade_id="t1", strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol="BANKNIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=57000, expiry="28AUG2026", quantity=35, entry_price=100.0, target=130.0,
        stoploss=90.0, entry_time=datetime.utcnow(), origin="AI_ORIGIN_CLAUDE",
        status=TradeStatus.CLOSED, sl_mode="FIXED", exit_price=110.0,
        net_pnl=350.0, pnl_percent=10.0, investment_amount=3500.0,
        result="WIN", exit_reason="TARGET",
    )
    fields.update(overrides)
    db.add(StrategyTrade(**fields))


def test_load_trades_excludes_ai_alt_and_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db, trade_id="t1", origin="AI_ORIGIN_CLAUDE")
    _seed_trade(db, trade_id="t2", origin="AI_ALT_CLAUDE")
    _seed_trade(db, trade_id="t3", status=TradeStatus.OPEN)
    db.commit()
    db.close()

    trades = _load_trades(str(path), "2000-01-01 00:00:00.000000")
    assert [t.trade_id for t in trades] == ["t1"]


def test_load_trades_respects_since_cutoff(tmp_path):
    path, db = _make_db(tmp_path)
    old = datetime.utcnow() - timedelta(days=90)
    _seed_trade(db, trade_id="old", entry_time=old)
    _seed_trade(db, trade_id="recent", entry_time=datetime.utcnow())
    db.commit()
    db.close()

    since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
    trades = _load_trades(str(path), since)
    assert [t.trade_id for t in trades] == ["recent"]


def test_load_ticks_by_trade_orders_by_recorded_at(tmp_path):
    path, db = _make_db(tmp_path)
    _seed_trade(db)
    db.commit()
    db.add(StrategyTradeTick(trade_id="t1", premium=105.0))
    db.add(StrategyTradeTick(trade_id="t1", premium=112.0))
    db.commit()
    db.close()

    ticks = _load_ticks_by_trade(str(path), ["t1"])
    assert ticks["t1"] == [105.0, 112.0]


def test_load_ticks_by_trade_empty_for_no_ids(tmp_path):
    path, _db = _make_db(tmp_path)
    assert _load_ticks_by_trade(str(path), []) == {}


def _trade(**overrides) -> Trade:
    fields = dict(
        trade_id="t1", bucket="AI Origination (Openai)", entry_price=100.0,
        net_pnl=-50.0, pnl_percent=-5.0, investment_amount=3500.0,
        result="LOSS", exit_reason="STOPLOSS",
    )
    fields.update(overrides)
    return Trade(**fields)


def test_summarize_counts_wins_losses_and_exit_reasons():
    trades = [
        _trade(trade_id="t1", result="WIN", pnl_percent=10.0, net_pnl=350.0, exit_reason="TARGET"),
        _trade(trade_id="t2", result="LOSS", pnl_percent=-5.0, net_pnl=-175.0, exit_reason="STOPLOSS"),
    ]
    stats = _summarize(trades, {})
    s = stats["AI Origination (Openai)"]
    assert s.n == 2
    assert s.wins == 1
    assert s.losses == 1
    assert s.net_pnl_sum == 175.0
    assert s.exit_reasons == {"TARGET": 1, "STOPLOSS": 1}


def test_summarize_detects_a_loss_that_had_positive_mfe():
    trades = [_trade(trade_id="t1", result="LOSS")]
    ticks = {"t1": [105.0, 108.0, 95.0]}  # ran to +8% before finishing negative
    stats = _summarize(trades, ticks)
    s = stats["AI Origination (Openai)"]
    assert s.losses_with_ticks == 1
    assert s.losses_with_positive_mfe == 1
    assert s.mfe_sum_for_giveback_losses == 8.0


def test_summarize_does_not_flag_a_loss_that_never_traded_positive():
    trades = [_trade(trade_id="t1", result="LOSS")]
    ticks = {"t1": [98.0, 95.0, 90.0]}
    stats = _summarize(trades, ticks)
    s = stats["AI Origination (Openai)"]
    assert s.losses_with_ticks == 1
    assert s.losses_with_positive_mfe == 0


def test_summarize_excludes_a_loss_with_too_few_ticks_from_giveback_check():
    trades = [_trade(trade_id="t1", result="LOSS")]
    ticks = {"t1": [95.0]}  # below MIN_TICKS_FOR_EXCURSION
    stats = _summarize(trades, ticks)
    s = stats["AI Origination (Openai)"]
    assert s.losses == 1
    assert s.losses_with_ticks == 0


def test_summarize_never_runs_giveback_check_on_a_win():
    trades = [_trade(trade_id="t1", result="WIN", pnl_percent=10.0)]
    ticks = {"t1": [105.0, 112.0]}
    stats = _summarize(trades, ticks)
    s = stats["AI Origination (Openai)"]
    assert s.losses_with_ticks == 0
    assert s.losses_with_positive_mfe == 0


def test_min_ticks_for_excursion_is_a_small_positive_floor():
    assert MIN_TICKS_FOR_EXCURSION >= 1
