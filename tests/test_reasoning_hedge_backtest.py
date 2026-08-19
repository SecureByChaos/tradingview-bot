from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.reasoning_hedge_backtest import (
    Entry,
    _bootstrap_mean_diff,
    _load_entries,
    _provider_from_origin,
    classify_hedge,
    run_backtest,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, reasoning, pnl_percent, entry_price=100.0,
                result=TradeResult.LOSS, origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED,
                index_symbol="BANKNIFTY"):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, stoploss=entry_price * 0.9, target=entry_price * 1.2,
        entry_time=utc_now(), origin=origin, status=status, result=result,
        pnl_percent=pnl_percent, ai_confidence=0.7, ai_reasoning=reasoning,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


# ---------------------------------------------------------------------------
# classify_hedge
# ---------------------------------------------------------------------------


def test_classify_hedge_matches_direct_hedge_phrase():
    hedged, matched = classify_hedge("this is a cautious rather than strong signal")
    assert hedged is True
    assert "direct_hedge:cautious rather than" in matched


def test_classify_hedge_matches_contradiction_marker():
    hedged, matched = classify_hedge(
        "this is not an ideal fresh entry, but the bearish structure still outweighs the exhaustion risk"
    )
    assert hedged is True
    assert "direct_hedge:not an ideal" in matched
    assert "contradiction_marker:but" in matched


def test_classify_hedge_matches_risk_acknowledgment():
    hedged, matched = classify_hedge("the move is already extended and price is still inside the opening range")
    assert hedged is True
    assert "risk_acknowledgment:already extended" in matched


def test_classify_hedge_case_insensitive():
    hedged, matched = classify_hedge("THE MAIN RISK IS a lack of confirmation")
    assert hedged is True
    assert "risk_acknowledgment:the main risk is" in matched


def test_classify_hedge_false_on_clean_reasoning():
    hedged, matched = classify_hedge("confirmed breakout, developing ADX, no conflicting signals")
    assert hedged is False
    assert matched == []


def test_classify_hedge_false_on_empty_reasoning():
    hedged, matched = classify_hedge("")
    assert hedged is False
    assert matched == []


def test_classify_hedge_records_multiple_categories():
    hedged, matched = classify_hedge(
        "this is a moderate-confidence continuation, however the main risk is a lack of a fresh breakout"
    )
    assert hedged is True
    categories = {m.split(":", 1)[0] for m in matched}
    assert "direct_hedge" in categories
    assert "contradiction_marker" in categories
    assert "risk_acknowledgment" in categories


# ---------------------------------------------------------------------------
# _provider_from_origin
# ---------------------------------------------------------------------------


def test_provider_from_origin_maps_known_suffixes():
    assert _provider_from_origin("AI_ORIGIN_OPENAI") == "openai"
    assert _provider_from_origin("AI_ORIGIN_CLAUDE") == "claude"


def test_provider_from_origin_returns_none_for_unrecognised():
    assert _provider_from_origin("AI_ORIGIN_GEMINI") is None
    assert _provider_from_origin("SIGNAL") is None


# ---------------------------------------------------------------------------
# _load_entries
# ---------------------------------------------------------------------------


