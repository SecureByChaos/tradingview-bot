from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.agentic import (
    RiskDecision,
    check_risk_gates,
    compute_position_size,
    evaluate_risk_now,
    resolve_risk_settings,
)
from app.dashboard_routes import compute_agentic_ai_position_size
from app.db_models import AgenticAISettings, Base, StrategyTrade, TradeResult, TradeStatus
from app.time_utils import IST


def _make_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _ist_now(y, m, d, hh=12, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _ist_exit(y, m, d, hh=6, mm=0) -> datetime:
    """Naive UTC wall-clock numbers landing on IST day (y, m, d) once
    shifted +5:30 -- same helper shape tests/test_agentic_ai_daily_log.py
    already established for compute_origin_stats' on_date filtering."""
    return datetime(y, m, d, hh, mm)


def _seed_trade(db, trade_id, net_pnl, result, status, exit_time=None, investment_amount=5000.0, origin="AGENTIC_AI"):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name=f"{origin} - Nifty 50", signal="BUY_CE",
        index_symbol="NIFTY", option_type="CE", tradingsymbol="X", symboltoken="1",
        strike=22500, expiry="27OCT2026", quantity=75, entry_price=100.0, target=120.0,
        stoploss=90.0, entry_time=exit_time or _ist_exit(2026, 10, 25), exit_time=exit_time,
        origin=origin, status=status, result=result, exit_reason="TARGET" if status == TradeStatus.CLOSED else None,
        profit_loss=net_pnl, net_pnl=net_pnl, pnl_percent=net_pnl / 10,
        investment_amount=investment_amount,
    ))


# ---------------------------------------------------------------------------
# compute_position_size
# ---------------------------------------------------------------------------


def test_compute_position_size_basic_math():
    # Risk 1% of Rs 100,000 = Rs 1,000. Per-unit risk = 10 (100 - 90).
    # Max units = 100, lot size 75 -> floor(100/75) = 1 lot.
    assert compute_position_size(100_000.0, 1.0, 100.0, 90.0, 75) == 1


def test_compute_position_size_floors_never_rounds_up():
    # Risk budget Rs 2,000, per-unit risk 10 -> 200 units, lot size 75 ->
    # floor(200/75) = 2 lots (150 units, Rs 1,500 at risk) -- never 3.
    assert compute_position_size(200_000.0, 1.0, 100.0, 90.0, 75) == 2


def test_compute_position_size_zero_when_no_real_stop_distance():
    assert compute_position_size(100_000.0, 1.0, 100.0, 100.0, 75) == 0


def test_compute_position_size_zero_on_non_positive_inputs():
    assert compute_position_size(0.0, 1.0, 100.0, 90.0, 75) == 0
    assert compute_position_size(100_000.0, 0.0, 100.0, 90.0, 75) == 0
    assert compute_position_size(100_000.0, 1.0, 100.0, 90.0, 0) == 0


def test_compute_position_size_zero_when_risk_budget_too_small_for_one_lot():
    # Risk budget Rs 100, per-unit risk 10 -> 10 units, lot size 75 -> 0 lots.
    assert compute_position_size(10_000.0, 1.0, 100.0, 90.0, 75) == 0


# ---------------------------------------------------------------------------
# resolve_risk_settings
# ---------------------------------------------------------------------------


def test_resolve_risk_settings_defaults_when_none():
    resolved = resolve_risk_settings(None)
    assert resolved == {
        "account_capital_rupees": 100000.0,
        "max_risk_percent_per_trade": 1.0,
        "max_daily_loss_percent": 3.0,
        "max_concurrent_positions": 1,
        "max_capital_deployed_percent": 20.0,
    }


def test_resolve_risk_settings_reads_real_row():
    db = _make_db()
    settings = AgenticAISettings(
        id=1, account_capital_rupees=50000.0, max_risk_percent_per_trade=2.0,
        max_daily_loss_percent=5.0, max_concurrent_positions=3, max_capital_deployed_percent=40.0,
    )
    db.add(settings)
    db.commit()
    resolved = resolve_risk_settings(settings)
    assert resolved["account_capital_rupees"] == 50000.0
    assert resolved["max_concurrent_positions"] == 3


# ---------------------------------------------------------------------------
# check_risk_gates
# ---------------------------------------------------------------------------


def test_check_risk_gates_allows_when_clear_with_no_settings_row():
    db = _make_db()
    decision = check_risk_gates(db, None, _ist_now(2026, 10, 25))
    assert decision.allowed is True
    assert decision.reason == "OK"


def test_check_risk_gates_blocks_on_daily_loss_limit():
    db = _make_db()
    settings = AgenticAISettings(id=1, account_capital_rupees=100000.0, max_daily_loss_percent=3.0)
    db.add(settings)
    # Floor is -3000. A realized loss of exactly -3000 today must block.
    _seed_trade(db, "t1", -3000.0, "LOSS", TradeStatus.CLOSED, exit_time=_ist_exit(2026, 10, 25))
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.allowed is False
    assert decision.reason == "DAILY_LOSS_LIMIT"


def test_check_risk_gates_does_not_block_just_above_the_daily_loss_floor():
    db = _make_db()
    settings = AgenticAISettings(id=1, account_capital_rupees=100000.0, max_daily_loss_percent=3.0)
    db.add(settings)
    _seed_trade(db, "t1", -2999.0, "LOSS", TradeStatus.CLOSED, exit_time=_ist_exit(2026, 10, 25))
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.allowed is True


