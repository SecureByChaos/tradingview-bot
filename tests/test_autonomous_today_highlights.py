"""get_autonomous_ai_today_highlights -- same four-piece shape as
get_ai_origination_today_highlights (tests/test_today_highlights.py),
reading AutonomousAILog/origin=="AUTONOMOUS_AI" instead. Replaced the AI-
Origination version on the live dashboard 17 Sep 2026. See CLAUDE.md's
17 Sep entry.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AutonomousAILog, Base, IndexConfig, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.platform import get_autonomous_ai_today_highlights
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _seed_indexes(db: Session) -> None:
    db.add(IndexConfig(symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True))
    db.add(IndexConfig(symbol="NIFTY", display_name="Nifty 50", enabled=True))
    db.commit()


def _log(**overrides) -> AutonomousAILog:
    fields = dict(
        timestamp=utc_now(),
        index_name="BANKNIFTY",
        raw_decision="NONE",
        confidence=0.8,
        trend_regime="BEARISH",
        reasoning="test reasoning",
    )
    fields.update(overrides)
    return AutonomousAILog(**fields)


def _trade(**overrides) -> StrategyTrade:
    fields = dict(
        trade_id="t-1",
        strategy_name="Autonomous AI - Bank Nifty",
        signal="BUY_CE",
        index_symbol="BANKNIFTY",
        tradingsymbol="X",
        symboltoken="1",
        strike=57000,
        expiry="28AUG2026",
        option_type="CE",
        quantity=35,
        entry_price=100.0,
        stoploss=90.0,
        target=120.0,
        entry_time=utc_now(),
        origin="AUTONOMOUS_AI",
        status=TradeStatus.CLOSED,
        result=TradeResult.WIN,
        mode=TradingMode.PAPER,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_empty_when_nothing_happened_today():
    db = _make_session()
    _seed_indexes(db)

    result = get_autonomous_ai_today_highlights(db)

    assert result["funnel"] == {"total_cycles": 0, "declined": 0, "opened": 0, "blocked": 0, "errors": 0}
    assert result["sharpest_call"] is None
    assert result["near_misses"] == []
    assert {entry["symbol"]: entry["trades"] for entry in result["index_comparison"]} == {"BANKNIFTY": 0, "NIFTY": 0}


def test_funnel_counts_and_excludes_position_open_marker():
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    db.add(_log(raw_decision="NONE", timestamp=now))
    db.add(_log(raw_decision="NONE", timestamp=now))
    db.add(_log(raw_decision="BUY_CE", trade_id="t-opened", timestamp=now))
    db.add(_log(raw_decision="BUY_PE", trade_id=None, timestamp=now))
    db.add(_log(raw_decision="NONE", block_reason="POSITION_OPEN", confidence=None, reasoning=None, timestamp=now))
    db.add(_log(raw_decision="ERROR", confidence=None, timestamp=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)

    assert result["funnel"] == {"total_cycles": 5, "declined": 2, "opened": 1, "blocked": 1, "errors": 1}


def test_index_comparison_uses_net_pnl_and_only_todays_closed_autonomous_trades():
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    # Counts: Bank Nifty win today.
    db.add(_trade(
        trade_id="t-bn-win", index_symbol="BANKNIFTY", result=TradeResult.WIN,
        profit_loss=520.0, net_pnl=500.0, exit_time=now,
    ))
    # Excluded: closed yesterday.
    db.add(_trade(
        trade_id="t-bn-old", index_symbol="BANKNIFTY", result=TradeResult.WIN,
        profit_loss=999.0, net_pnl=999.0, exit_time=now - timedelta(days=1),
    ))
    # Excluded: an AI Origination trade, not Autonomous AI.
    db.add(_trade(
        trade_id="t-origin", index_symbol="BANKNIFTY", origin="AI_ORIGIN_OPENAI", result=TradeResult.WIN,
        profit_loss=999.0, net_pnl=999.0, exit_time=now,
    ))
    # Excluded: still open (no exit today to bucket it under).
    db.add(_trade(trade_id="t-open", index_symbol="BANKNIFTY", status=TradeStatus.OPEN, exit_time=None))
    # Nifty loss today.
    db.add(_trade(
        trade_id="t-nf-loss", index_symbol="NIFTY", result=TradeResult.LOSS,
        profit_loss=-195.0, net_pnl=-200.0, exit_time=now,
    ))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)
    by_symbol = {entry["symbol"]: entry for entry in result["index_comparison"]}

    assert by_symbol["BANKNIFTY"]["trades"] == 1
    assert by_symbol["BANKNIFTY"]["wins"] == 1
    assert by_symbol["BANKNIFTY"]["net_pnl"] == 500.0  # net_pnl, not gross profit_loss (520.0)
    assert by_symbol["NIFTY"]["trades"] == 1
    assert by_symbol["NIFTY"]["losses"] == 1
    assert by_symbol["NIFTY"]["net_pnl"] == -200.0


def test_sharpest_call_picks_the_best_closed_trade_today():
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    db.add(_trade(
        trade_id="t-small-win", result=TradeResult.WIN, pnl_percent=3.0,
        ai_reasoning="a modest winner", exit_time=now,
    ))
    db.add(_trade(
        trade_id="t-big-win", result=TradeResult.WIN, pnl_percent=18.0,
        ai_reasoning="a clean breakout with no conflicting signals", exit_time=now,
    ))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)

    assert result["sharpest_call"]["kind"] == "trade"
    assert result["sharpest_call"]["pnl_percent"] == 18.0
    assert result["sharpest_call"]["reasoning"] == "a clean breakout with no conflicting signals"


def test_sharpest_call_falls_back_to_highest_confidence_none_without_closed_trades():
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    db.add(_log(raw_decision="NONE", confidence=0.4, reasoning="mild caution", timestamp=now))
    db.add(_log(raw_decision="NONE", confidence=0.91, reasoning="the trend is already fully mature", timestamp=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)

    assert result["sharpest_call"]["kind"] == "decline"
    assert result["sharpest_call"]["confidence"] == 0.91
    assert result["sharpest_call"]["reasoning"] == "the trend is already fully mature"


def test_sharpest_call_ignores_position_open_markers_with_no_confidence():
    # A POSITION_OPEN marker's confidence is always None (see check_
    # autonomous_entry) -- it must never be picked as the "highest confidence
    # NONE" fallback.
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    db.add(_log(raw_decision="NONE", block_reason="POSITION_OPEN", confidence=None, reasoning=None, timestamp=now))
    db.add(_log(raw_decision="NONE", confidence=0.6, reasoning="genuine decline", timestamp=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)

    assert result["sharpest_call"]["kind"] == "decline"
    assert result["sharpest_call"]["confidence"] == 0.6


def test_near_misses_only_blocked_decisions_newest_first_capped_at_five():
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    for i in range(7):
        db.add(_log(
            raw_decision="BUY_PE", trade_id=None, confidence=0.5,
            reasoning=f"blocked #{i}", timestamp=now - timedelta(minutes=i),
        ))
    # Excluded: actually opened.
    db.add(_log(raw_decision="BUY_CE", trade_id="t-opened", timestamp=now))
    # Excluded: a genuine decline, not a blocked want-to-trade.
    db.add(_log(raw_decision="NONE", timestamp=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)

    assert len(result["near_misses"]) == 5
    assert result["near_misses"][0]["reasoning"] == "blocked #0"  # newest first


def test_yesterdays_data_is_excluded():
    db = _make_session()
    _seed_indexes(db)
    yesterday = utc_now() - timedelta(days=1)
    db.add(_log(raw_decision="NONE", timestamp=yesterday))
    db.add(_trade(result=TradeResult.WIN, pnl_percent=10.0, exit_time=yesterday))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)

    assert result["funnel"]["total_cycles"] == 0
    assert result["sharpest_call"] is None
    assert all(entry["trades"] == 0 for entry in result["index_comparison"])