def test_load_entries_includes_closed_ai_origination_with_reasoning(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", reasoning="cautious rather than aggressive", pnl_percent=-1.0)
    _add_ticks(db, "t1", [100.0, 103.5, 97.0])
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    assert len(entries) == 1
    e = entries[0]
    assert e.trade_id == "t1"
    assert e.hedged is True
    assert abs(e.mfe_percent - 3.5) < 1e-6
    assert abs(e.mae_percent - (-3.0)) < 1e-6


def test_load_entries_excludes_non_ai_origination(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", reasoning="clean breakout", pnl_percent=1.0, origin="SIGNAL")
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_excludes_open_trades(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", reasoning="clean breakout", pnl_percent=1.0, status=TradeStatus.OPEN)
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_excludes_missing_or_empty_reasoning(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="t1", reasoning="", pnl_percent=1.0)
    _add_trade(db, trade_id="t2", reasoning=None, pnl_percent=1.0)
    db.commit()
    db.close()
    assert _load_entries(str(path)) == []


def test_load_entries_tags_provider(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="c1", reasoning="clean setup", pnl_percent=1.0, origin="AI_ORIGIN_CLAUDE")
    _add_trade(db, trade_id="o1", reasoning="clean setup", pnl_percent=1.0, origin="AI_ORIGIN_OPENAI")
    db.commit()
    db.close()

    entries = {e.trade_id: e for e in _load_entries(str(path))}
    assert entries["c1"].provider == "claude"
    assert entries["o1"].provider == "openai"


# ---------------------------------------------------------------------------
# _bootstrap_mean_diff / run_backtest
# ---------------------------------------------------------------------------


def test_bootstrap_mean_diff_detects_a_real_gap():
    hedged = [-5.0, -6.0, -4.0, -7.0, -5.5] * 5
    not_hedged = [3.0, 4.0, 2.0, 5.0, 3.5] * 5
    lo, hi = _bootstrap_mean_diff(hedged, not_hedged)
    assert hi < 0


def _entry(provider, reasoning, pnl_percent):
    return Entry(
        trade_id="t", provider=provider, index_symbol="BANKNIFTY", reasoning=reasoning,
        pnl_percent=pnl_percent, mfe_percent=None, mae_percent=None, is_win=(pnl_percent > 0),
    )


def test_run_backtest_reports_per_provider_sections_independently(caplog):
    claude_entries = [_entry("claude", "cautious rather than aggressive", -3.0) for _ in range(5)]
    openai_entries = [_entry("openai", "confirmed breakout, no conflict", 2.0) for _ in range(5)]
    with caplog.at_level("INFO"):
        run_backtest(claude_entries + openai_entries)
    messages = "\n".join(r.message for r in caplog.records)
    assert "OPENAI ONLY" in messages
    assert "CLAUDE ONLY" in messages


def test_run_backtest_reports_matched_phrase_frequency(caplog):
    entries = [_entry("claude", "already extended and already run", -1.0) for _ in range(3)]
    with caplog.at_level("INFO"):
        run_backtest(entries)
    messages = "\n".join(r.message for r in caplog.records)
    assert "risk_acknowledgment:already extended" in messages


def test_run_backtest_per_category_bootstrap_detects_an_isolated_effect(caplog):
    # 19 Aug 2026 real-data motivation: contradiction_marker (mostly bare "but")
    # is common and near-breakeven, diluting the aggregate hedged/not-hedged
    # comparison. direct_hedge and risk_acknowledgment traded much worse on
    # their own. The per-category bootstrap must be able to surface a
    # category-specific effect even when it wouldn't survive being pooled
    # with a large, near-neutral category.
    risk_ack_bad = [_entry("openai", "the main risk is a lack of confirmation", -6.0) for _ in range(25)]
    contradiction_neutral = [
        _entry("openai", "developing trend, but ADX remains marginal", pnl)
        for pnl in ([1.0, -1.0] * 40)
    ]
    with caplog.at_level("INFO"):
        run_backtest(risk_ack_bad + contradiction_neutral)
    messages = "\n".join(r.message for r in caplog.records)
    assert "PER-CATEGORY BOOTSTRAP" in messages
    assert "risk_acknowledgment" in messages
    assert "reliably WORSE" in messages


def test_run_backtest_per_category_bootstrap_handles_thin_category(caplog):
    entries = [_entry("openai", "not a strong setup", -1.0)] + [
        _entry("openai", "clean confirmed setup", 1.0) for _ in range(5)
    ]
    with caplog.at_level("INFO"):
        run_backtest(entries)
    messages = "\n".join(r.message for r in caplog.records)
    assert "too few observations for a bootstrap comparison" in messages