def test_check_risk_gates_yesterdays_loss_does_not_count_today():
    db = _make_db()
    settings = AgenticAISettings(id=1, account_capital_rupees=100000.0, max_daily_loss_percent=3.0)
    db.add(settings)
    _seed_trade(db, "t1", -9999.0, "LOSS", TradeStatus.CLOSED, exit_time=_ist_exit(2026, 10, 24))
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.allowed is True


def test_check_risk_gates_open_trades_unrealized_pnl_never_counts_toward_daily_loss():
    # An OPEN trade carries real_pnl via profit_loss/net_pnl in the schema
    # but compute_origin_stats only reads CLOSED trades -- a floating loss
    # must not trip the circuit breaker, only a realized one. max_concurrent_
    # positions is raised so the one open trade this test seeds isn't itself
    # caught by a different gate (MAX_CONCURRENT_POSITIONS), which would
    # make this test pass for the wrong reason.
    db = _make_db()
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_daily_loss_percent=3.0, max_concurrent_positions=5,
    )
    db.add(settings)
    _seed_trade(db, "t1", -50000.0, "LOSS", TradeStatus.OPEN, exit_time=None)
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.allowed is True


def test_check_risk_gates_blocks_on_max_concurrent_positions():
    db = _make_db()
    settings = AgenticAISettings(id=1, max_concurrent_positions=1)
    db.add(settings)
    _seed_trade(db, "open1", 0.0, TradeResult.OPEN, TradeStatus.OPEN)
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.allowed is False
    assert decision.reason == "MAX_CONCURRENT_POSITIONS"


def test_check_risk_gates_other_origins_open_trades_do_not_count():
    db = _make_db()
    settings = AgenticAISettings(id=1, max_concurrent_positions=1)
    db.add(settings)
    _seed_trade(db, "open1", 0.0, TradeResult.OPEN, TradeStatus.OPEN, origin="AUTONOMOUS_AI")
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.allowed is True


def test_check_risk_gates_blocks_on_max_capital_deployed():
    db = _make_db()
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_concurrent_positions=5, max_capital_deployed_percent=20.0,
    )
    db.add(settings)
    # Ceiling is Rs 20,000. One open position already deploying Rs 20,000.
    _seed_trade(db, "open1", 0.0, TradeResult.OPEN, TradeStatus.OPEN, investment_amount=20000.0)
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.allowed is False
    assert decision.reason == "MAX_CAPITAL_DEPLOYED"


def test_check_risk_gates_daily_loss_takes_priority_over_other_gates():
    # Both the daily-loss floor and the concurrent-position cap are breached
    # at once -- the reason reported must be the first check in the fixed
    # order (daily loss), not whichever gate happens to be checked last.
    db = _make_db()
    settings = AgenticAISettings(
        id=1, account_capital_rupees=100000.0, max_daily_loss_percent=3.0, max_concurrent_positions=1,
    )
    db.add(settings)
    _seed_trade(db, "closed1", -5000.0, "LOSS", TradeStatus.CLOSED, exit_time=_ist_exit(2026, 10, 25))
    _seed_trade(db, "open1", 0.0, TradeResult.OPEN, TradeStatus.OPEN)
    db.commit()

    decision = check_risk_gates(db, settings, _ist_now(2026, 10, 25))
    assert decision.reason == "DAILY_LOSS_LIMIT"


def test_risk_decision_is_a_plain_frozen_dataclass():
    decision = RiskDecision(True, "OK", "detail")
    assert decision.allowed is True
    assert decision.reason == "OK"
    assert decision.detail == "detail"


# ---------------------------------------------------------------------------
# evaluate_risk_now
# ---------------------------------------------------------------------------


def test_evaluate_risk_now_returns_a_flat_dict_shape():
    db = _make_db()
    result = evaluate_risk_now(db)
    assert set(result.keys()) == {"allowed", "reason", "detail", "evaluated_at"}
    assert result["allowed"] is True
    assert result["reason"] == "OK"


def test_evaluate_risk_now_reflects_a_real_blocking_condition():
    db = _make_db()
    settings = AgenticAISettings(id=1, max_concurrent_positions=1)
    db.add(settings)
    _seed_trade(db, "open1", 0.0, TradeResult.OPEN, TradeStatus.OPEN)
    db.commit()

    result = evaluate_risk_now(db)
    assert result["allowed"] is False
    assert result["reason"] == "MAX_CONCURRENT_POSITIONS"


# ---------------------------------------------------------------------------
# Route-level: the sizing calculator and the GET page's size_result wiring
# ---------------------------------------------------------------------------


def test_position_size_route_redirects_with_query_params():
    response = compute_agentic_ai_position_size(entry_price=100.0, stop_loss_price=90.0, lot_size=75)
    assert response.status_code == 303
    assert "size_entry=100.0" in response.headers["location"]
    assert "size_stop=90.0" in response.headers["location"]
    assert "size_lot=75" in response.headers["location"]

# GET /agentic-ai's own size_result computation (reading the query params and
# calling compute_position_size with the resolved risk settings) is covered
# directly above via compute_position_size/resolve_risk_settings -- the page
# route itself renders a real Jinja template and is checked by starting a
# real server, per this project's established live-verification convention
# for template-rendering routes, not a direct in-process call here.
