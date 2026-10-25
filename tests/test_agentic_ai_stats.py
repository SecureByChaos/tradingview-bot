from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.agentic import MIN_BUCKET_LIVE, compute_origin_stats
from app.db_models import Base, StrategyTrade, TradeStatus
from app.time_utils import utc_now


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _seed_closed_trade(db, trade_id, net_pnl, profit_loss, result, exit_reason="TARGET", origin="AGENTIC_AI"):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="Agentic AI - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=120.0,
        stoploss=90.0, entry_time=utc_now(), exit_time=utc_now(), origin=origin,
        status=TradeStatus.CLOSED, result=result, exit_reason=exit_reason,
        profit_loss=profit_loss, net_pnl=net_pnl, pnl_percent=net_pnl / 10,
    ))


def test_compute_origin_stats_empty_population_reports_zero(tmp_path):
    db = _make_db(tmp_path)
    db.commit()
    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["n"] == 0
    assert "note" in stats


def test_compute_origin_stats_basic_win_loss_math(tmp_path):
    db = _make_db(tmp_path)
    for i in range(22):
        # 15 wins at +100, 7 losses at -50 -- net = 15*100 - 7*50 = 1150
        is_win = i < 15
        _seed_closed_trade(db, f"t{i}", net_pnl=100.0 if is_win else -50.0,
                            profit_loss=110.0 if is_win else -55.0,
                            result="WIN" if is_win else "LOSS")
    db.commit()

    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["n"] == 22
    assert stats["wins"] == 15
    assert stats["losses"] == 7
    assert stats["win_rate_percent"] == round(15 / 22 * 100, 1)
    assert stats["net_pnl_total"] == 1150.0
    assert stats["below_trust_minimum"] is False


def test_compute_origin_stats_flags_below_trust_minimum(tmp_path):
    db = _make_db(tmp_path)
    for i in range(MIN_BUCKET_LIVE - 1):
        _seed_closed_trade(db, f"t{i}", net_pnl=10.0, profit_loss=12.0, result="WIN")
    db.commit()

    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["n"] == MIN_BUCKET_LIVE - 1
    assert stats["below_trust_minimum"] is True


def test_compute_origin_stats_ci_excludes_zero_on_clear_synthetic_edge(tmp_path):
    db = _make_db(tmp_path)
    for i in range(30):
        # Every trade nets exactly +50 -- a trivially reliable positive population.
        _seed_closed_trade(db, f"t{i}", net_pnl=50.0, profit_loss=55.0, result="WIN")
    db.commit()

    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["ci_excludes_zero"] is True
    lo, hi = stats["net_pnl_90pct_ci"]
    assert lo > 0 and hi > 0


def test_compute_origin_stats_ci_straddles_zero_on_noisy_population(tmp_path):
    db = _make_db(tmp_path)
    values = [30.0, -30.0, 20.0, -20.0, 10.0, -10.0] * 5  # mean 0, real noise
    for i, v in enumerate(values):
        _seed_closed_trade(db, f"t{i}", net_pnl=v, profit_loss=v, result="WIN" if v >= 0 else "LOSS")
    db.commit()

    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["ci_excludes_zero"] is False


def test_compute_origin_stats_excludes_open_trades(tmp_path):
    db = _make_db(tmp_path)
    db.add(StrategyTrade(
        trade_id="open1", strategy_name="Agentic AI - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=120.0,
        stoploss=90.0, entry_time=utc_now(), origin="AGENTIC_AI", status=TradeStatus.OPEN,
    ))
    db.commit()
    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["n"] == 0


def test_compute_origin_stats_origin_pattern_exact_match_excludes_others(tmp_path):
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", net_pnl=10.0, profit_loss=10.0, result="WIN", origin="AGENTIC_AI")
    _seed_closed_trade(db, "t2", net_pnl=10.0, profit_loss=10.0, result="WIN", origin="AUTONOMOUS_AI")
    db.commit()
    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["n"] == 1


def test_compute_origin_stats_origin_pattern_like_wildcard(tmp_path):
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", net_pnl=10.0, profit_loss=10.0, result="WIN", origin="AI_ORIGIN_OPENAI")
    _seed_closed_trade(db, "t2", net_pnl=10.0, profit_loss=10.0, result="WIN", origin="AI_ORIGIN_CLAUDE")
    _seed_closed_trade(db, "t3", net_pnl=10.0, profit_loss=10.0, result="WIN", origin="QUICK_SCALP")
    db.commit()
    stats = compute_origin_stats(db, "AI_ORIGIN_%")
    assert stats["n"] == 2


def test_compute_origin_stats_exit_reason_breakdown(tmp_path):
    db = _make_db(tmp_path)
    _seed_closed_trade(db, "t1", net_pnl=10.0, profit_loss=10.0, result="WIN", exit_reason="TARGET")
    _seed_closed_trade(db, "t2", net_pnl=-10.0, profit_loss=-10.0, result="LOSS", exit_reason="STOPLOSS")
    _seed_closed_trade(db, "t3", net_pnl=-10.0, profit_loss=-10.0, result="LOSS", exit_reason="STOPLOSS")
    db.commit()
    stats = compute_origin_stats(db, "AGENTIC_AI")
    assert stats["exit_reason_breakdown"] == {"TARGET": 1, "STOPLOSS": 2}
