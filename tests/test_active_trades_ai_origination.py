from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, TradeStatus, TradingMode
from app.platform import get_open_trades_with_ticks, origin_label
from app.time_utils import utc_now


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _trade(**overrides) -> StrategyTrade:
    fields = dict(
        trade_id="t-1",
        strategy_name="BNV7",
        signal="BUY_CE",
        index_symbol="BANKNIFTY",
        tradingsymbol="BANKNIFTY28AUG2657800CE",
        symboltoken="123",
        strike=57800,
        expiry="28AUG2026",
        option_type="CE",
        quantity=35,
        entry_price=100.0,
        stoploss=90.0,
        target=120.0,
        entry_time=utc_now(),
        origin="SIGNAL",
        status=TradeStatus.OPEN,
        mode=TradingMode.PAPER,
    )
    fields.update(overrides)
    return StrategyTrade(**fields)


def test_includes_open_ai_origin_trades_alongside_signal_trades():
    # 15 Aug 2026: the AI Origination page is gone -- its open positions
    # (paper and live alike) now surface here on the main dashboard instead.
    db = _make_session()
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.add(_trade(trade_id="t-claude", origin="AI_ORIGIN_CLAUDE"))
    db.add(_trade(trade_id="t-openai", origin="AI_ORIGIN_OPENAI"))
    db.commit()

    trades = get_open_trades_with_ticks(db)

    assert {t["trade_id"] for t in trades} == {"t-signal", "t-claude", "t-openai"}


def test_still_excludes_ai_alt_shadow_trades():
    # AI_ALT_* is a separate shadow/comparison feature, never a position
    # anyone is holding -- must not be swept in by a loose origin filter.
    # CLAUDE.md: match with LIKE 'AI_ORIGIN_%%', never != 'SIGNAL'.
    db = _make_session()
    db.add(_trade(trade_id="t-signal", origin="SIGNAL"))
    db.add(_trade(trade_id="t-alt", origin="AI_ALT_CLAUDE"))
    db.commit()

    trades = get_open_trades_with_ticks(db)

    assert [t["trade_id"] for t in trades] == ["t-signal"]


def test_excludes_closed_ai_origin_trades():
    db = _make_session()
    db.add(_trade(trade_id="t-open", origin="AI_ORIGIN_CLAUDE", status=TradeStatus.OPEN))
    db.add(_trade(trade_id="t-closed", origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED))
    db.commit()

    trades = get_open_trades_with_ticks(db)

    assert [t["trade_id"] for t in trades] == ["t-open"]


def test_signal_trade_carries_origin_and_mode_fields():
    db = _make_session()
    db.add(_trade(trade_id="t-signal", origin="SIGNAL", mode=TradingMode.LIVE))
    db.commit()

    trades = get_open_trades_with_ticks(db)

    assert trades[0]["origin"] == "SIGNAL"
    assert trades[0]["source_label"] == "Signal"
    assert trades[0]["mode"] == TradingMode.LIVE


def test_ai_origin_trade_carries_provider_source_label():
    db = _make_session()
    db.add(_trade(trade_id="t-claude", origin="AI_ORIGIN_CLAUDE", mode=TradingMode.PAPER))
    db.commit()

    trades = get_open_trades_with_ticks(db)

    assert trades[0]["origin"] == "AI_ORIGIN_CLAUDE"
    assert trades[0]["source_label"] == "AI Origin · Claude"
    assert trades[0]["mode"] == TradingMode.PAPER


def test_origin_label_helper_covers_signal_ai_origin_and_ai_alt():
    assert origin_label("SIGNAL") == "Signal"
    assert origin_label(None) == "Signal"
    assert origin_label("AI_ORIGIN_CLAUDE") == "AI Origin · Claude"
    assert origin_label("AI_ORIGIN_OPENAI") == "AI Origin · Openai"
    assert origin_label("AI_ALT_CLAUDE") == "AI Alt · Claude"
