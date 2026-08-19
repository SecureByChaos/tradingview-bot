from __future__ import annotations

from app.ai.originator import SYSTEM_PROMPT


def test_system_prompt_requires_resolving_a_stated_risk():
    # 19 Aug 2026: the reasoning-hedge backtest found no reliable outcome
    # correlation for hedge language, at any category -- a statement about
    # detecting the pattern after the fact, not about whether stating a risk
    # and trading through it anyway is sound reasoning. This paragraph
    # changes the decision process itself rather than filtering output.
    assert "resolve that risk explicitly before deciding to trade" in SYSTEM_PROMPT
    assert "must result in NONE" in SYSTEM_PROMPT


def test_system_prompt_names_the_contradiction_markers_to_watch_for():
    # Mirrors the same three conjunctions reasoning_hedge_backtest.py's
    # contradiction_marker category detects, so the model is warned about
    # exactly the pattern the detector (and the trigger trades) found.
    assert '"but,"' in SYSTEM_PROMPT
    assert '"however,"' in SYSTEM_PROMPT
    assert '"although"' in SYSTEM_PROMPT


def test_system_prompt_does_not_accept_a_bare_restatement_as_resolution():
    assert "not a restatement of the risk followed by" in SYSTEM_PROMPT


def test_confidence_calibration_paragraph_still_intact():
    # This change is inserted just before the confidence-calibration
    # paragraph added earlier the same day -- confirm neither edit clobbered
    # the other.
    assert "full 0.0-1.0 range" in SYSTEM_PROMPT
    assert '"confidence": 0-1' in SYSTEM_PROMPT
