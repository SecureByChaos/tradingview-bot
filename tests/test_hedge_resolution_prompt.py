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


def test_system_prompt_requires_checking_resolutions_against_own_context():
    # 26 Aug 2026: a real trade's resolution called a move "fresh" while the
    # same context showed trend_duration_pct_of_session=100.0 -- the
    # resolution-shaped language requirement above doesn't catch a resolution
    # that is specific-sounding but factually inconsistent with data already
    # in the prompt. This is a distinct, additional check.
    assert "checked against the numeric context you were" in SYSTEM_PROMPT
    assert "fresh" in SYSTEM_PROMPT
    assert "70-80%" in SYSTEM_PROMPT


def test_system_prompt_names_move_extent_as_a_second_freshness_check():
    assert "cumulative move since trend start is" in SYSTEM_PROMPT
    assert "several ATR" in SYSTEM_PROMPT


def test_system_prompt_self_consistency_check_also_forces_none():
    # Same escape hatch as the original hedge-resolution requirement --
    # a resolution that fails this check must not be downgraded to a
    # lower-confidence trade, it must become NONE.
    assert "that resolution does not hold" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.count("not a trade at reduced confidence") == 2
