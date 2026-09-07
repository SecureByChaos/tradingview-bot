from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai import originator
from app.ai.originator import run_origination_checks
from app.ai.repository import create_settings
from app.dashboard_routes import update_ai_settings_page
from app.db_models import AIOriginationLog, AISettings, Base, IndexConfig
from app.time_utils import IST


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _make_index(symbol: str = "NIFTY") -> IndexConfig:
    return IndexConfig(symbol=symbol, display_name=symbol, enabled=True, ai_origination_live_trade=False)


class FakeSmartAPI:
    def __init__(self, price: float = 24300.0) -> None:
        self.price = price

    def get_index_spot(self, _index) -> float:
        return self.price


class _ExplodingOptionFinder:
    def find_atm_contract(self, *args, **kwargs):
        raise AssertionError("option_finder must never be touched -- AI Origination is paused")


def test_run_origination_checks_skips_entirely_when_ai_origination_enabled_is_false(monkeypatch, caplog):
    # 7 Sep 2026: requested as a way to pause AI Origination for a few days
    # without also stopping Autonomous AI, which reads the same shared
    # enabled/mode pair (see the companion test below). enabled=True and
    # mode="LIVE" here confirm the new flag is a genuinely separate gate,
    # not just a restatement of the existing one.
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", ai_origination_enabled=False)
    db.commit()

    monkeypatch.setattr(originator, "utc_now", lambda: datetime(2026, 8, 31, 11, 0, tzinfo=IST))
    monkeypatch.setattr(
        originator, "_load_market_context",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must never build context while paused")),
    )
    monkeypatch.setattr(
        originator, "_call_provider",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must never call the model while paused")),
    )

    with caplog.at_level("INFO"):
        run_origination_checks(FakeSmartAPI(), _ExplodingOptionFinder(), db=db)

    assert "AI Origination paused" in caplog.text
    assert db.query(AIOriginationLog).count() == 0


def test_run_origination_checks_runs_normally_when_ai_origination_enabled_is_true(monkeypatch):
    # Control: the new column defaults to True, so a settings row created
    # without mentioning it at all must behave exactly as before this change.
    db = _make_session()
    db.add(_make_index())
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", secondary_enabled=False)
    db.commit()

    assert db.query(AISettings).one().ai_origination_enabled is True

    monkeypatch.setattr(originator, "utc_now", lambda: datetime(2026, 8, 31, 11, 0, tzinfo=IST))
    calls = []
    monkeypatch.setattr(
        originator, "_load_market_context",
        lambda *a, **k: calls.append(1) or (None, False),
    )

    run_origination_checks(FakeSmartAPI(), _ExplodingOptionFinder(), db=db)

    assert calls  # context building was actually reached, unlike the paused case above


def test_run_autonomous_checks_is_unaffected_by_ai_origination_enabled(monkeypatch):
    # The whole point of this flag: pausing AI Origination via
    # ai_origination_enabled=False must NOT also pause Autonomous AI, which
    # reads the same shared enabled/mode pair originator.py checks first.
    from app.ai import autonomous as autonomous_module
    from app.ai.autonomous import run_autonomous_checks

    db = _make_session()
    db.add(IndexConfig(
        symbol="BANKNIFTY", display_name="Bank Nifty", enabled=True,
        exchange_segment="NFO", instrument_name="BANKNIFTY",
        spot_exchange="NSE", spot_symbol="NIFTY BANK", spot_token="99926009",
    ))
    create_settings(db, id=1, enabled=True, mode="LIVE", provider="openai", ai_origination_enabled=False)
    db.commit()

    monkeypatch.setattr(autonomous_module, "utc_now", lambda: datetime(2026, 8, 31, 10, 0, tzinfo=IST))
    monkeypatch.setattr(
        autonomous_module, "get_index_live_figures",
        lambda *a, **k: [{"symbol": "BANKNIFTY", "price": 57000.0}],
    )
    monkeypatch.setattr(autonomous_module, "_compute_features", lambda *a, **k: None)

    calls = []

    def _fake_check_entry(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(autonomous_module, "check_autonomous_entry", _fake_check_entry)
    monkeypatch.setattr(autonomous_module, "check_autonomous_exits", lambda *a, **k: None)

    run_autonomous_checks(object(), object(), object(), db=db)

    assert calls, "Autonomous AI must keep running even while ai_origination_enabled is False"


def _settings_db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def test_ai_settings_route_persists_ai_origination_enabled_toggle():
    db = _settings_db_session()

    update_ai_settings_page(
        db=db, mode="SHADOW", provider="dummy", model="", api_key="", base_url="",
        temperature=0.2, timeout_seconds=20, confidence_threshold=60, system_prompt="",
        enabled="on", ai_origination_enabled=None,
    )

    settings = db.query(AISettings).one()
    assert settings.ai_origination_enabled is False

    update_ai_settings_page(
        db=db, mode="SHADOW", provider="dummy", model="", api_key="", base_url="",
        temperature=0.2, timeout_seconds=20, confidence_threshold=60, system_prompt="",
        enabled="on", ai_origination_enabled="on",
    )

    db.refresh(settings)
    assert settings.ai_origination_enabled is True
