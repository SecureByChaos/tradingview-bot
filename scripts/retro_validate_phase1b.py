"""Retro-validate the Phase 1b enriched prompt against historical AI Origination entries.

Rebuilds the enriched prompt from stored candles at each historical entry
timestamp, sends it to the same provider/model the live trade used, and
compares the fresh decision against what actually happened -- the "prove it
offline before deploying" step the Phase 1b spec calls for. The candle store
makes this possible: indicators can be recomputed for any past moment because
they're pure functions of stored OHLC (see app/indicators.py's module
docstring on live/backtest equivalence).

Reuses app.ai.originator's own prompt-builder and provider-caller (including
the private ones) rather than reimplementing them, because fidelity to the
exact production code path is the entire point -- a reimplementation that
drifts from the live prompt would validate a prompt nobody actually sends.

Three groups, per the spec:
  GROUP_LOSSES   27 Jul's STOPLOSS losses. Does the enriched prompt say NONE,
                 or flip direction?
  GROUP_WINNERS  the control -- 27 Jul's TRAIL_EXIT/TARGET trades and 28 Jul's
                 winners. Does the enriched prompt still take them? A prompt
                 that avoids every loss by also refusing every winner is a
                 downgrade in disguise. Report net simulated outcome here, not
                 avoided-loss count.
  GROUP_OTHER    everything else in the window (e.g. 29 Jul's entries).

Only trades with spot_at_entry recorded can be replayed -- it's a forward-only
diagnostic field (see docs/ai-origination-roadmap.md) and cannot be
reconstructed for trades that predate it.

Replay is only fully scoreable in one direction: where the enriched prompt
still takes the same trade, the actual outcome is known and used as the
simulated P&L. Where it flips to the opposite side, there is no option
premium history to score against (the candle store holds index candles, not
option premiums) -- those are reported as unscored, not guessed at.

Usage:
    python -m scripts.retro_validate_phase1b
    python -m scripts.retro_validate_phase1b --start 2026-07-27 --end 2026-07-29
    python -m scripts.retro_validate_phase1b --dry-run   # build prompts only, no AI calls/cost
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select

from app.ai.originator import _ProviderView, _build_user_prompt, _call_provider, _prompt_has_defect
from app.ai.repository import get_settings
from app.database import SessionLocal
from app.db_models import AISettings, IndexConfig, StrategyTrade, TradeStatus
from app.market_context import build_market_context
from app.market_data import FIFTEEN_MINUTE, FIVE_MINUTE, ONE_MINUTE, load_bars, resample
from app.time_utils import to_ist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("retro_validate_phase1b")

# Matches originator.py's own live warm-up window (7 days of 1-minute bars).
_CANDLE_LOAD_LIMIT = 3000

GROUP_LOSSES = "27 Jul losses (control: should flip to NONE or opposite)"
GROUP_WINNERS = "Winners -- 27 Jul TRAIL_EXIT/TARGET + 28 Jul (control: must still be taken)"
GROUP_OTHER = "Other entries in window"

_LOSS_DATE = date(2026, 7, 27)
_WINNER_DATES = {date(2026, 7, 27), date(2026, 7, 28)}


def _classify(trade: StrategyTrade, entry_date: date) -> str:
    # Classified from the stored rows themselves (exit_reason/pnl_percent)
    # rather than hardcoded trade_ids, so this still works against a
    # different database or a re-run after new trades have landed.
    if entry_date == _LOSS_DATE and trade.exit_reason == "STOPLOSS" and (trade.pnl_percent or 0) < 0:
        return GROUP_LOSSES
    if entry_date in _WINNER_DATES and trade.exit_reason in ("TRAIL_EXIT", "TARGET"):
        return GROUP_WINNERS
    return GROUP_OTHER


def _provider_from_origin(origin: str) -> str:
    return origin.replace("AI_ORIGIN_", "", 1).strip().lower()


def _provider_view(settings: AISettings, provider: str) -> Optional[_ProviderView]:
    """The _ProviderView for whichever configured slot (primary/secondary)
    matches `provider`, or None if that provider isn't configured in the
    *current* ai_settings -- a trade opened months ago by a provider since
    removed from Settings genuinely can't be replayed with "the same model"."""
    if provider == (settings.provider or "").strip().lower():
        return _ProviderView(settings.provider, settings.model, settings.api_key, settings.base_url, settings.timeout_seconds)
    if settings.secondary_enabled and provider == (settings.secondary_provider or "").strip().lower():
        return _ProviderView(
            settings.secondary_provider, settings.secondary_model, settings.secondary_api_key,
            settings.secondary_base_url, settings.timeout_seconds,
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2026-07-27")
    parser.add_argument("--end", default="2026-07-29")
    parser.add_argument("--dry-run", action="store_true", help="Build prompts only, skip AI calls")
    args = parser.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    with SessionLocal() as session:
        settings = get_settings(session)
        if settings is None:
            logger.error("No ai_settings row -- run the app once first so init_db seeds it.")
            return 1

        indexes = {index.symbol: index for index in session.scalars(select(IndexConfig))}

        trades = [
            trade
            for trade in session.scalars(
                select(StrategyTrade).where(
                    StrategyTrade.origin.like("AI_ORIGIN_%"),
                    StrategyTrade.status == TradeStatus.CLOSED,
                )
            )
            if (entry_ist := to_ist(trade.entry_time)) is not None and start <= entry_ist.date() <= end
        ]
        if not trades:
            logger.error("No closed AI Origination trades between %s and %s.", start, end)
            return 1

        results: dict[str, list[dict]] = {GROUP_LOSSES: [], GROUP_WINNERS: [], GROUP_OTHER: []}
        skipped = 0

        for trade in sorted(trades, key=lambda t: t.entry_time):
            entry_ist = to_ist(trade.entry_time)
            group = _classify(trade, entry_ist.date())
            index = indexes.get(trade.index_symbol)
            if index is None:
                logger.warning("Skipping %s: no IndexConfig row for %s", trade.trade_id, trade.index_symbol)
                skipped += 1
                continue
            if trade.spot_at_entry is None:
                logger.warning(
                    "Skipping %s (%s %s): spot_at_entry not recorded (predates Phase 0) -- cannot replay",
                    trade.trade_id, trade.index_symbol, entry_ist,
                )
                skipped += 1
                continue

            as_of = entry_ist.replace(tzinfo=None)
            bars_1m = load_bars(session, index.symbol, ONE_MINUTE, end=as_of, limit=_CANDLE_LOAD_LIMIT)
            if not bars_1m:
                logger.warning("Skipping %s: no stored 1-minute candles at or before %s", trade.trade_id, as_of)
                skipped += 1
                continue
            bars_5m = resample(bars_1m, FIVE_MINUTE)
            bars_15m = resample(bars_1m, FIFTEEN_MINUTE)
            ctx = build_market_context(index.symbol, bars_1m, bars_5m, bars_15m, trade.spot_at_entry, as_of)
            if ctx is None or ctx.adx is None or ctx.atr_value is None or ctx.supertrend_5m is None or ctx.supertrend_15m is None:
                logger.warning("Skipping %s: insufficient candle history to rebuild context at %s", trade.trade_id, as_of)
                skipped += 1
                continue

            prompt = _build_user_prompt(index, trade.spot_at_entry, ctx, entry_ist)
            if _prompt_has_defect(prompt):
                logger.error("Skipping %s: rebuilt prompt is malformed", trade.trade_id)
                skipped += 1
                continue

            record: dict = {
                "trade": trade, "entry_ist": entry_ist, "prompt": prompt,
                "original_action": trade.ai_action, "new_action": None, "new_reasoning": "",
            }

            if args.dry_run:
                results[group].append(record)
                continue

            provider = _provider_from_origin(trade.origin)
            view = _provider_view(settings, provider)
            if view is None:
                logger.warning("Skipping %s: provider %s not configured in current ai_settings", trade.trade_id, provider)
                skipped += 1
                continue
            decision = _call_provider(provider, view, prompt)
            if decision is None:
                logger.warning("Skipping %s: unrecognised provider %s", trade.trade_id, provider)
                skipped += 1
                continue
            record["new_action"] = decision.action
            record["new_reasoning"] = decision.reasoning
            results[group].append(record)

        _report(results, skipped, args.dry_run)
    return 0


def _report(results: dict[str, list[dict]], skipped: int, dry_run: bool) -> None:
    print(f"\n{skipped} trade(s) skipped (see warnings above for reasons).\n")
    if dry_run:
        print("--dry-run: prompts built, no AI calls made. Nothing to score.\n")
        for group, records in results.items():
            print(f"{group}: {len(records)} trade(s)")
            for record in records[:1]:
                print(record["prompt"])
                print()
        return

    for group, records in results.items():
        if not records:
            print(f"\n{group}: 0 trades")
            continue
        same = [r for r in records if r["new_action"] == r["original_action"]]
        to_none = [r for r in records if r["new_action"] == "NONE"]
        flipped = [
            r for r in records
            if r["new_action"] in ("BUY_CE", "BUY_PE") and r["new_action"] != r["original_action"]
        ]
        errored = [r for r in records if r["new_action"] not in ("BUY_CE", "BUY_PE", "NONE")]

        # Report the group's net simulated outcome -- not how many losses it
        # avoided -- per the spec: a prompt that dodges every loss by also
        # refusing every winner (GROUP_WINNERS) is a downgrade wearing a
        # disguise, and only the net figure exposes that.
        net_simulated = sum(r["trade"].profit_loss or 0 for r in same)
        print(f"\n{group}: {len(records)} trade(s)")
        print(f"  Same call as production : {len(same):>2}  (net simulated P&L Rs {net_simulated:>10,.2f} -- actual outcome applies)")
        print(f"  Now says NONE            : {len(to_none):>2}  (avoided, simulated P&L Rs {0.0:>10,.2f})")
        print(f"  Flipped direction        : {len(flipped):>2}  (no option-premium data for the flipped side -- unscored)")
        print(f"  Provider error/unparsed  : {len(errored):>2}  (unscored)")
        for r in to_none[:5]:
            print(
                f"    NONE  {r['entry_ist'].strftime('%d-%b %H:%M')} {r['trade'].index_symbol} "
                f"(was {r['original_action']}, actual {r['trade'].pnl_percent:+.2f}%): {r['new_reasoning']}"
            )
        for r in flipped[:5]:
            print(
                f"    FLIP  {r['entry_ist'].strftime('%d-%b %H:%M')} {r['trade'].index_symbol} "
                f"{r['original_action']} -> {r['new_action']}: {r['new_reasoning']}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
