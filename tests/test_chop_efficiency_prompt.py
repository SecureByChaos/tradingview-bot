"""Efficiency ratio's prompt-rendering and SYSTEM_PROMPT guidance -- the
formula itself is covered by tests/test_efficiency_ratio.py, and the live
build_market_context wiring by tests/test_market_context_efficiency_wiring.py.
"""

from __future__ import annotations

from datetime import datetime

from app.ai.originator import SYSTEM_PROMPT, _build_user_prompt, _efficiency_ratio_text
from app.db_models import IndexConfig
from app.market_context import Levels, MarketContext
from app.time_utils import IST, utc_now


def _make_context(chop_efficiency_ratio: float | None) -> MarketContext:
    return MarketContext(
        index_symbol="NIFTY", as_of=utc_now(), spot=24300.0,
        levels=Levels(),
        cpr=None,
        adx=28.4, plus_di=25.3, minus_di=15.1, atr_value=118.35, atr_percent=0.49,
        rsi_value=55.2, ema9=24291.7, ema21=24281.35, ema50=24261.9,
        supertrend_5m=1, supertrend_15m=1, supertrend_5m_value=24271.6, supertrend_15m_value=24261.4,
        htf_ema20=24271.8, htf_ema50=24251.3, distance_from_ema21_atr=0.53, day_range_atr_multiple=1.02,
        trend_duration_bars=10, trend_duration_pct_of_session=40.0, move_extent_atr=1.23,
        chop_efficiency_ratio=chop_efficiency_ratio,
    )


def test_efficiency_ratio_text_buckets():
    assert "choppy" in _efficiency_ratio_text(0.1)
    assert "mixed" in _efficiency_ratio_text(0.4)
    assert "clean" in _efficiency_ratio_text(0.8)
    # Boundaries: 0.3 belongs to the mixed bucket, 0.5 to the clean bucket.
    assert "mixed" in _efficiency_ratio_text(0.3)
    assert "clean" in _efficiency_ratio_text(0.5)


def test_prompt_includes_the_efficiency_ratio_line_when_present():
    idx = IndexConfig(symbol="NIFTY", display_name="Nifty 50", enabled=True)
    now_ist = datetime(2026, 8, 27, 11, 0, tzinfo=IST)
    prompt = _build_user_prompt(idx, 24300.0, _make_context(0.22), now_ist, (15, 15))
    assert "Efficiency ratio (last hour): 0.22 -> choppy" in prompt


def test_prompt_omits_the_line_when_efficiency_ratio_is_none():
    # Same omit-don't-fabricate convention as every other TREND AGE field.
    idx = IndexConfig(symbol="NIFTY", display_name="Nifty 50", enabled=True)
    now_ist = datetime(2026, 8, 27, 11, 0, tzinfo=IST)
    prompt = _build_user_prompt(idx, 24300.0, _make_context(None), now_ist, (15, 15))
    assert "Efficiency ratio" not in prompt


def test_system_prompt_tells_the_model_to_weigh_efficiency_ratio_alongside_adx():
    assert "efficiency ratio" in SYSTEM_PROMPT.lower()
    assert "both lagging" in SYSTEM_PROMPT


def test_system_prompt_json_schema_and_earlier_paragraphs_survive():
    # Confirms this addition didn't clobber the surrounding trend-age and
    # confidence-calibration paragraphs it was inserted next to.
    assert '"confidence": 0-1' in SYSTEM_PROMPT
    assert "Weigh how long the current trend has already run" in SYSTEM_PROMPT
