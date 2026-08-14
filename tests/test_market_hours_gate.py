from __future__ import annotations

from datetime import datetime

from app.ai import originator
from app.multi_strategy_monitor import MultiStrategyMonitor
from app.time_utils import IST


def _ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class _ExplodingSmartAPI:
    """Any attribute access is a test failure -- the gate must return before
    this object is ever touched."""

    def __getattr__(self, name):
        raise AssertionError(f"SmartAPI.{name} should never be called outside market hours")


class _ExplodingOptionFinder:
    def find_atm_contract(self, *args, **kwargs):
        raise AssertionError("OptionFinder should never be called outside market hours")


def test_run_origination_checks_skips_entirely_on_a_weekend(monkeypatch, caplog):
    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 15, 12, 0))  # Saturday
    with caplog.at_level("INFO"):
        originator.run_origination_checks(_ExplodingSmartAPI(), _ExplodingOptionFinder())
    messages = [r.message for r in caplog.records]
    assert any("Skipped: Signal received on a Saturday" in m for m in messages)


def test_run_origination_checks_skips_entirely_on_an_nse_holiday(monkeypatch, caplog):
    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 1, 26, 12, 0))  # Republic Day
    with caplog.at_level("INFO"):
        originator.run_origination_checks(_ExplodingSmartAPI(), _ExplodingOptionFinder())
    messages = [r.message for r in caplog.records]
    assert any("Skipped: Signal received on an NSE trading holiday" in m for m in messages)


def test_run_origination_checks_skips_entirely_late_evening_on_a_weekday(monkeypatch, caplog):
    # The confirmed 13 Aug incident: 16:00-18:00 IST, well past the 15:30
    # close this gate uses.
    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 13, 17, 0))  # Thursday evening
    with caplog.at_level("INFO"):
        originator.run_origination_checks(_ExplodingSmartAPI(), _ExplodingOptionFinder())
    messages = [r.message for r in caplog.records]
    assert any("Skipped: Signal received outside NSE trading hours" in m for m in messages)


def test_run_origination_checks_still_requires_smartapi_and_option_finder(monkeypatch, caplog):
    # Pre-existing gate must still run first regardless of the new one.
    monkeypatch.setattr(originator, "utc_now", lambda: _ist(2026, 8, 13, 11, 0))  # Thursday, trading hours
    with caplog.at_level("INFO"):
        originator.run_origination_checks(None, None)
    messages = [r.message for r in caplog.records]
    assert any("no smartapi/option_finder available" in m for m in messages)


class _ExplodingManager:
    def monitor_open_trades(self, db):
        raise AssertionError("monitor_open_trades should never be called outside a trading day")


def test_multi_strategy_monitor_tick_skips_entirely_on_a_weekend(monkeypatch):
    import app.multi_strategy_monitor as mod

    monkeypatch.setattr(mod, "utc_now", lambda: _ist(2026, 8, 15, 12, 0))  # Saturday
    monitor = MultiStrategyMonitor(_ExplodingManager(), risk=None, v7_manager=_ExplodingManager())
    monitor.tick()  # must return without raising -- both managers would raise if touched


def test_multi_strategy_monitor_tick_skips_entirely_on_an_nse_holiday(monkeypatch):
    import app.multi_strategy_monitor as mod

    monkeypatch.setattr(mod, "utc_now", lambda: _ist(2026, 1, 26, 12, 0))  # Republic Day
    monitor = MultiStrategyMonitor(_ExplodingManager(), risk=None, v7_manager=_ExplodingManager())
    monitor.tick()


def test_multi_strategy_monitor_tick_does_not_skip_late_evening_on_a_weekday(monkeypatch):
    # Deliberately NOT gated by hour-of-day -- must still reach the real body
    # (and therefore the DB/bot-status check) at 17:00 on an ordinary Thursday,
    # unlike AI Origination's own gate above. SessionLocal/get_or_create_state
    # are faked out so this test is isolated from whatever real trading.db
    # happens to contain in a given environment.
    import app.multi_strategy_monitor as mod
    from app.db_models import BotStatus

    monkeypatch.setattr(mod, "utc_now", lambda: _ist(2026, 8, 13, 17, 0))  # Thursday evening

    class _FakeState:
        status = BotStatus.RUNNING

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mod, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(mod, "get_or_create_state", lambda db: _FakeState())

    reached_manager = {"called": False}

    class _RecordingManager:
        def monitor_open_trades(self, db):
            reached_manager["called"] = True

    class _RiskStub:
        def enforce_daily_loss_limits(self, db):
            pass

    monitor = MultiStrategyMonitor(_RecordingManager(), risk=_RiskStub(), v7_manager=None)
    monitor.tick()
    assert reached_manager["called"] is True
