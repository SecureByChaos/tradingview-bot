"""get_autonomous_ai_today_highlights -- same four-piece shape as
get_ai_origination_today_highlights (tests/test_today_highlights.py),
reading AutonomousAILog/origin=="AUTONOMOUS_AI" instead. Replaced the AI-
Origination version on the live dashboard 17 Sep 2026. See CLAUDE.md's
17 Sep entry.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import AutonomousAILog, Base, Candle, IndexConfig, IndexPriceTick, StrategyTrade, TradeResult, TradeStatus, TradingMode
from app.market_data import ONE_MINUTE
from app.platform import (
    _todays_market_direction,
    autonomous_ai_market_alignment_for_day,
    get_autonomous_ai_today_highlights,
    today_ist,
)
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


# ---------------------------------------------------------------------------
# _todays_market_direction (19 Sep 2026) -- "how the market actually moved
# today," and (via index_comparison's alignment field) whether Autonomous
# AI's CE/PE lean for the day matched it.
# ---------------------------------------------------------------------------

def _candle(index_symbol: str, ts_ist, close: float, interval: str = ONE_MINUTE) -> Candle:
    return Candle(index_symbol=index_symbol, interval=interval, ts_ist=ts_ist, open=close, high=close, low=close, close=close)


def test_market_direction_bullish_from_previous_candle_close_to_today():
    db = _make_session()
    today = today_ist()
    yesterday = today - timedelta(days=1)
    db.add(_candle("BANKNIFTY", datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15), 57000.0))
    db.add(_candle("BANKNIFTY", datetime.combine(today, datetime.min.time()) + timedelta(hours=10), 57200.0))
    db.commit()

    result = _todays_market_direction(db, "BANKNIFTY", today)

    assert result["direction"] == "BULLISH"
    assert result["change_percent"] > 0


def test_market_direction_bearish_and_flat_bands():
    db = _make_session()
    today = today_ist()
    yesterday = today - timedelta(days=1)
    db.add(_candle("NIFTY", datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15), 24000.0))
    db.add(_candle("NIFTY", datetime.combine(today, datetime.min.time()) + timedelta(hours=10), 23900.0))
    db.commit()
    bearish = _todays_market_direction(db, "NIFTY", today)
    assert bearish["direction"] == "BEARISH"

    db2 = _make_session()
    db2.add(_candle("NIFTY", datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15), 24000.0))
    db2.add(_candle("NIFTY", datetime.combine(today, datetime.min.time()) + timedelta(hours=10), 24010.0))
    db2.commit()
    flat = _todays_market_direction(db2, "NIFTY", today)
    assert flat["direction"] == "FLAT"


def test_market_direction_falls_back_to_index_price_ticks_when_no_candles():
    db = _make_session()
    today = today_ist()
    yesterday = today - timedelta(days=1)
    db.add(IndexPriceTick(index_symbol="NIFTY", price=24000.0, recorded_at=datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15)))
    db.add(IndexPriceTick(index_symbol="NIFTY", price=24200.0, recorded_at=datetime.combine(today, datetime.min.time()) + timedelta(hours=10)))
    db.commit()

    result = _todays_market_direction(db, "NIFTY", today)

    assert result["direction"] == "BULLISH"


def test_market_direction_unknown_with_no_data_at_all():
    db = _make_session()
    result = _todays_market_direction(db, "NIFTY", today_ist())
    assert result == {"change_percent": None, "direction": "UNKNOWN"}


def test_index_comparison_alignment_matches_ce_lean_to_bullish_day():
    db = _make_session()
    _seed_indexes(db)
    today = today_ist()
    yesterday = today - timedelta(days=1)
    db.add(_candle("BANKNIFTY", datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15), 57000.0))
    db.add(_candle("BANKNIFTY", datetime.combine(today, datetime.min.time()) + timedelta(hours=10), 57500.0))
    now = utc_now()
    db.add(_trade(trade_id="t1", index_symbol="BANKNIFTY", option_type="CE", exit_time=now))
    db.add(_trade(trade_id="t2", index_symbol="BANKNIFTY", option_type="CE", exit_time=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)
    entry = next(e for e in result["index_comparison"] if e["symbol"] == "BANKNIFTY")

    assert entry["market_direction"] == "BULLISH"
    assert entry["ce_count"] == 2
    assert entry["pe_count"] == 0
    assert entry["alignment"] == "ALIGNED"


def test_index_comparison_alignment_flags_pe_lean_on_bullish_day_as_misaligned():
    db = _make_session()
    _seed_indexes(db)
    today = today_ist()
    yesterday = today - timedelta(days=1)
    db.add(_candle("BANKNIFTY", datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15), 57000.0))
    db.add(_candle("BANKNIFTY", datetime.combine(today, datetime.min.time()) + timedelta(hours=10), 57500.0))
    now = utc_now()
    db.add(_trade(trade_id="t1", index_symbol="BANKNIFTY", option_type="PE", exit_time=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)
    entry = next(e for e in result["index_comparison"] if e["symbol"] == "BANKNIFTY")

    assert entry["alignment"] == "MISALIGNED"


def test_index_comparison_alignment_is_no_trades_when_nothing_closed():
    db = _make_session()
    _seed_indexes(db)
    today = today_ist()
    yesterday = today - timedelta(days=1)
    db.add(_candle("BANKNIFTY", datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15), 57000.0))
    db.add(_candle("BANKNIFTY", datetime.combine(today, datetime.min.time()) + timedelta(hours=10), 57500.0))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)
    entry = next(e for e in result["index_comparison"] if e["symbol"] == "BANKNIFTY")

    assert entry["alignment"] == "NO_TRADES"


def test_index_comparison_alignment_is_no_clear_direction_when_market_unknown():
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    db.add(_trade(trade_id="t1", index_symbol="BANKNIFTY", option_type="CE", exit_time=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)
    entry = next(e for e in result["index_comparison"] if e["symbol"] == "BANKNIFTY")

    assert entry["market_direction"] == "UNKNOWN"
    assert entry["alignment"] == "NO_CLEAR_DIRECTION"


def test_index_comparison_alignment_is_mixed_when_ce_and_pe_counts_tie():
    db = _make_session()
    _seed_indexes(db)
    today = today_ist()
    yesterday = today - timedelta(days=1)
    db.add(_candle("BANKNIFTY", datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=15), 57000.0))
    db.add(_candle("BANKNIFTY", datetime.combine(today, datetime.min.time()) + timedelta(hours=10), 57500.0))
    now = utc_now()
    db.add(_trade(trade_id="t1", index_symbol="BANKNIFTY", option_type="CE", exit_time=now))
    db.add(_trade(trade_id="t2", index_symbol="BANKNIFTY", option_type="PE", exit_time=now))
    db.commit()

    result = get_autonomous_ai_today_highlights(db)
    entry = next(e for e in result["index_comparison"] if e["symbol"] == "BANKNIFTY")

    assert entry["alignment"] == "MIXED"


def test_market_alignment_for_day_works_for_a_past_day_not_just_today():
    # 22 Sep 2026: this is the whole point of extracting the function --
    # a day that is no longer "today" must still produce a real comparison,
    # not the UNKNOWN/NO_TRADES defaults a today_ist()-scoped call would give
    # once the date has rolled past it.
    db = _make_session()
    _seed_indexes(db)
    target_day = today_ist() - timedelta(days=3)
    day_before = target_day - timedelta(days=1)
    db.add(_candle("BANKNIFTY", datetime.combine(day_before, datetime.min.time()) + timedelta(hours=15), 57000.0))
    db.add(_candle("BANKNIFTY", datetime.combine(target_day, datetime.min.time()) + timedelta(hours=10), 57200.0))
    db.add(
        _trade(
            trade_id="t-past",
            index_symbol="BANKNIFTY",
            option_type="CE",
            result=TradeResult.WIN,
            exit_time=datetime.combine(target_day, datetime.min.time()) + timedelta(hours=9),
        )
    )
    db.commit()

    result = autonomous_ai_market_alignment_for_day(db, target_day)
    entry = next(e for e in result if e["symbol"] == "BANKNIFTY")

    assert entry["market_direction"] == "BULLISH"
    assert entry["trades"] == 1
    assert entry["ce_count"] == 1
    assert entry["alignment"] == "ALIGNED"


def test_market_alignment_for_day_ignores_trades_closed_on_other_days():
    db = _make_session()
    _seed_indexes(db)
    target_day = today_ist() - timedelta(days=2)
    other_day = target_day - timedelta(days=1)
    db.add(_trade(trade_id="t-other-day", index_symbol="BANKNIFTY", exit_time=datetime.combine(other_day, datetime.min.time()) + timedelta(hours=9)))
    db.commit()

    result = autonomous_ai_market_alignment_for_day(db, target_day)
    entry = next(e for e in result if e["symbol"] == "BANKNIFTY")

    assert entry["trades"] == 0
    assert entry["alignment"] == "NO_TRADES"


def test_today_highlights_index_comparison_matches_extracted_function_for_today():
    # Confirms the 22 Sep 2026 refactor (extracting the loop out of
    # get_autonomous_ai_today_highlights) is behavior-preserving for the
    # live dashboard's own "today" case.
    db = _make_session()
    _seed_indexes(db)
    now = utc_now()
    db.add(_trade(trade_id="t1", index_symbol="BANKNIFTY", option_type="CE", exit_time=now))
    db.commit()

    highlights = get_autonomous_ai_today_highlights(db)
    extracted = autonomous_ai_market_alignment_for_day(db, today_ist())

    assert highlights["index_comparison"] == extracted
