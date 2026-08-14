from __future__ import annotations

from app.ai.originator import _MIN_CONFIDENCE_TO_ACT, _Decision, _clears_confidence_floor


def _decision(confidence: float | None, action: str = "BUY_CE") -> _Decision:
    return _Decision(action=action, confidence=confidence, sl_percent=10.0, target_percent=20.0, reasoning="test")


def test_floor_is_the_backtested_0_60_value():
    # scripts/confidence_sizing_backtest.py against 185 closed AI Origination
    # trades: <0.60 (n=28) had 25.0% win rate / -5.35% mean P&L / -8.57% mean
    # MAE, reliably worse than every bucket at 0.60+ (bootstrap 90% CI on the
    # mean P&L gap excludes zero). See CLAUDE.md's "AI confidence /
    # hedging-language sizing backtest" entry for the full numbers.
    assert _MIN_CONFIDENCE_TO_ACT == 0.60


def test_below_floor_is_blocked():
    assert _clears_confidence_floor(_decision(0.59)) is False


def test_at_floor_is_allowed():
    assert _clears_confidence_floor(_decision(0.60)) is True


def test_above_floor_is_allowed():
    assert _clears_confidence_floor(_decision(0.77)) is True


def test_just_below_floor_is_blocked():
    # The three trigger trades from the roadmap (0.55, 0.55, 0.77) -- the two
    # lowest must be blocked under the new floor.
    assert _clears_confidence_floor(_decision(0.55)) is False


def test_missing_confidence_is_treated_as_below_floor():
    assert _clears_confidence_floor(_decision(None)) is False


def test_helper_does_not_care_about_action_type():
    # The gate is applied by the caller only when action is BUY_CE/BUY_PE --
    # the helper itself is a pure confidence check, action-agnostic.
    assert _clears_confidence_floor(_decision(0.9, action="NONE")) is True
    assert _clears_confidence_floor(_decision(0.1, action="NONE")) is False
