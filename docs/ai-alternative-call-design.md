# AI Alternative-Call on Rejection — Design Notes

Status: Evaluation phase, paper trading only. No live capital involved.

## Problem

Today, `run_shadow_review` (app/ai/shadow.py) runs as a background task *after*
a trade is already opened. Its APPROVE/WATCH/REJECT decision is logged for
analysis only — it never gates or changes what actually gets traded. A
REJECT decision currently means "we noted this was a bad entry," not
"we did something differently."

## Goal

When the AI would REJECT a signal, instead of just logging a rejection, it
should propose an alternative call using the same data it already receives
(EMA9/20/21, EMA gap, VWAP, RSI, ATR, ADX, DI+/DI-, Supertrend, ORB high/low,
volume ratio, trend direction, HTF confirmation, strong candle, sideways
filter, strategy filters). The alternative can be:

- Same direction, adjusted terms (different strike/expiry/entry timing,
  tighter stop-loss/target), or
- A full flip to the opposite side (CE instead of PE), if the model judges
  the original signal is wrong-footed.

The model decides which of these to propose — no restriction to one type.

## Architecture: stays fully async, no webhook latency added

Because the original TradingView signal always executes regardless of what
the AI decides (side-by-side, not a replacement), there was no need to move
the AI call in front of the webhook response after all. It stays exactly
where it already lived: an async background task (`run_shadow_review`) that
runs after the original trade is already open. When its decision is REJECT,
it additionally opens 0-2 more paper trades (one per provider that rejected)
tagged by origin. The webhook response time is unaffected either way.

## Two models, run in parallel

Two providers are already configured (GPT-5.4 mini as primary, Claude Sonnet
as secondary) via the existing dual-provider review path in
`run_shadow_review`. Rather than picking one model for this feature, both
independently propose their own alternative call on rejection. This mirrors
the same "let outcomes decide" principle applied to the alternative-call
concept itself.

## Side-by-side paper execution

On rejection, three paper trades are tracked in parallel, each tagged by
origin so results can be compared later:

- `SIGNAL` — the original TradingView signal, paper-executed as if nothing
  changed (control group).
- `AI_ALT_<primary provider>` — the primary model's proposed alternative.
- `AI_ALT_<secondary provider>` — the secondary model's proposed alternative.

All existing risk controls (daily loss limits, per-strategy risk lock,
cooldown-after-loss) still apply to AI-originated trades — they are not a
side door around the risk system.

## Guardrails: none yet, by design

No deterministic indicator guardrail (e.g. "never flip if ADX < X") is
layered on top of the LLM's judgment during this evaluation phase. The point
of the evaluation is to find out whether the LLM's synthesis of the
indicators it already receives is any good. Adding a hard rule now would
make it impossible to tell whether a good outcome came from the model or
from the guardrail. Guardrails can be added later if a specific, repeatable
failure pattern shows up in the data.

## Execution mode

Auto-execute the alternative calls (no manual approval step) since this is
paper trading only. Confidence-tiered execution (auto vs. suggest vs. log
only) is a refinement to revisit once real confidence-vs-outcome data exists
— not something to lock in from assumptions.

## Notifications

No Telegram integration for this feature for now. Everything is logged
silently. Telegram can be added later once a direction (which model, which
alternative type, what confidence threshold) is chosen from the data.

## Evaluation plan

Run for several weeks of paper trading across enough rejected signals to be
meaningful. Compare, per origin tag:

- Win rate and average P&L
- How often each model proposes a flip vs. an adjusted-terms alternative
- How often "reject with no alternative" would have outperformed both paths

## Open items for later (not yet decided)

- Confidence thresholds for auto-execute vs. suggest vs. log-only
- Whether to add deterministic indicator guardrails after initial data
- Whether/how to bring Telegram notifications back in
- Criteria for ever considering this live-trading ready

## Implementation notes (v1, as built)

- `StrategyTrade` gained `origin` ("SIGNAL" / "AI_ALT_OPENAI" / "AI_ALT_CLAUDE")
  and `source_trade_id` columns, migrated via the existing `_ensure_columns`
  pattern in `app/database.py` (no Alembic in this project).
- `ReviewResult` (app/ai/models.py) gained an `alternative` field
  (`AlternativeCall`: action NONE/ADJUST/FLIP, option_type, sl_percent,
  target_percent, confidence, reasoning), populated by the same prompt/API
  call that produces the APPROVE/WATCH/REJECT decision -- no second LLM call.
- **Scope limitation**: alternatives stay ATM, same as normal entries. The
  model can only vary side (CE/PE) and stop-loss/target percentages, not
  strike/expiry selection -- `OptionFinder.find_atm_contract` doesn't support
  arbitrary strike offsets today, and extending it was out of scope for v1.
  Worth revisiting once initial data shows whether side/terms alone is enough
  signal.
- `app/ai/alternative_trader.py` (new) does the actual paper-execution, and
  hard-codes two safety rules regardless of any setting: it only ever acts on
  PAPER-mode original trades, and never calls `place_market_order`.
- **Bugs found and fixed while wiring this in**: the existing state-machine
  queries (`current_state`, `active_trade_count`, `latest_open_trade_for_option`
  in both `MultiStrategyTradeManager` and `V7Manager`) and the daily-stats
  aggregation (`rebuild_strategy_daily_stats`, `rebuild_daily_stats` in
  `app/platform.py`) didn't filter by origin. Left unfixed, an open or closed
  AI_ALT_* paper trade would have corrupted the real signal's FLAT/LONG state,
  miscounted active-trade and daily-trade-count limits, and could have
  triggered the real strategy's risk lock or daily-loss square-off based on an
  experimental trade's outcome. All of these now filter to `origin == "SIGNAL"`.
  `close_trade`/`_close_trade` in both managers also now skip Telegram sends,
  strategy-stats updates, and exit-time AI review for AI_ALT_* trades.
- Same contamination class also found and fixed in `app/main.py` (the
  shadow-review subject lookup in `queue_shadow_review`, and the
  cooldown-after-recent-loss check in `webhook()`) and in reporting
  (`strategy_metrics` and `_closed_trades_between` in `app/platform.py` /
  `app/reports.py`) so dashboard performance numbers and AI-written periodic
  reports reflect real trading only.
- Dashboard/API trade listings (`/active-trade`, `/trades`, dashboard views)
  intentionally still include AI_ALT_* trades -- `serialize_strategy_trade`
  now exposes `origin` and `source_trade_id` so they're visible and
  distinguishable in the UI, which is the point of the evaluation.
- **Not verified by running the test suite** -- the sandboxed shell was
  unavailable this session, so this was checked by careful manual read-through
  only (including a deliberate second pass specifically hunting for other
  queries that touch `StrategyTrade` without an origin filter). Recommend
  running `pytest` and a manual paper-trade webhook test before trusting this
  in the live evaluation loop.
