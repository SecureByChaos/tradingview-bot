from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db_models import Base, StrategyTrade
from scripts.pull_option_candles import _traded_contracts


def _make_session_factory(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_trade(db, **overrides):
    fields = dict(
        trade_id=f"t-{overrides.get('tradingsymbol', 'X')}", strategy_name="Quick Scalp - Bank Nifty",
        signal="BUY_CE", index_symbol="BANKNIFTY", option_type="CE", tradingsymbol="BANKNIFTY29SEP26C54600",
        symboltoken="111", exchange="NFO", strike=54600, expiry="29SEP2026", quantity=150,
        entry_price=100.0, target=103.75, stoploss=97.5, entry_time=datetime(2026, 9, 15, 5, 0),
        origin="QUICK_SCALP",
    )
    fields.update(overrides)
    db.add(StrategyTrade(**fields))


def test_traded_contracts_default_pattern_matches_both_ai_origination_providers(tmp_path, monkeypatch):
    factory = _make_session_factory(tmp_path)
    monkeypatch.setattr("scripts.pull_option_candles.SessionLocal", factory)
    with factory() as db:
        _seed_trade(db, trade_id="t1", tradingsymbol="A", symboltoken="1", origin="AI_ORIGIN_OPENAI")
        _seed_trade(db, trade_id="t2", tradingsymbol="B", symboltoken="2", origin="AI_ORIGIN_CLAUDE")
        _seed_trade(db, trade_id="t3", tradingsymbol="C", symboltoken="3", origin="QUICK_SCALP")
        db.commit()

    contracts = _traded_contracts(date(2026, 9, 1), date(2026, 9, 30))
    symbols = {c["tradingsymbol"] for c in contracts}
    assert symbols == {"A", "B"}


def test_traded_contracts_exact_origin_pulls_only_that_strategy(tmp_path, monkeypatch):
    factory = _make_session_factory(tmp_path)
    monkeypatch.setattr("scripts.pull_option_candles.SessionLocal", factory)
    with factory() as db:
        _seed_trade(db, trade_id="t1", tradingsymbol="A", symboltoken="1", origin="AI_ORIGIN_OPENAI")
        _seed_trade(db, trade_id="t2", tradingsymbol="B", symboltoken="2", origin="QUICK_SCALP")
        db.commit()

    contracts = _traded_contracts(date(2026, 9, 1), date(2026, 9, 30), origin_pattern="QUICK_SCALP")
    symbols = {c["tradingsymbol"] for c in contracts}
    assert symbols == {"B"}


def test_traded_contracts_respects_date_window_regardless_of_origin(tmp_path, monkeypatch):
    factory = _make_session_factory(tmp_path)
    monkeypatch.setattr("scripts.pull_option_candles.SessionLocal", factory)
    with factory() as db:
        _seed_trade(
            db, trade_id="t1", tradingsymbol="OLD", symboltoken="1", origin="QUICK_SCALP",
            entry_time=datetime(2026, 8, 1, 5, 0),
        )
        _seed_trade(
            db, trade_id="t2", tradingsymbol="NEW", symboltoken="2", origin="QUICK_SCALP",
            entry_time=datetime(2026, 9, 15, 5, 0),
        )
        db.commit()

    contracts = _traded_contracts(date(2026, 9, 1), date(2026, 9, 30), origin_pattern="QUICK_SCALP")
    symbols = {c["tradingsymbol"] for c in contracts}
    assert symbols == {"NEW"}


def test_traded_contracts_returns_empty_for_unmatched_origin(tmp_path, monkeypatch):
    factory = _make_session_factory(tmp_path)
    monkeypatch.setattr("scripts.pull_option_candles.SessionLocal", factory)
    with factory() as db:
        _seed_trade(db, trade_id="t1", tradingsymbol="A", symboltoken="1", origin="VALIDATED_SIGNAL")
        db.commit()

    contracts = _traded_contracts(date(2026, 9, 1), date(2026, 9, 30), origin_pattern="QUICK_SCALP")
    assert contracts == []
