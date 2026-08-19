from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db_models import Base, StrategyTrade, StrategyTradeTick, TradeResult, TradeStatus
from app.time_utils import utc_now
from scripts.confidence_by_provider_backtest import (
    CLAUDE_CANDIDATE_FLOORS,
    Entry,
    _bootstrap_mean_diff,
    _floor_bootstrap,
    _load_entries,
    _provider_from_origin,
    run_claude_analysis,
    run_openai_reconfirmation,
)


def _make_db(tmp_path):
    path = tmp_path / "trading.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    return path, Session(engine)


def _add_trade(db, *, trade_id, confidence, pnl_percent, entry_price=100.0,
                result=TradeResult.LOSS, origin="AI_ORIGIN_CLAUDE", status=TradeStatus.CLOSED,
                index_symbol="BANKNIFTY"):
    db.add(StrategyTrade(
        trade_id=trade_id, strategy_name="AI Origination - Bank Nifty", signal="BUY_CE",
        index_symbol=index_symbol, tradingsymbol="X", symboltoken="1", strike=57000,
        expiry="28AUG2026", option_type="CE", quantity=35,
        entry_price=entry_price, stoploss=entry_price * 0.9, target=entry_price * 1.2,
        entry_time=utc_now(), origin=origin, status=status, result=result,
        pnl_percent=pnl_percent, ai_confidence=confidence,
    ))


def _add_ticks(db, trade_id, premiums):
    for premium in premiums:
        db.add(StrategyTradeTick(trade_id=trade_id, premium=premium))


def test_provider_from_origin_maps_known_suffixes():
    assert _provider_from_origin("AI_ORIGIN_OPENAI") == "openai"
    assert _provider_from_origin("AI_ORIGIN_CLAUDE") == "claude"


def test_provider_from_origin_returns_none_for_unrecognised_suffix():
    assert _provider_from_origin("AI_ORIGIN_GEMINI") is None
    assert _provider_from_origin("SIGNAL") is None
    assert _provider_from_origin("") is None


def test_load_entries_tags_each_row_with_its_provider(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="c1", confidence=0.3, pnl_percent=-1.0, origin="AI_ORIGIN_CLAUDE")
    _add_trade(db, trade_id="o1", confidence=0.8, pnl_percent=1.5, origin="AI_ORIGIN_OPENAI")
    db.commit()
    db.close()

    entries = {e.trade_id: e for e in _load_entries(str(path))}
    assert entries["c1"].provider == "claude"
    assert entries["o1"].provider == "openai"


def test_load_entries_skips_unrecognised_provider_suffix(tmp_path, caplog):
    path, db = _make_db(tmp_path)
    # origin LIKE 'AI_ORIGIN_%' still matches a hypothetical third provider --
    # _load_entries must skip it (and warn) rather than crash or miscount it
    # into either known bucket.
    _add_trade(db, trade_id="g1", confidence=0.5, pnl_percent=1.0, origin="AI_ORIGIN_GEMINI")
    db.commit()
    db.close()

    with caplog.at_level("WARNING"):
        entries = _load_entries(str(path))
    assert entries == []
    assert any("origin matching" in r.message for r in caplog.records)


def test_load_entries_computes_mfe_mae_from_ticks(tmp_path):
    path, db = _make_db(tmp_path)
    _add_trade(db, trade_id="c1", confidence=0.3, pnl_percent=-0.42, entry_price=100.0)
    _add_ticks(db, "c1", [100.0, 103.84, 96.98])
    db.commit()
    db.close()

    entries = _load_entries(str(path))
    assert abs(entries[0].mfe_percent - 3.84) < 1e-6
    assert abs(entries[0].mae_percent - (-3.02)) < 1e-6


def _entry(provider, confidence, pnl_percent):
    return Entry(
        trade_id="t", provider=provider, index_symbol="BANKNIFTY", confidence=confidence,
        pnl_percent=pnl_percent, mfe_percent=None, mae_percent=None, is_win=(pnl_percent > 0),
    )


def test_floor_bootstrap_detects_a_real_gap(caplog):
    below = [_entry("claude", 0.15, -6.0) for _ in range(10)]
    above = [_entry("claude", c, 2.0) for c in [0.40, 0.45, 0.55, 0.60, 0.65] for _ in range(4)]
    with caplog.at_level("INFO"):
        _floor_bootstrap(below + above, 0.20, label_prefix="Claude ")
    messages = "\n".join(r.message for r in caplog.records)
    assert "reliably WORSE below floor" in messages


def test_floor_bootstrap_reports_no_reliable_difference_when_there_is_none(caplog):
    below = [_entry("claude", 0.15, pnl) for pnl in [-2.0, 1.0, -1.0, 2.0, -3.0] * 4]
    above = [_entry("claude", 0.55, pnl) for pnl in [-2.0, 1.0, -1.0, 2.0, -3.0] * 4]
    with caplog.at_level("INFO"):
        _floor_bootstrap(below + above, 0.20, label_prefix="Claude ")
    messages = "\n".join(r.message for r in caplog.records)
    assert "no reliable difference at this sample size" in messages


def test_floor_bootstrap_too_thin_reports_gracefully(caplog):
    with caplog.at_level("INFO"):
        _floor_bootstrap([_entry("claude", 0.1, -1.0)], 0.20, label_prefix="Claude ")
    messages = "\n".join(r.message for r in caplog.records)
    assert "too few observations" in messages


def test_run_openai_reconfirmation_only_reads_openai_entries(caplog):
    claude_only = [_entry("claude", 0.3, -5.0) for _ in range(25)]
    with caplog.at_level("INFO"):
        run_openai_reconfirmation(claude_only)
    messages = "\n".join(r.message for r in caplog.records)
    assert "n=0" in messages
    assert "nothing to reconfirm" in messages


def test_run_claude_analysis_only_reads_claude_entries(caplog):
    openai_only = [_entry("openai", 0.8, 3.0) for _ in range(10)]
    with caplog.at_level("INFO"):
        run_claude_analysis(openai_only)
    messages = "\n".join(r.message for r in caplog.records)
    assert "cannot assess" in messages


def test_run_claude_analysis_flags_when_entire_population_is_thin(caplog):
    thin = [_entry("claude", c, 1.0) for c in [0.1, 0.2, 0.3, 0.4, 0.5]]
    with caplog.at_level("INFO"):
        run_claude_analysis(thin)
    messages = "\n".join(r.message for r in caplog.records)
    assert "below the 20-observation trust minimum" in messages


def test_run_claude_analysis_sweeps_every_candidate_floor(caplog):
    entries = [_entry("claude", c, 1.0) for c in [0.1, 0.25, 0.4, 0.6]] * 6
    with caplog.at_level("INFO"):
        run_claude_analysis(entries)
    messages = "\n".join(r.message for r in caplog.records)
    for floor in CLAUDE_CANDIDATE_FLOORS:
        assert f"floor={floor:.2f}" in messages
