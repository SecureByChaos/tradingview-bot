# CLAUDE.md — StrikeVault working notes

Guidance for Claude Code working in this repo. `README.md` covers setup, deployment,
env vars and the public API — this file covers the things that aren't obvious from
reading the code, and the mistakes that are easy to make in it.

## What this is

FastAPI + SQLAlchemy + SQLite options auto-trading bot for Indian index options
(Bank Nifty, Nifty 50; Sensex planned). TradingView webhooks in, Angel One SmartAPI
execution out. Jinja2 dashboard, APScheduler background jobs, session-based admin auth.

**Everything is PAPER trading unless two independent switches are both on.** Do not
change that without an explicit instruction. See "Live-trading safety" below.

## Ground rules

- **Never place, modify or cancel real orders while exploring.** `SMARTAPI_LIVE_TRADING`
  gates real execution; assume it may be on.
- **Deployment is manual and separate.** Code changes here are local. The live server is
  updated by the user's own push. Never assume a change is live, and say so when
  reporting work.
- **This is real money at stake even in paper mode** — paper results drive live decisions.
  Correctness matters more than speed.

## Architecture beyond the README

### The `origin` field is the isolation mechanism

`StrategyTrade.origin` is what keeps four separate experiments from contaminating each
other. Every query that reports on one of them must filter on it correctly.

| origin | Meaning |
|---|---|
| `SIGNAL` | Real TradingView-signal trade. The only kind that touches risk locks, stats, Telegram. |
| `AI_ALT_OPENAI` / `AI_ALT_CLAUDE` | AI proposed an alternative after rejecting a signal (`app/ai/alternative_trader.py`) |
| `AI_ORIGIN_OPENAI` / `AI_ORIGIN_CLAUDE` | AI opened a position entirely on its own, no signal involved (`app/ai/originator.py`) |

Match with `LIKE 'AI_ALT_%'` / `LIKE 'AI_ORIGIN_%'`, **never** `!= 'SIGNAL'`. That exact
bug once put AI Origination trades on the AI Alternatives page.

### Three AI subsystems, deliberately independent

- `app/ai/validator.py` + `reviewer` — reviews incoming signals (APPROVE/WATCH/REJECT)
- `app/ai/alternative_trader.py` — on REJECT, may open an `AI_ALT_*` paper trade
- `app/ai/exit_shadow.py` — every 3 min, re-reviews open **`SIGNAL`** trades only.
  Observation-only: it can never close anything.
- `app/ai/originator.py` — every 5 min, opens `AI_ORIGIN_*` trades from spot momentum

### Scheduler jobs (`app/scheduler.py`)

| Job | Cadence |
|---|---|
| `trade-monitor` | 30s |
| `ai-exit-shadow-check` | 3 min |
| `ai-origination-check` | 5 min |
| `option-chain-collect` | 5 min, mon-fri 09:00-15:59 IST (archival only) |
| `closing-auction-capture` | 15:45 IST mon-fri (stores the CAS close) |
| `daily-square-off` | 15:15 IST cron |

## Gotchas that have caused real bugs

**SQLite does not round-trip tzinfo**, even on `DateTime(timezone=True)` columns.
`trade.entry_time` can come back offset-naive and blow up a subtraction against an
aware `utc_now()` with `can't subtract offset-naive and offset-aware datetimes`.
Always normalise through `to_ist()` (`app/time_utils.py`) before datetime arithmetic.

**LLMs wrap JSON in markdown fences** despite being told not to. Every JSON parse of a
provider response must go through `extract_json_object()` (`app/ai/json_utils.py`).
Failing to do this surfaces as a useless generic "Invalid AI response."

**Angel One's `/quote` family is 1 req/sec, process-wide.** `get_ltp`, `get_index_ohlc`
and `get_candles` all share `_throttle_quote_call()`. Several independent loops contend
for this budget — never add an unthrottled quote call.

**Angel One returns `open/high/low = 0` for index instruments** fairly often, even when
`close`/`ltp` are populated. `get_index_ohlc()` returns `None` rather than trusting
zeros. Handle the `None`.

**Expired F&O contracts vanish from the historical API.** `getCandleData` only serves
instruments still in the live scrip master. Once a contract expires its intraday history
is gone permanently — there is no archive. Any historical option pull is deadline-bound
by expiry.

**The DTE bucket function is deliberately duplicated and must not diverge.**
`_dte_bucket` exists in both `scripts/backtest/premium.py` (fits the coefficients) and
`app/premium_model.py` (stdlib, reads them at runtime). The two are kept separate so a
live app module never depends on a standalone analysis script.
Divergence fails silently: the calibration writes one bucket name, the live lookup asks
for another, no bucket ever matches, and every contract quietly falls back to an
extrapolated coefficient. Across the real DTE range that is a factor-level error
(Bank Nifty ATM CE ≈ 65 at 2–5 DTE, ≈ 25 at 21+). `tests/test_premium_buckets.py`
enforces parity; buckets are `0-1 / 2-5 / 6-10 / 11-20 / 21+`.

**A captured error is not a logged error.** Every AI Origination ERROR path builds a
specific reason — an HTTP status from `AIClient`, a timeout, a parse failure — into
`_Decision.reasoning`. For a week the cycle log printed only `-> ERROR (claude,
secondary)` and dropped the reason, which read exactly like a swallowing `except` but
wasn't one. Nothing was caught and discarded; the detail was captured and never written.
Fixed 4 Aug: ERROR now logs the reason at ERROR level and persists an event.

**`data_stale` labels, it does not gate.** `_load_market_context` returns a context built
from stored history when the candle refresh fails, sets `data_stale=True`, warns, and
proceeds. The fail-closed rule was implemented for missing ADX/ATR/Supertrend but *not*
for a failed refresh. So a `Data Stale: YES` trade was opened on old data by design, not
by accident — `scripts/stale_data_correlation.py` is the check on whether that choice
costs anything.

**Claude's call path caps output at 256 tokens; OpenAI's has no cap.** `_call_claude`
sets `max_tokens: 256` while `_call_openai` sets `response_format: json_object` and no
limit. A longer prompt means a longer `reasoning` field, and truncated JSON fails
`extract_json_object` — so a prompt change can break one provider and not the other. The
tell is `stop_reason == "max_tokens"`, now logged explicitly.

**Index tokens are one digit apart and easy to confuse.**
Nifty 50 spot = `99926000`, Bank Nifty spot = `99926009`, both NSE. A wrong token here
produces plausible-looking but badly wrong strikes.

**`IndexPriceTick` density depends on whether a browser tab is open.** The recorder is
called both by the origination cycle (5 min) and by live-dashboard polling, throttled to
25s. So the AI's input resolution silently varies from ~3 to 100+ samples. This is a
known confound; `tick_sample_count` now records it per trade.

## The shared-FIXED-branch hazard

`monitor_open_trades` in `app/multi_strategy.py` handles **every non-V7 strategy** —
BNV5.1, BNV6, BNV7, NV1 *and* all AI trades. Its `FIXED` stop/target branch is shared.

The rule-based strategies are the ones currently making money. **Any exit-logic change
must be scoped with `trade.origin.startswith("AI_ORIGIN_")`**, the same way `STALL_EXIT`
and the AI Origination trailing stop already are. Changing the branch wholesale alters
what is working and destroys the single-variable measurement AI Origination changes
depend on.

## Exit paths (AI Origination)

All evaluated on the 30s monitor tick. `sl_mode` is per-trade (`FIXED` unless the model
returned unusable numbers, which it reliably doesn't — so `FIXED` in practice).

| Reason | Condition |
|---|---|
| `STOPLOSS` | premium ≤ stoploss (model-proposed % of entry, accepted 5–50%) |
| `TARGET` | premium ≥ target (model-proposed %) |
| `TRAIL_EXIT` | AI Origination only: after +8% activation, premium ≤ `high − entry×5%` |
| `STALL_EXIT` | AI Origination only: ≥60 min elapsed **and** \|P&L\| ≤ 5% **and** trail never armed |
| `TIME_EXIT` | ≥15:15 IST (end-of-day square-off, not a max-hold) |
| `TV_EXIT` | V7 only — unreachable for anything else |

`TRAIL_EXIT` is deliberately distinct from `STOPLOSS`: one is a rescued winner, the
other a plain loss. Folding them together makes the trailing stop unmeasurable.

TradingView `SELL_*` signals do **not** close trades for any strategy except V7 — they
record an observation via `record_exit_suggestion`.

## AI Origination config snapshot

- Entries only between **09:30 and 15:15 IST** (both hardcoded in `originator.py`)
- Expiry: nearest available, no offset, no expiry-day roll or cutoff
- Strike: nearest ATM, `round(spot / strike_interval) × strike_interval`
- Position size: **exactly 1 lot, always** (`quantity=contract.lot_size`) — capital
  deployed is therefore purely premium-dependent
- Confidence floor to act: 0.55
- Each provider gets its **own independent trade slot per index** — Claude and OpenAI can
  hold concurrent separate positions on the same index
- Paper unless the index has `ai_origination_live_trade` checked **and**
  `SMARTAPI_LIVE_TRADING` is on

### What the model actually sees at entry

Only this: index name, 45-minute lookback window, sample count, earliest price, current
price, % change, window high/low, up/down tick counts, and — best-effort, often absent —
today's session open/high/low/previous-close.

**No** candles, volume, indicators, HTF trend, option chain, OI/IV/PCR, VIX, or its own
track record. It does not know which strike or premium its decision will produce, and it
does not know it already has positions open. Diagnosed failure mode: it reads *being at
an extreme* as directional evidence ("price at session highs, therefore bullish").

## Option-chain archive (collection only)

`app/option_chain.py` snapshots OI, IV, volume, LTP and spot every 5 minutes for both
indices, ATM ± 10 strikes, across the nearest two expiries plus the monthly if the
nearest two missed it (Bank Nifty's already are monthlies; Nifty's usually are not).

**Nothing live reads it and nothing in it is interpreted.** No PCR column — it is a
ratio of summed OI, derivable at query time, and storing it would bake in a strike
range a future analysis may not want. It exists because chain history cannot be
backfilled: Angel serves none, so the archive can only start from the day it starts.
Evaluating it needs months and the same significance machinery the price setups went
through — do not wire it into a signal before that.

It writes to **`data/option_chain.db`, a separate SQLite file**, not the trading DB.
At ~210 rows per snapshot it accumulates on the order of 500 MB/year, which must not
land in the file order placement and risk locks depend on. `--status` projects growth
from measured bytes per row.

**The rate-limit separation is partial and the code says so.** Angel limits per API
key, so a second client on the same credentials buys no budget and merely drops the
process-wide throttle. Set `SMARTAPI_ANALYTICS_*` for a real second key; without it the
collector shares the live budget and stays subordinate — hard per-cycle call cap, and a
full skip for 15 minutes after any rate limit on the live client. Cost is ~7 requests
per cycle either way.

Run `python -m scripts.collect_option_chain --once --probe` after deploying. Field
names in `getMarketData` are not stable across SDK versions, and an archive of null
open interest accumulates silently and looks fine until someone tries to use it.
Confirmed 3 Aug: OI is `opnInterest`, volume `tradeVolume`, both present.

**`impliedVolatility` from `optionGreek` is stored raw and its units are unverified.**
The 3 Aug probe read 5.81 on a Bank Nifty 22-DTE contract whose own premium implies
nearer 15% by a straddle estimate. Reconcile before any analysis leans on it.

## Closing Auction Session (from 3 Aug 2026)

NSE/SEBI added a Closing Auction Session for F&O-eligible **stocks**: continuous
trading ends 15:15, auction runs 15:15–15:35, replacing the VWAP close. Index
**derivatives are not auctioned** and trade continuously to 15:40.

**The index value is frozen 15:15–15:30, not volatile.** Every Nifty/Bank Nifty
constituent is in the auction, so there is no continuous matching underneath the index
and NSE states the value "is constant as it is based on traded values". The 3 Aug
spot/futures divergence is the visible signature — futures keep trading while spot sits
still. `app/market_hours.py` is the single source of truth for these boundaries.

**Live trading is not exposed, by construction.** Entries stop at 15:15
(`_past_trading_end`), and every exit and the square-off price off *option premium* via
`get_ltp`, which is continuously quoted until 15:40. Nothing in the trading path reads
spot during the frozen window.

**What the candles actually look like** (measured 3–4 Aug, `scripts/audit_auction_window.py`):
one flat bar at 15:15 at the last continuous value, then *no bars at all* until a single
bar around 15:29 carrying the auction close. Not fifteen flat bars — the feed emits
nothing when nothing trades.

| | 15:14 (last continuous) | 15:29 (CAS close) |
|---|---|---|
| Bank Nifty 3 Aug | 57,680.90 | **58,247.95** |
| Nifty 3 Aug | 24,573.55 | **24,774.30** |

Both 15:29 values match NSE's published closes exactly. The 200–567 point gap is real
market structure, not a bad print.

**The defect was that nothing fetched candles after 15:15.** AI Origination stops there
and was the only live caller, so the stored session close was the ~15:13 bar and the CAS
close was never written — 3 Aug has it only by accident, from a manual backfill that
evening. `market_context` reads the previous close the next morning for CPR
classification and PDH/PDL levels, and a pivot is (H+L+C)/3, so a close wrong by 567
points moves every derived level. Fixed by the `closing-auction-capture` job at 15:45
(`capture_closing_auction`).

Nothing is held to expiry settlement (TIME_EXIT closes everything at 15:15), so the
CAS-derived settlement value for expiry-day moneyness never applies to a position here.

## Live-trading safety (two-key pattern)

Real orders require **both**:

1. A per-entity opt-in — `StrategyConfig.live_trade` for signal strategies,
   `IndexConfig.ai_origination_live_trade` for AI Origination
2. The server-side `SMARTAPI_LIVE_TRADING` env var

`place_market_order` independently no-ops to a `"PAPER_ORDER"` id if the env var is off,
so a UI checkbox alone can never move real money. Preserve this.

## Costs

`profit_loss` is **gross** and must stay that way — historical analysis is anchored to it.
Cost lives separately in `estimated_cost` / `net_pnl`, computed by `app/trade_costs.py`
(Angel One published rates; slippage is a separate constant defaulting to 0.0 because
paper trading has none). Costs backfill on startup since they're derivable.

## Conventions

- Migrations are additive `ALTER TABLE` statements in `_ensure_columns()`
  (`app/database.py`), guarded by an inspector check. No Alembic.
- New nullable columns mean "not recorded", not a fabricated default. Backfill only when
  the value is genuinely derivable (cost, investment amount) — never invent it.
- Comments explain *why*, especially for anything non-obvious or that encodes a past bug.
  Match the existing density.
- IST display everywhere user-facing; `format_ist()` / the `ist` Jinja filter.
- Scripts live in `scripts/`, run as `python -m scripts.<name>`.

## Commands

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000   # run
pytest                                                      # tests
python -m scripts.reconcile_origination                     # trade population split
python -m scripts.pull_option_candles --dry-run             # option candle pull
python -m scripts.calibrate_premium --db data/trading.db --write   # refit coefficients
python -m scripts.collect_option_chain --status             # chain archive size/coverage
python -m scripts.collect_option_chain --plan               # what would be collected
python -m scripts.collect_option_chain --once --probe       # check broker field names
```

## Current state / open items

### Live index feed now gates on market hours too -- the one SmartAPI-touching path the 14/15 Aug fixes didn't cover (17 Aug 2026)

**Requested**: "Also bot should stop pinging smartapi after market hours." This exact topic already
has two prior entries in this file (14 Aug's scheduler fix, 15 Aug's dashboard-tick fix), so before
writing anything a fresh audit (via a research subagent) checked every SmartAPI-touching code path
in the repo against both fixes rather than assuming either duplicate work or a brand-new problem.

**Confirmed still gated correctly**: `ai-origination-check` (5-min job, `check_market_hours()` at
the top of `run_origination_checks`), `trade-monitor` (30s job, `trading_day_reason()`, weekday/
holiday only -- deliberately not hour-gated, per the 14 Aug entry's own reasoning: it must keep
catching open trades near/after 15:15), `option-chain-collect` (cron window + internal re-check,
unchanged), `closing-auction-capture` (cron time is the gate), and the dashboard's own
`get_index_live_figures` (feed reads are pure in-memory; the tick-write path already gated 15 Aug).

**The real, still-live gap**: `app/live_feed.py`'s `IndexFeed._run()` background thread. Neither
prior fix touched it because both only gated periodic `apscheduler` jobs or dashboard-poll-
triggered REST calls -- this is a persistent thread started once at app startup
(`app/main.py`'s lifespan), not a job. Its reconnect loop had no market-hours awareness at all:
every `_RECONNECT_DELAY_SECONDS` (10s) it read fresh auth tokens and, once available, opened a
real `SmartWebSocketV2` connection to Angel One and subscribed -- all night, every weekend, every
NSE holiday, for as long as the process ran. This is almost certainly what prompted the request.

**Fixed**: `_run()`'s loop now checks `check_market_hours(utc_now())` first, before even looking
at auth tokens. When closed, it skips the connection attempt entirely, marks the feed disconnected
(so the dashboard's stale-badge logic degrades correctly), and sleeps a much longer
`_CLOSED_MARKET_POLL_SECONDS` (300s) rather than the open-market `_RECONNECT_DELAY_SECONDS` (10s)
-- nothing is time-sensitive while the market's shut, and re-deriving "is it open" every 10s all
night is itself pointless work. Logs once on the open->closed transition and once on closed->open,
not on every poll iteration, to avoid ~280 identical log lines over a closed weekend.

**Deliberately NOT built**: forcibly disconnecting an already-open connection the instant trading
hours end. `ws.connect()` blocks for the life of one connection attempt; the gate is only
re-evaluated after a disconnect happens naturally. Scope here was "stop dialing out repeatedly
after hours," not "guarantee the socket is closed by 15:30:01" -- the latter would need a second
thread interrupting a blocking call, real added complexity for a case (an idle overnight WS
session) that costs nothing further once no new ticks are being requested or produced.

**Also fixed while in this area**: `daily-square-off`'s `CronTrigger` (`app/scheduler.py`) gained
`day_of_week="mon-fri"`, matching every other cron job in that file. This was flagged-but-not-fixed
in the 14 Aug entry ("technically fires at 15:15 on Saturday/Sunday too... worth a one-line fix if
anyone's looking at that file again") -- exactly this situation. Low severity on its own
(`square_off_all`'s empty-open-trades early return already made a weekend firing a no-op) but a
real SmartAPI-touching call with no reason to run outside a trading day, same class of gap as the
live feed fix above.

4 new tests: `tests/test_live_feed.py` gained `test_run_skips_connection_attempt_when_market_closed`
(asserts `SmartWebSocketV2` is never constructed while closed, and the longer poll interval is
used) and `test_run_resumes_connecting_once_market_reopens` (closed-then-open transition actually
reaches `connect()`); the pre-existing `test_run_waits_when_tokens_missing_then_connects_once_available`
now patches `check_market_hours` to force "open" so it stays deterministic regardless of when the
suite actually runs, rather than depending on real wall-clock time.

Full suite: 317 passed (was 315). `python -c "import app.main"` imports cleanly -- the two new
`app.signal_validation`/`app.time_utils` imports in `live_feed.py` don't introduce a circular
import or grow the live import graph in any way `tests/test_module_imports.py` flags.

**Not verified against the real feed** -- same standing constraint as the rest of this module (see
its own module docstring): no network path to Angel One from this sandbox. After deploying, the
check is: confirm no `[LIVEFEED] Connected` log lines appear outside 09:15-15:30 IST on a trading
day, and confirm exactly one `[LIVEFEED] Market closed (...)` line appears at close (not one every
10s) followed by silence until the next `[LIVEFEED] Market open again` the following trading
session.

### AI Origination gets two manual risk knobs: max stop-loss %, and a same-direction CONSECUTIVE-LOSS gate replacing the old entry-count gate (17 Aug 2026)

**Requested**: "I want set manual max stoploss and threshold value." Discussed before building --
scoped to AI Origination only (confirmed via AskUserQuestion), since rule-based strategies
already have admin-configurable `sl_percent`/`tp_percent` per strategy and are under their own
change freeze per this file's "shared-FIXED-branch hazard" section. Two follow-up rounds pinned
down what "max stoploss" and "threshold" actually meant:

- **Max stoploss**: "I can not risk 50% loss." AI Origination's proposed stop/target were only
  ever accepted if both fell inside a hardcoded 5-50% sanity band
  (`_MIN_SL_TARGET_PERCENT`/`_MAX_SL_TARGET_PERCENT`); outside it the trade fell back to generic
  trailing-stop instead of the AI's own numbers. 50% was a code constant, not a setting -- there
  was no way to tighten it without a deploy.
- **Threshold**: "if two consecutive same direction trades fails then only threshold should come
  in picture... 2 trades taken 1 won and second loss then threshold should not apply." This is
  NOT the same shape as the existing 11 Aug hard gate
  (`_MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK = 2`), which blocked a 3rd same-direction entry
  purely on entry COUNT, regardless of whether the first two won or lost -- so it could block a
  working thesis, not just a failing one. The 17 Aug backtest just above this entry found that
  count-gate's own threshold inconclusive on real data anyway. Confirmed via AskUserQuestion that
  this was the specific gate the user meant loosening, and that the intended replacement
  semantics are a CONSECUTIVE-LOSS streak, not a raw count.

**Implementation:**

- `AISettings` gains two new columns (`app/db_models.py`, additive `_ensure_columns()` migration
  in `app/database.py`): `ai_origination_max_sl_percent` (float, default 50.0) and
  `ai_origination_max_same_direction_losses` (int, default 2). Both default to the exact values
  the code previously hardcoded, so deploying this changes nothing until an admin actually edits
  Settings > AI's new "AI Origination Risk" section.
- **Stop/target sanity check split** (`app/ai/originator.py`'s `_open_trade`): `_is_sane` (one
  shared 5-50% band for both stop and target) is replaced by `_stop_is_sane` (uses the new
  admin-configurable `max_sl_percent`) and `_target_is_sane` (keeps the original hardcoded 50%
  ceiling, untouched). Tightening the stop max must not also cap the target -- a trade with a
  15% stop and a 48% target is still "sane" and uses the AI's own numbers even with the admin
  max stop set to 20%; only the stop side is now configurable, since that's specifically what
  was flagged as too permissive.
- **Same-direction gate replaced, not just re-thresholded.** New
  `_same_direction_consecutive_losses(db, index_symbol, action)` queries `strategy_trades`
  directly (CLOSED, `origin LIKE 'AI_ORIGIN_%'`, matching index+signal, across both providers --
  same cross-provider rationale as the existing `_same_direction_entries_today`) ordered
  newest-first, and counts a losing streak from the most recent trade backward, stopping at the
  first non-loss (WIN or BREAKEVEN) or at the start of today's history. A single win anywhere in
  the window resets it to 0 -- exactly the worked example in the request (1 win + 1 loss today
  -> streak is 0, not 2, so a 3rd entry is allowed). OPEN trades are excluded entirely from the
  walk (no resolved outcome yet, so they neither extend nor break the streak), matching how
  `StrategyStats.consecutive_losses` already treats streaks elsewhere in this app. Gated on
  `AISettings.ai_origination_max_same_direction_losses` via a new `_max_same_direction_losses()`
  helper.
- **The old count-based `_same_direction_entries_today` field is deliberately left completely
  untouched** -- it still feeds the system prompt's soft trend-age caution text, the CSV export's
  "Same-Dir Entries Today" column, `market_context_json`, and (importantly) the
  `same_direction_entries_backtest.py` script built and run for real just above this entry. That
  script analyzes entry COUNT vs. outcome and remains fully valid and unaffected -- it answers a
  different, still-useful question ("does the raw count of repeats predict outcome") from the new
  gate ("did the repeats specifically fail"). Because the new gate no longer reads
  `market_context.same_direction_entries_today` at all, it also now runs correctly even when
  `market_context` is `None` -- a behavior improvement over the old gate, which silently skipped
  entirely in that case.
- `tests/test_trend_age_gate.py` (tested the old count-gate's exact mechanics via a
  `market_context` dict) deleted and replaced with `tests/test_same_direction_loss_gate.py` (23
  tests) covering: the consecutive-loss streak walk (empty day, two losses, a win resetting the
  streak, win-then-loss only counting the trailing loss, breakeven also resetting, cross-provider
  counting, per-direction scoping, open trades excluded, previous-day trades excluded, non-AI-
  Origination origins excluded), the two settings-fallback helpers, and `_open_trade` integration
  (blocks at 2 consecutive losses, allows the exact 1-win-1-loss worked example, allows the first
  entry of the day, per-direction, an admin-configured threshold of 1, correct behavior with
  `market_context=None`, and the stop/target sanity split in both directions).
- Settings > AI gets a new "AI Origination Risk" section (`settings.html`) with the two fields
  and explanatory tooltips; `/ai-settings` POST validates `5.0 <
  ai_origination_max_sl_percent <= 100` (the floor mirrors `_MIN_SL_TARGET_PERCENT`, duplicated
  rather than imported to avoid a `dashboard_routes` -> `originator` coupling for one constant)
  and `ai_origination_max_same_direction_losses >= 1`.
- **Noticed in passing, flagged to the user, not touched**: the "Confidence Threshold" field
  already on that same Settings > AI page doesn't affect AI Origination at all --
  `originator.py` uses its own separate hardcoded `_MIN_CONFIDENCE_TO_ACT = 0.60` constant
  instead of reading `AISettings.confidence_threshold`. Out of scope for this task; worth fixing
  or removing later so the field isn't misleading.

Full suite: 315 passed (was 298; -6 for the deleted old gate test file, +23 for the new one).
`python -c "import app.main"` imports cleanly.

**Verified live**: started the app against a scratch SQLite DB, logged in, confirmed
`/settings?tab=ai` renders the new section with the correct defaults (50.0, 2), saved new values
(15, 1) and confirmed they persisted and re-rendered correctly, and confirmed both validation
rejections fire (max stop below the 5% floor, max losses below 1) with a 400.

### same_direction_entries_today outcome backtest -- run for real, INCONCLUSIVE (17 Aug 2026)

**Trigger**: a proposal to add a live-ADX-based early-exit trigger for running trades
("if the trend weakens mid-trade, exit regardless of trailing state"). Discussed before
building anything -- the 6 Aug STALL_EXIT backtest already tested closely-related ground
from the opposite direction ("skip STALL_EXIT when ADX >= 25", i.e. hold instead of exit)
and found holding was worse in 8 of 8 applicable trades, the documented conclusion being
"index continuation is not premium continuation." ADX describes the index, not the
premium actually held, so a live ADX-weakening trigger risks the same failure mode in
reverse. ADX is also not actually live at trade-monitor granularity -- it's computed once
per index per 5-minute AI Origination cycle, not on the 30s monitor tick, so wiring it
into every open trade's exit check would mean either accepting 5-min-stale reads or
adding fresh computation to the 30s loop, which risks reopening the SmartAPI rate-limit
contention this project has fought more than once. No gate was proposed or built from
that conversation.

Follow-up request, a different and more tractable question: does
`same_direction_entries_today` (the count already computed at entry time and stored per
trade in `market_context_json`) predict outcome, bucketed 0/1/2/3+? This revisits the 11
Aug hard gate (`_MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK = 2` in `app/ai/originator.py`)
with the thing `trend_age_gate_backtest.py` couldn't use at the time -- real AI
Origination history. That script validated a *proxy* reconstructed from the 2-year
index-level candle archive, since no real per-trade history existed yet when the gate
shipped (shipped from a same-day anecdote -- 7 same-direction entries in one day -- not a
backtest, per the standing exception CLAUDE.md notes for evidence at that severity).

**Built**: `scripts/same_direction_entries_backtest.py`, same shape as
`confidence_sizing_backtest.py` (its closest analog -- bucketed outcome backtest over the
closed-AI-Origination-trades population, MFE/MAE from `strategy_trade_ticks` rather than
the `highest_price`/`lowest_price` columns per that script's already-documented bug,
bootstrap 90% CI at `MIN_BUCKET_LIVE=20`). Reads each trade's own
`same_direction_entries_today[trade.signal]` from its stored `market_context_json` --
trades predating the field (key absent) are excluded outright rather than defaulted to 0,
since "unknown" and "zero prior entries today" are different facts. Two comparisons,
both policy-relevant rather than just descriptive: bucket 0 vs bucket 1 (would tightening
the gate to >=1 have been justified?), and buckets <2 combined vs >=2 combined (does the
threshold actually shipped hold up?). Because the gate has blocked any new entry at
count>=2 since 11 Aug, buckets 2 and 3+ can only ever be populated by pre-gate history --
the script says this plainly rather than presenting a thin bucket as settled. 12 new tests
(`tests/test_same_direction_entries_backtest.py`) cover the own-signal-key extraction
(a BUY_PE trade must read its own count, not BUY_CE's from the same context), the
field-absent-vs-zero distinction, the tick-based MFE/MAE derivation, the population filter
(AI Origination only, closed only, context JSON required), and both bootstrap comparisons
detecting a real synthetic effect.

**Not run in this sandbox** -- same constraint as every other backtest script in this
project: `sqlite3.OperationalError: no such table: strategy_trades` against this
environment's `data/trading.db`.

```bash
python -m scripts.same_direction_entries_backtest --db data/trading.db
```

**Run for real, same day, on production data.** Population was thin from the start: 48
of the 69 closed AI Origination trades were excluded outright for predating the
`same_direction_entries_today` field, leaving only 21 usable. Buckets: `0` n=10 (40.0%
win, +0.56% mean P&L), `1` n=7 (57.1% win, +0.31%), `2` n=3 (33.3% win, -6.00%), `3+` n=1
(0% win, -12.98%) -- every bucket individually below the trust minimum, exactly as
expected given the gate's own effect on the population.

- **Bucket 0 vs bucket 1**: bootstrap 90% CI `[-5.68, +7.02]`, crosses zero -- no
  reliable difference. No case for tightening the gate to `>=1`.
- **`<2` combined (n=17) vs `>=2` combined (n=4)**: bootstrap 90% CI `[+0.72, +15.54]`,
  excludes zero -- on its face "reliably better" below the gate. **Not trustworthy as
  stated**: the `>=2` side is 4 trades, and the entire `3+` bucket is a single `-12.98%`
  loss doing most of the work in that gap. This is the same single-outlier-as-pattern
  shape the break-confirmation backtest's n=1 bucket was explicitly not treated as
  evidence for, and the same standard applies here.

**Verdict: INCONCLUSIVE, not CONFIRMED.** The point estimates lean toward the shipped
`>=2` threshold being the right call (not toward loosening it), but n=4 -- dominated by
one trade -- does not clear this project's own bar for calling a threshold validated.
No code change indicated by this result. Worth noting structurally: because the gate has
blocked new entries at `>=2` since 11 Aug, this specific comparison's weaker side cannot
grow beyond pre-gate history -- unlike most "insufficient evidence, keep watching" calls
elsewhere in this file, more paper trading will not on its own fix this population's
size. If this needs a real answer, it would have to come from loosening or removing the
gate temporarily to accumulate fresh `>=2` observations, which is a real risk/reward
decision this note does not make on its own.

Read the two bootstrap comparisons before concluding anything; a bucket flagged below the
20-observation trust minimum is an expected outcome for 2/3+ given the gate's own effect
on the population, not a reason to force a verdict from it.

### AI Settings merged into Settings as a tab; Performance merged into Trade History; AI Alternatives filter removed; strategy sub-filter added (15 Aug 2026)

**Requested**: "Merge AI Settings into Setting as a new tab. Also merge performance tab into Trade
history tab. Remove AI Alternatives from trade history tab. When user select signals a new drop
down should appear which should have strategies."

**AI Settings → Settings tab**: `/ai-settings` (GET) now 307-redirects to `/settings?tab=ai` --
the same relocate-not-delete pattern `/strategies` already used for Settings > Strategies. The
AI form's own POST routes (`/ai-settings`, `/ai-settings/test`, `/ai-settings/test-secondary`)
keep their URLs (no reason to move them, they're form targets, not pages) but now redirect to /
render `settings.html` with `tab="ai"` instead of the standalone `ai_settings.html`, which is
deleted. A new `_settings_context()` helper in `dashboard_routes.py` gathers the full Settings
page context (general settings, strategies, indexes, AI settings, live-trading status) once,
shared by the plain GET and both connection-test POSTs -- previously each of the three routes
that render this page duplicated its own smaller slice of context. The template's AI pane uses
`ai_settings.*` for the `AISettings` row and `ai_test_result` for the connection-test banner,
kept distinct from the tab's other panes' `settings.*` (the unrelated `PlatformSettings` row)
now that both live in one template. Settings > Instruments' existing AI Origination Live blurb,
which linked to the now-deleted AI Origination page, updated to point at the AI tab instead.

**Performance → Trade History**: `/performance` (GET) now 307-redirects to `/history`. Its
KPI cards (net return, win rate, max drawdown) and three Chart.js panels (equity curve, daily
P&L, win/loss donut) are folded into `history.html`, below the existing filter form and above
the existing trades table -- no second "recent trades" list, since History's own full table
(richer columns, no 20-row cap) already covers what Performance's own recent-trades section was
for. `get_performance_summary()` (`app/platform.py`) -- previously hardcoded to `origin ==
SIGNAL` unconditionally, regardless of what the caller asked for -- is replaced by
`compute_performance_kpis(closed_trades)`, a pure function over an already-filtered trade list
rather than a second query: the route fetches trades once for the table, and the same closed
subset now feeds both the table and the KPI/chart numbers, instead of two separate DB round
trips computing overlapping populations. This also means the KPI/chart numbers now honor
whatever origin/strategy filter is currently selected on the page (previously they were silently
SIGNAL-only even when Performance's own strategy dropdown was left on "All") -- pick "AI
Origination Only" and the charts describe AI Origination's own equity curve, not signal trades.

**AI Alternatives removed from Trade History**: the `<option value="ai_alt">AI Alternatives
Only</option>` dropdown entry is gone, and `strategy_trades_query_for_filter()`
(`app/platform.py`) had its `elif origin == "ai_alt":` branch deleted outright (unreachable dead
code once the only two callers -- `history()` and `history_export()` -- stopped accepting
`"ai_alt"` into their allowed-origin set). This reverses the earlier 15 Aug decision ("Trade
History's pre-existing `ai_alt` filter value... historical `AI_ALT_*` rows already in the DB are
still visible there, by design") per this task's explicit instruction -- `AI_ALT_*` rows are
still visible under "All Trades" (no origin filter applied), just no longer separately
selectable. Historical `AI_ALT_*` data in the DB is untouched either way.

**Strategy sub-filter, shown only for Signal Only**: a new "Strategy" dropdown
(`signal_strategy_names()` in `app/platform.py`, distinct `StrategyTrade.strategy_name` values
among `origin == SIGNAL` rows only -- AI trades don't have a real `StrategyConfig`-backed
strategy name) appears next to the Origin dropdown. It's populated on every page load regardless
of the current origin selection, but only visually shown when Origin = Signal Only -- a small
inline `onchange` handler toggles a wrapper div's `display` with no page reload, and the initial
`display` state is server-rendered from the current `origin` value so a page that already has
Signal Only selected (e.g. after submitting the form) shows the strategy dropdown immediately,
not just after a second interaction. Selecting a stray `?strategy=` on a non-signal origin is
ignored server-side (`strategy_filter = strategy if origin == "signal" and strategy else None`)
rather than silently filtering the table to nothing. `strategy_trades_query_for_filter()` gained
a `strategy_name` parameter (applied as a plain `WHERE strategy_name ==` at the query level, not
a Python-side filter) so both the HTML view and the CSV export honor it identically -- CSV export
gained the matching `strategy` query param.

Verified live in this sandbox (no browser, but a real running instance): started the app against
a scratch SQLite DB, logged in, and curled every affected route. Confirmed: `/settings?tab=ai`
renders the AI pane with no Jinja errors; saving AI settings and hitting both "Test Connection"
buttons redirect/render correctly with `tab=ai` active; `/performance` and `/ai-settings` both
307-redirect as expected; `/history` renders the KPI grid and (once real closed trades exist)
the three charts; a strategy dropdown populated with a real strategy name appears only when
Origin = Signal Only and correctly excludes an AI Origination trade from both the table and a
signal+strategy-filtered CSV export; the old `/ops` and `/active-trade-page` (removed in the
prior task) still correctly 404. Full suite: 286 passed (was 280; 6 new tests in
`tests/test_history_settings_merge.py` covering the `ai_alt` origin value no longer filtering,
the strategy filter applying at the query level alone and combined with origin, `signal_
strategy_names()` excluding AI trades, and `compute_performance_kpis()` on an empty list and a
real win/loss pair).

### Active Trade tab removed; Ops Summary renamed to SmartAPI Health and stripped to just health data (15 Aug 2026)

**Requested**: "Remove Active Trade tab completely and rename ops summary to something related to
smartapi health. Remove everything from ops summary except pre market health and broker health."

**Active Trade removal**: `/active-trade-page` (`active_trade_page()` in `app/dashboard_routes.py`)
and its template `active_trade.html` deleted outright, along with the nav link in `base.html`. Its
sole supporting mechanism -- `_cached_ltp()`/`_trade_ltp_cache`/`_TRADE_LTP_CACHE_TTL_SECONDS`, a
5s per-contract SmartAPI LTP cache added 7 Aug specifically for this page's per-open-trade premium
fetch (see "Dashboard-driven SmartAPI rate exhaustion" below) -- had no other caller, so it was
deleted with it rather than left unwired: unlike the AI subsystem modules removed in the entry
below, this was a small helper with no independent documented history, entirely in service of one
page that no longer exists. Its test file (`tests/test_trade_ltp_cache.py`) removed with it. The
now-unused `import time as time_module` (only consumer was `_cached_ltp`) was cleaned up too;
`datetime.time` (imported under the plain `time` name in the same file) is still used elsewhere and
was left alone.

**Ops Summary → SmartAPI Health**: route moved from `/ops` to `/smartapi-health`
(`smartapi_health_page()`, replacing the old `dashboard()` function name too, since it no longer
renders the general-purpose dashboard). Grepped first for anything else depending on the `/ops`
path -- none found in `app/`, `tests/`, or `docs/` beyond the nav link and the `/health-check`
POST route's own redirect target, both updated. Template renamed `dashboard.html` →
`smartapi_health.html`, page title in `page_header()` now shows `health.overall_status` as its
badge (READY/WARNING/DOWN-colored) instead of the old `summary.bot_status` -- the page is now
about SmartAPI health specifically, so its own header status is the more relevant one.

**Removed from the page** (per "remove everything except pre market health and broker health"):
the top bot-status metric grid (Bot Status, Open Trades, today's P&L, consecutive losses, trading
allowed, risk status, current state) and its risk-lock alert banner, the Recent Logs section, and
the Strategy Metrics table. `get_dashboard_summary()`, `strategy_metrics()`, and `latest_logs()` are
all still real functions used elsewhere (`/control`, `/strategies` filters, `/logs`) -- only their
use from this one route was dropped, nothing in `app/platform.py` was touched.

**Kept, unchanged**: the Pre-Market Health section (overall status/health score/last-checked/
recovery-count metrics plus the "Run Health Check" button) and the Broker Health section (broker
metric grid, the broker/authentication/ltp/database/webhook/trading/ai/server components table,
and Last Error) -- byte-for-byte the same markup as before, just lifted into the new template with
nothing else around them.

Full suite: 280 passed (was 283; -3 for the deleted LTP-cache test file, no new tests needed --
this change removes code paths rather than adding conditional logic worth covering).

**Not verified live** -- this sandbox has no deployed server. After deploying: confirm `/ops` and
`/active-trade-page` both stop routing (404, or a login redirect if the session already expired),
confirm the nav shows "SmartAPI Health" linking to `/smartapi-health` with no separate "Active
Trade" entry, and confirm "Run Health Check" on the new page still redirects back to itself
correctly.

### AI Reviews, AI Alternatives, AI Exit Calls, AI Context Inspector removed; AI Origination summary folded into Reports (15 Aug 2026)

**Requested**: "Remove AI Alternatives, AI Exit calls, AI Reviews and AI Context Inspector as we
dont need it now. Also include ai origination trade summary in Reports tab, do not make it
separate." Investigated before building: all four pages are read-only UI layers, but two
independent backend pipelines feed them and both cost real LLM API money on every run
regardless of whether the page exists to view the result --

- `app/ai/shadow.py`'s `run_shadow_review()` -- queued as a `BackgroundTasks` job from every
  accepted BUY webhook (`queue_shadow_review()` in `app/main.py`) -- calls the signal
  validator/reviewer (feeds **AI Reviews** + **AI Context Inspector**) and, on REJECT, may open
  a paper `AI_ALT_*` trade via `alternative_trader.py` (feeds **AI Alternatives**).
- `app/ai/exit_shadow.py`'s `run_exit_shadow_checks()` -- a separate scheduled job, every 3 min,
  re-reviewing every open `SIGNAL` trade (feeds **AI Exit Calls**). Purely observational -- per
  its own doc comment it can never close a trade -- but still a real LLM call every 3 minutes
  regardless of whether anyone is looking at the page.

So removing the pages alone would have deleted the UI while leaving both pipelines running
silently in the background, still spending money with no visible output anymore -- worse than
before, not better. Two scope questions went back to the user rather than guessing:

1. **Should removing AI Reviews/AI Alternatives/AI Context Inspector also stop the shared
   per-signal review call (and the paper `AI_ALT_*` trades it produces)?** → **Stop it entirely
   (recommended).**
2. **Should removing AI Exit Calls also stop the 3-minute exit-shadow job?** → **Stop it
   entirely (recommended).**

**Implementation:**

- `app/main.py`'s `webhook()` no longer calls `queue_shadow_review()` -- removed entirely, along
  with `_build_shadow_market_data()` (its only caller), the `BackgroundTasks` parameter, and the
  now-dead `payload_indicators`/`payload_trend`/`payload_strategy_filters`/`payload_trade_state`
  locals that existed only to feed it. This also removes 1-2 SmartAPI calls per accepted BUY
  webhook that `_build_shadow_market_data` made purely to enrich the review context.
- `app/scheduler.py` no longer registers the `ai-exit-shadow-check` job (was every 3 min) --
  scheduler now runs 8 jobs, not 9.
- Four routes, their route functions, and their four dedicated `AITradeReview`/`AIExitCall`-
  reading data functions in `app/platform.py` (`origin_comparison_metrics`,
  `get_exit_shadow_summary`, `ai_reviews_query_for_filter`) deleted, along with the
  `/api/ai-reviews/export` CSV route in `app/api_routes.py` and all four templates and their
  `base.html` nav links.
- **`app/ai/shadow.py`, `app/ai/exit_shadow.py`, `app/ai/alternative_trader.py`,
  `app/ai/validator.py`, `app/ai/review_repository.py`, `app/ai/context_repository.py` were
  deliberately left in the repo, unwired, rather than deleted.** They encode substantial
  documented history (the three-AI-subsystem design above, the Claude `max_tokens` truncation
  fix, the JSON-fence parsing gotcha) and the request said "we dont need it **now**", not "this
  is permanently wrong" -- same reversibility posture already established for
  `option_chain_collection_enabled` (defaults `False`, module kept intact) rather than deleting
  `app/option_chain.py`. Confirmed via grep that nothing still imports or calls into any of them
  after this change -- they are dead code, not orphaned-but-reachable code.
- Carefully preserved everything adjacent that looked related but wasn't: `_context_json`
  (shared with `/reports`), `AITradeReview` usage inside `reports.py`'s existing Pattern
  Discovery report (`_reviews_with_outcome_between`/`_ai_correlation_stats` -- unrelated to the
  deleted AI Reviews *page*), `strategy_trades_query_for_filter`'s pre-existing `ai_alt` filter
  value on Trade History (historical `AI_ALT_*` rows already in the DB are still visible there,
  by design -- only the dedicated comparison page is gone), `get_latest_context_log`/
  `/ai/latest-context` (a separate, UI-unreferenced API endpoint, out of scope), and
  `create_reviewer`/`SignalContextBuilder` (AI Settings' unrelated "Test Connection" buttons).
- **AI Origination trade summary added to `/reports`** as a fifth on-demand report type
  (`ReportType.ORIGINATION`), not a separate page/tab, matching the existing Daily/Weekly/
  Monthly/Pattern Discovery "Generate Now" button pattern exactly. `generate_origination_summary()`
  (`app/reports.py`) queries closed trades with `origin LIKE 'AI_ORIGIN_%'` (explicit `LIKE`, per
  the standing rule -- never `!= 'SIGNAL'`, which would also sweep in `AI_ALT_*`), over a
  selectable lookback (30/90/180 days/all-time, default 30 -- shorter than Pattern Discovery's
  default 90 since AI Origination itself is newer). Stats mirror `_trade_stats()`'s shape but
  break down `by_provider` (Claude vs OpenAI, parsed off the `origin` suffix) and `by_index`
  instead of `by_strategy` -- AI Origination trades don't have a `StrategyConfig` row to group
  by -- and add `by_mode` (paper vs live counts) and `avg_confidence`, both specific to this
  subsystem. Same `_generate_narrative`/`_save_report` machinery every other report type already
  uses, including the OpenAI-narrative-with-template-fallback path.
- **Noticed in passing, not acted on (out of scope for this task)**: AI Settings' "Mode" dropdown
  (DISABLED/SHADOW/ADVISORY/BLOCKING) never actually gated a real trade -- grep confirmed only
  the `== "DISABLED"` branch was wired into any code path; `queue_shadow_review()` (now removed
  entirely) only ever fired *after* `response.accepted` was already `True`, as a decoupled
  background task, so ADVISORY and BLOCKING behaved identically to SHADOW. Worth knowing if
  anyone reads that dropdown as a real safety control -- it wasn't one.

8 new tests (`tests/test_origination_report.py`) cover the provider-parsing helper, the
`origin LIKE`-filter population query (excludes `SIGNAL` and `AI_ALT_*`, excludes open trades),
the stats bucketing (provider/index/option-type/exit-reason/mode, best/worst provider,
consecutive-loss streak, empty-population defaults), and `generate_origination_summary()` itself
(saves an `ORIGINATION`-typed `AIReport`, template-fallback narrative when no AI provider is
configured, confirms `SIGNAL` trades never leak into the numbers). Full suite: 283 passed (was
275 before this change).

**Not verified live** -- this sandbox has no deployed server to click through. After deploying,
confirm on the real dashboard: the four removed nav links are gone and their URLs 404 (or
redirect to login if unauthenticated), no new `AITradeReview`/`AIExitCall` rows appear after an
accepted webhook or after 3 minutes elapse with an open `SIGNAL` trade, and the Reports page's
new "AI Origination Summary" button produces a card with real numbers once there is at least one
closed `AI_ORIGIN_*` trade in the live DB.

### AI Origination folded into the main dashboard; its own tab removed (15 Aug 2026)

**Requested**: show AI Origination's live trades on the main dashboard's Active Trades, and
remove the separate AI Origination tab entirely. Investigated before building (the tab was the
*only* place `AI_ORIGIN_*` trades were visible at all -- open positions with confidence/
reasoning, closed history, and KPIs), so three scope questions went back to the user before any
code changed:

1. **Which AI Origination trades show in Active Trades once the tab is gone?** → **All open
   AI Origination trades, paper and live alike** (not live-only) -- a mode badge distinguishes
   them, same as the tab used to.
2. **What happens to the closed-trade history and KPI strip (win rate, net P&L, total
   originated) that only the tab showed?** → **Dropped.** The dashboard is live-position-only
   now; there is no UI view of AI Origination's closed-trade track record anymore. (The data
   still exists in `strategy_trades` for anyone querying the DB directly --
   `scripts/confidence_sizing_backtest.py` and friends are unaffected.)
3. **The live-trading toggle the user also asked for in AI Settings turned out to already
   exist** -- a per-index "AI Origination Live" checkbox in Settings > Instruments
   (`IndexConfig.ai_origination_live_trade`), gated together with the server-side
   `SMARTAPI_LIVE_TRADING` env var exactly per CLAUDE.md's existing two-key pattern. → AI
   Settings gets a **read-only status panel** (which indices are live-enabled, whether the
   server flag is on, a link to Instruments), not a second writable control. Two controls
   writing the same flag was rejected specifically to avoid the two ever silently disagreeing
   on something that moves real money.

**Implementation:**

- `get_open_trades_with_ticks()` (`app/platform.py`) now matches
  `origin == 'SIGNAL' OR origin LIKE 'AI_ORIGIN_%'` -- explicit `LIKE`, never `!= 'SIGNAL'`, per
  the standing rule in "The `origin` field is the isolation mechanism" above. `AI_ALT_*` shadow/
  comparison trades stay excluded; they're not a position anyone is holding. Each row now also
  carries `origin`, `source_label` (e.g. "AI Origin · Claude"), and `mode` (PAPER/LIVE).
- `origin_label()` moved from `app/dashboard_routes.py` to `app/platform.py` (still registered as
  the same Jinja filter) so the trade-shaping function could use it without a
  platform→dashboard_routes circular import.
- `live_dashboard.html`'s trade cards show a source badge for any non-signal trade and a `LIVE`
  badge (red, real money) for any trade -- signal or AI-originated -- with `mode == LIVE`. `PAPER`
  gets no badge; it's the expected default.
- `/ai-origination` route, `get_origination_summary()`, and `ai_origination.html` deleted
  outright, along with the nav link in `base.html`. Nothing else in the repo referenced them
  (confirmed by grep) except `docs/ai-origination-roadmap.md` mentions elsewhere, which are about
  the unrelated background origination *job*, not this page.
- New `get_live_trading_status()` (`app/platform.py`) reads `IndexConfig.ai_origination_live_trade`
  per enabled index and `smartapi.settings.live_trading` (the server flag), returned read-only to
  `ai_settings.html`. Explicitly documented in its own docstring as not-a-control, so a future
  reader isn't tempted to wire a checkbox to it later without re-reading why that was rejected.

11 new tests across `tests/test_active_trades_ai_origination.py` (mixed SIGNAL/AI_ORIGIN_*
population, AI_ALT_* still excluded, closed AI Origination trades still excluded, `origin`/
`source_label`/`mode` fields correct for both trade types) and `tests/test_live_trading_status.py`
(server flag on/off, per-index status, disabled indices excluded, missing `smartapi.settings`
defaults safely to off). `tests/test_strike_display.py`'s origination-specific test was repointed
at `get_open_trades_with_ticks` instead of the deleted function.

**Not verified live** -- no browser access from this sandbox to confirm the dashboard renders
correctly with a real AI Origination trade open, or that the AI Settings status panel matches
what Settings > Instruments actually has checked.

### Dashboard kept "updating" on closed days -- a real, separate tick-write path the 14 Aug scheduler fix didn't cover (14/15 Aug 2026)

**Trigger**: reported live over a weekend -- the dashboard still looked active on days the
market never opened, and shouldn't need to.

**Real and separate from the scheduler fix directly above.** That fix gated
`run_origination_checks`'s and `MultiStrategyMonitor.tick`'s own SmartAPI-driven paths, but
`get_index_live_figures` (`app/platform.py`) is driven by *dashboard polling* --
`/api/live-dashboard`, fetched unconditionally every 10s by any open browser tab
(`live_dashboard.html`), with no market-hours awareness of its own. Confirmed by reading: every
poll called `record_index_tick_if_stale()`, which wrote a new `IndexPriceTick` row roughly every
`_INDEX_TICK_THROTTLE_SECONDS` (~25s) regardless of day or hour -- on a weekend/holiday this was
the *same frozen price*, re-recorded over and over, for as long as anyone had the tab open. Real
DB writes with zero new information, purely a side effect of viewing the page. Originator.py's
own call to the same function is unaffected by this entry (already upstream of the scheduler
gate above); this is `get_index_live_figures`'s independent call site.

**Fixed**: `get_index_live_figures` now checks `check_market_hours(utc_now())` once per call and
skips `record_index_tick_if_stale` when it's not a trading day/hour -- the figure itself (last
known price, `is_live` state, the existing per-index "stale" badge) is untouched, only the
redundant write stops. 3 new tests confirm a tick IS still written on an ordinary trading
moment, and is NOT written on a weekend, an NSE holiday, or a weekday evening.

**Also fixed the visible symptom this was reported from, not just the backend cause.** The
dashboard's top "Updated Xs ago" badge resets on every successful 10s fetch regardless of
whether anything in the payload changed, so it kept implying live activity even with the feed
frozen and no new ticks being written. `_live_dashboard_data` now returns a `market_open` field
(same `check_market_hours` check, no new SmartAPI call), and the badge shows "Market closed"
instead of a ticking counter when it's false -- while the existing per-index "stale" badge
(`renderIndices`) is untouched, so a genuine feed outage *during* real trading hours still
reads as "stale," not "market closed," which is a different, more urgent condition. 4 new tests
cover `market_open` on a trading weekday, a weekend, a holiday, and a weekday evening.

**Not verified live** -- this sandbox has no browser/production access to watch the dashboard
render. After deploying, the check is straightforward: open the dashboard on a closed day (or
just watch the top badge outside 09:15-15:30 on an ordinary weekday) and confirm it reads
"Market closed" rather than counting up, and separately confirm no new `IndexPriceTick` rows
accumulate for a closed period despite the tab staying open:

```sql
SELECT index_symbol, COUNT(*) FROM index_price_ticks
WHERE date(recorded_at) = '<a Saturday or holiday date>' GROUP BY index_symbol;
```

should come back empty (or with only rows predating this fix's deployment).

### SmartAPI calls stopped outside market hours -- root cause was scheduling order, not missing logic (14 Aug 2026)

**Confirmed**: 96 `[AI][ORIGIN]`/`SmartAPI` log lines between 16:00-18:00 IST on 13 Aug, hours
after the 15:15 square-off. Both scheduled jobs (`ai-origination-check` every 5 min,
`trade-monitor` every 30s) are on bare `IntervalTrigger`s with no day/time constraint --
`IntervalTrigger` doesn't support one -- so both fire 24/7 by construction.

**AI Origination was the real, confirmed cost.** `run_origination_checks` already has
`_still_observing`/`_past_trading_end` (09:45-15:15), but those only gate the entry *decision*.
`smartapi.get_index_spot(index)` -- a real SmartAPI call, once per enabled index -- fired
**before** either check, every single 5-min cycle, unconditionally. The existing market-hours
logic was there; it just wasn't applied early enough to stop the network call it was meant to
gate. No weekday/holiday check existed anywhere in this file either.

**`trade-monitor` (30s job) turned out to already be near-zero-cost outside hours, on inspection
-- not by design, incidentally.** `MultiStrategyTradeManager.monitor_open_trades` and
`V7Manager.monitor_open_trades` both query for open trades first and return immediately if
there are none, before touching SmartAPI at all. As long as every trade actually closes at the
15:15 square-off, there is nothing left to iterate after hours and the job is already free. This
is a *consequence* of that code's structure, not a market-hours gate -- confirmed by reading, not
by log volume, since the 96-line count doesn't distinguish real SmartAPI calls from apscheduler's
own "Running job"/"executed successfully" announcement lines, which also match the grep pattern
in the trigger's own diagnostic command and fire unconditionally every 30s regardless of whether
any real work happened.

**Fixed, with two deliberately different gates, not one applied uniformly to both jobs**:

- `app/signal_validation.py` already had a real 2026 `NSE_HOLIDAYS` calendar and
  `check_market_hours()` (weekday + holiday + 09:15-15:30 window), used for flagging incoming
  TradingView signals -- `option_chain.py`'s collector already reuses it as a scheduling gate the
  same way. Refactored out `trading_day_reason()` (weekday + holiday only, no hour component) so
  both existing and new callers share one calendar rather than each re-deriving it -- exactly the
  kind of duplication CLAUDE.md's own DTE-bucket entry warns can silently drift apart. Verified
  `check_market_hours`'s exact original wording is unchanged (`tests/test_signal_validation.py`).
- **AI Origination**: `check_market_hours(utc_now())` checked once, at the very top of
  `run_origination_checks`, before `get_index_spot` or any other work. Deliberately the *wider*
  09:15-15:30 window, not the narrower 09:45-15:15 entry window `_still_observing`/
  `_past_trading_end` already own further down -- this gate only needs to rule out evenings,
  nights, weekends and holidays; the pre-open tick-recording behaviour between 09:15-09:45 must
  keep working exactly as before, and does.
- **`trade-monitor`**: `trading_day_reason()` only (no hour-of-day component) at the top of
  `MultiStrategyMonitor.tick()`. Deliberately *not* also gated by time-of-day the way AI
  Origination is -- this job carries ongoing exit-safety responsibility for real open positions
  and must keep running through every hour of an actual trading day, including right up to and
  past 15:15, so it can still catch a trade that the square-off missed for some reason rather
  than going silent on it. The existing empty-open-trades early return already makes the
  intraday-hours cost zero in the normal case; this is a second, independent line of defence for
  the abnormal one, not a replacement for it. `square_off()` itself is untouched, per scope.

16 new tests (`tests/test_signal_validation.py`, `tests/test_market_hours_gate.py`): the
weekday/holiday helper's boundaries (an unknown year skips the holiday check rather than
guessing, matching the module's own stated failure-mode preference), both scheduler gates
skipping entirely on a weekend/holiday with SmartAPI/manager stand-ins that raise if touched at
all, AI Origination's gate correctly firing on an ordinary weekday evening (reproducing the 13
Aug incident), and `trade-monitor`'s gate deliberately *not* firing at the same evening hour on
an ordinary weekday (confirming the two jobs' gates are intentionally asymmetric, not a
copy-paste of the same check).

**Also noticed, not touched (out of scope)**: `daily-square-off`'s own `CronTrigger` in
`app/scheduler.py` has no `day_of_week="mon-fri"` restriction, unlike every other cron job in
that file -- it technically fires at 15:15 on Saturday/Sunday too. Almost certainly harmless
(`square_off_all` has the same empty-open-trades early return), and the task explicitly scoped
this fix to not touch square-off logic. Worth a one-line fix if anyone's looking at that file
again, not urgent enough to justify touching it here.

**Not verified live** -- this sandbox has no journalctl/production access. After deploying, the
task's own verification commands are the right ones to run:

```bash
sudo journalctl -u tradingview-bot --since "<today> 15:30:00" --until "<tomorrow> 09:15:00" | grep -c "SmartAPI"
```

should drop to ~zero (a brief tail right at the 15:30 boundary is expected and fine). Confirm the
jobs still fire and make real calls normally the next trading day, and check whether the daily
rate-limit hit count moved at all now that the AI Origination job isn't adding after-hours
call volume on top of the collision issue already being investigated separately.

### AI Origination confidence floor raised 0.55 -> 0.60, backtested (14 Aug 2026)

Implements the decision from the confidence-sizing backtest below. Three metrics (win rate,
mean P&L, mean MAE) all independently agreed the `<0.60` bucket (n=28) is reliably worse than
every bucket at 0.60+, which is itself roughly flat -- a step function, not a gradient. A
scaled-by-confidence position size was therefore rejected in favor of a hard floor: there is no
evidence more confidence above 0.60 deserves more size, since P&L does not improve further in
that range (0.60-0.75 is in fact the single best-performing bucket, largest sample too).

**Implemented as a threshold change, not a new gate.** `_MIN_CONFIDENCE_TO_ACT` already existed
in `app/ai/originator.py` -- a confidence floor checked in `run_origination_checks` before
`_open_trade` is even called, one layer above the trend-age/`same_direction_entries_today` gate
that lives inside `_open_trade` itself. Adding a second, separate 0.60 check inside `_open_trade`
(as the task spec's suggested location implied, apparently unaware this constant already existed)
would have been redundant with -- and strictly shadowed by -- the existing 0.55 gate one layer up,
since anything below 0.60 already fails to reach `_open_trade` at 0.55. Raised the existing
constant instead: smaller diff, no duplicate/overlapping threshold for a future reader to puzzle
over. All 185 trades in the backtest population already had confidence >= 0.55 by construction
(the pre-existing floor), consistent with this being the correct single point of control.

**Also added**: an explicit skip log at the confidence-check site
(`[AI][ORIGIN] {symbol}: Skipped: ai_confidence={value} below floor {floor}`), matching the
pattern DTE-floor and `same_direction_entries_today` skips already use -- previously this
condition fell through silently with no INFO-level line of its own. The confidence check was
also pulled into a small `_clears_confidence_floor()` helper (mirrors `_open_trade`'s existing
`_is_sane()` pattern) purely so it's unit-testable without needing `run_origination_checks`'s
full AI-client/DB/market-context machinery -- `_open_trade`'s own gates already have this kind
of isolated test coverage (`tests/test_trend_age_gate.py`), this one didn't.

**Auditability confirmed, not just assumed**: `record_decision()` is called unconditionally
after this check regardless of which branch fires (same as every other decision outcome), so a
skipped BUY_CE/BUY_PE with its real confidence value is queryable in `ai_origination_logs` via
`decision IN ('BUY_CE','BUY_PE') AND trade_id IS NULL AND confidence < 0.60` -- distinguishable
from a genuine model-chosen NONE, which records `decision='NONE'` instead.

7 new tests (`tests/test_confidence_floor.py`) cover the exact boundary (0.59 blocked, 0.60
allowed), missing confidence treated as failing the floor, and the two trigger trades' own
confidence values (0.55, 0.55) landing on the correct side. Scope respected: only
`app/ai/originator.py` touched, no changes to exit logic, stop/target construction, the
trailing mechanism, or the model prompt -- confirmed via `git diff --stat`.

**Not verified live** -- this sandbox cannot run the origination cycle end-to-end (no SmartAPI
credentials, no real AI provider calls). After deploying, per the task's own verification steps:
spot-check the first live session for any `ai_confidence < 0.60` row in `ai_origination_logs`
and confirm it shows `trade_id IS NULL` rather than an opened trade; over the following one to
two weeks, re-run `scripts/confidence_sizing_backtest.py` against fresh post-deployment data and
confirm the `<0.60` bucket has stopped accumulating new closed trades (since none should be
opening there anymore).

### AI confidence / hedging-language sizing backtest -- tooling built, NOT run (14 Aug 2026)

**Trigger, a repeat pattern across three trades this cycle, not a single anecdote:**

- 12 Aug, Bank Nifty CE (confidence 0.66, "cautious... rather than a strong breakout") --
  trend already ran full session, still inside opening range. Lost.
- 14 Aug, Nifty CE (confidence 0.55) -- 5-min breakout but 15-min Supertrend still down,
  extended from EMA21. Lost, `STALL_EXIT` at -0.42% after only 3.84% MFE.
- 14 Aug, Bank Nifty PE (confidence 0.77) -- "the move is already extended and the trend
  is mature," `trend_duration_pct_of_session=100.0`. Lost.

Contrast with 14 Aug's one clean winner (Bank Nifty PE, confidence 0.71, developing ADX, no
self-flagged conflict in the reasoning). All three losses had the model naming a real
conflict in its own reasoning and trading at full size anyway -- confidence and hedging
language both look like they're carrying real signal that currently has zero effect on
position size.

**Not shipped without a backtest first**, same discipline as every other change this cycle --
three trades is a repeat pattern (stronger than the single-trade break-confirmation case tested
11 days ago, which came back NOT SUPPORTED from similarly compelling anecdotal grounds) but
still not the ~2 months of history that would let this be tested properly.

**Built**: `scripts/confidence_sizing_backtest.py`, two checks against the same population
(every closed AI Origination trade with a recorded `ai_confidence`):

1. Confidence-bucketed (`<0.6`, `0.6-0.75`, `0.75-0.85`, `>0.85`): win rate, mean P&L, mean
   MFE, mean MAE per bucket (MFE/MAE derived from `highest_price`/`lowest_price` vs
   `entry_price`, no new columns), plus a bootstrap 90% CI on the Pearson correlation between
   the raw confidence score and `pnl_percent` across all trades -- the direct test of "does
   confidence predict outcome."
2. Reasoning-text hedging-language check, meant to be read alongside part 1, not only as a
   fallback: does `ai_reasoning` containing any of the roadmap's own five keywords
   ("cautious," "moderate," "extended," "already run," "mature") correlate with worse
   outcomes independent of the confidence number, plus a cross-tab reporting how often the
   score and the text actually disagree (the 14 Aug Bank Nifty PE loss -- confidence 0.77,
   hedged reasoning -- is exactly such a case). Same bootstrap-CI/`MIN_BUCKET_LIVE=20`
   pattern as `break_confirmation_backtest.py`, duplicated per this project's per-script
   convention rather than shared.

14 new tests (`tests/test_confidence_sizing_backtest.py`) cover the population filter
(AI Origination only, closed only, confidence required), MFE/MAE derivation, each of the
five hedge keywords matching case-insensitively, the Pearson helper's edge cases (zero
variance, too few points), and that both bootstrap helpers detect a real synthetic
effect when one is deliberately constructed.

**Not run** -- `data/trading.db` in this sandbox is a 0-byte file with no schema at all
(confirmed: `sqlite3.OperationalError: no such table: strategy_trades`, the same failure
every other backtest script in this project already hits here). No sizing mechanism (section
2 of the roadmap: scale-to-confidence vs. a hedged-reasoning floor) has been implemented --
that decision explicitly depends on which of the two checks above turns out more predictive,
which cannot be answered without real data. Run on the machine with real history:

```bash
python -m scripts.confidence_sizing_backtest --db data/trading.db
```

Read part 1's bucket sizes first. If every bucket clears `MIN_BUCKET_LIVE=20`, the confidence
bucketing and correlation CI are trustworthy on their own. If not (likely, given the same
~2-month-history constraint every other live-history check in this project has hit), read
part 2's hedging check as the primary signal -- it pools the whole population into two
buckets instead of four, so it reaches the trust minimum sooner. Per the roadmap's own
deliverable 3: if neither check clears its bar, "insufficient evidence, keep watching" is the
correct thing to report, not a reason to ship a sizing curve fit to three trades.

**Run for real, same day -- found and fixed a real bug in the script before trusting the
numbers.** First run against production `data/trading.db` (185 closed AI Origination trades
with a recorded confidence) reported `mean_mae=+0.00%` in literally every bucket of both PART 1
and PART 2 -- not close to zero, exactly zero, which is the signature of a bug rather than a
coincidence of real trading outcomes. Cause: the script read MAE from
`StrategyTrade.lowest_price`, which `dashboard_routes.py` already documents (and works around,
in its own CSV export) as only maintained on the side `monitor_open_trades` needs for the
trailing-stop engine -- for a long trade (every AI Origination trade is `BUY_CE`/`BUY_PE`)
`lowest_price` stays pinned at its entry-time seed value forever, so a lowest_price-derived MAE
is deterministically 0.00% for every trade, never a real adverse excursion. `highest_price` (and
therefore MFE) was unaffected -- it's the side that *is* maintained for a long trade. Fixed by
reading real extremes from `strategy_trade_ticks` (30s premium samples) instead, mirroring
`dashboard_routes.py`'s own established `_excursion` helper rather than inventing a second way
to compute this. 2 new tests confirm ticks are used and that a misleadingly-seeded
`highest_price`/`lowest_price` pair is ignored even when populated.

The bug did not affect win rate, mean P&L, or the confidence/pnl correlation -- none of those
read `lowest_price` -- so the real headline numbers from the same run stand:

- **No smooth scaling relationship.** `Pearson r(confidence, pnl_percent) = +0.072`, bootstrap
  90% CI `[-0.046, +0.187]` -- crosses zero, not reliable. A continuous confidence-to-size curve
  (roadmap option 2a as a linear/tiered scale) is NOT supported by this correlation.
- **But a real, sample-adequate floor effect.** The `<0.60` bucket (n=28, clears
  `MIN_BUCKET_LIVE=20`) stood apart on point estimates: 25.0% win rate, -5.35% mean P&L, versus
  roughly breakeven (-0.70% to +0.25%) for every bucket at 0.60 and above. The script had no way
  to say whether that gap was reliable rather than eyeballed -- **fixed in the same pass**, added
  a bootstrap 90% CI comparing `<0.60` against everything else (mirrors PART 2's existing
  hedged-vs-not comparison). 2 new tests cover a detected floor effect and a null case.
- **Hedging language: directional but not reliable.** Hedged mean P&L -2.08% (n=77) vs not-hedged
  -0.09% (n=108) -- the point estimate matches the trigger's intuition, but the bootstrap 90% CI
  on the difference is `[-4.68, +0.68]`, crossing zero.
- **The score and the text really do disagree, often.** 26 of 185 trades (14%) had high
  confidence (>=0.75) *with* hedged reasoning -- the 14 Aug Bank Nifty PE loss (confidence 0.77,
  "already extended... mature") is exactly this shape, not a one-off. 20 more had low confidence
  (<0.60) with no hedging language. ~25% of the population has the two signals disagreeing, so
  neither can stand in for the other.

**Verdict: a hard confidence floor around 0.60 has real, sample-adequate support (roadmap option
2a's floor variant specifically, not a scaling curve). A hedged-reasoning gate (option 2b) does
NOT clear its bar yet** -- directionally consistent with the trigger, but the CI is not reliable
at n=77/108. Not shipped this pass: the floor threshold itself (exactly 0.60, vs. a value chosen
with more margin) and its behavior (skip entirely vs. downsize) are a real design decision the
task deliberately leaves to the user rather than picking unilaterally from one bucket boundary
that happened to be pre-specified in the script. Worth its own dated follow-up once decided.

```bash
python -m scripts.confidence_sizing_backtest --db data/trading.db
```

**MAE fix verified live, and it adds a second line of evidence for the same floor.** Re-run
after deploying the fix: `mean_mae` is now real and negative everywhere (was `+0.00%`
everywhere before), and it scales with confidence the same direction as win rate and P&L --
`<0.60` averages **-8.57%** adverse excursion versus -4.67% to -5.49% for every bucket at 0.60+.
Low-confidence AI Origination trades don't just lose more often, they draw down close to twice
as deep before failing. Every other number (win rates, P&L, the floor bootstrap CI, the
hedging check, the cross-tab) reproduced byte-identical to the pre-fix run, as expected since
none of it read the buggy column.

### 13 Aug retry-bypass fix was real but not the cause of the actual production rejections (14 Aug 2026)

The 13 Aug fix (below) shipped and was live all of 14 Aug. Verified via the new `[THROTTLE]`
log line that the throttle itself is working correctly -- `gap=0.04x` entries are the expected
shape (a call arriving right behind another gets logged, then sleeps to the 1.3s minimum before
dispatching), not evidence of a bug. But the rejection count was unchanged: **39, statistically the
same as the original 38/day baseline**, not better. The 13 Aug fix, though a real and correct fix
for the bug it targeted, was fixing a bug that turned out not to be causing the counted rejections.

**Found by reading the actual log context around a rejection, not by more static analysis.**
`sudo journalctl ... | grep -B5 "Access denied because of exceeding access rate"` showed the
failure is logged from `app/ai/originator.py`'s `_load_market_context`, not from anywhere inside
`smartapi_client.py`'s throttle/retry code -- and it fires on essentially every 5-minute
origination cycle, immediately (~1.3s, correctly throttled) after a preceding `get_ltp` call, not
intermittently. Traced into the installed `SmartApi` SDK
(`.venv/lib/python3.11/site-packages/SmartApi/smartConnect.py:229-234`): Angel's rate-limit
rejection for `getCandleData` does not always come back as the well-formed
`{"status": false, "message": "..."}` dict `_rate_limited()` checks -- it can come back as a
**raw non-JSON text body** (`b'Access denied because of exceeding access rate'`). The SDK's own
`_request()` then raises `DataException("Couldn't parse the JSON response received from the
server: {content}")` instead of returning anything `_rate_limited()`/`_token_expired()` can
inspect. That exception sailed straight past `_call_with_reauth`'s unguarded
`response = func(*args, **kwargs)` -- never reaching `_retry_rate_limited` at all -- through
`get_candles()` (no try/except there either), and was only ever caught four frames later by
`_load_market_context`'s own broad `except Exception`, which logs and silently falls back to
stale stored history. This is exactly why `"Retrying rate-limited request"` never appeared in
production logs despite dozens of daily rejections: `_retry_rate_limited` was never invoked for
this failure shape, so neither the 12 Aug margin widening nor the 13 Aug retry-bypass fix could
possibly have mattered -- both are real fixes to a code path this specific failure never reaches.

**Fixed**: `_call_with_reauth`'s initial dispatch and `_retry_rate_limited`'s own retry dispatches
are now wrapped in `try/except`, checking the raised exception's text for the same
`"access rate"`/`"rate limit"` substrings `_rate_limited()` already checks in the dict case (new
`_is_rate_limit_error_text` static method). A match routes into the exact same retry path a
dict-shaped rejection already used; anything else (a real parse failure, a genuine outage)
re-raises unchanged rather than being misrouted. This is deliberately narrow -- it does not touch
the post-reauth (token-expiry) retry branch, which is only reachable when the initial response
already parsed as a dict, so it was never part of this failure mode. 8 new tests in
`tests/test_smartapi_throttle.py` cover: the text-matcher against the exact production exception
string and against an unrelated parse failure (must not match), that `_call_with_reauth` routes a
raised rate-limit exception into retry and recovers, that a genuinely unrelated exception still
propagates through both `_call_with_reauth` and `_retry_rate_limited` unchanged, and that a retry
attempt hitting the same raised-exception shape is treated as "still limited" rather than
aborting the loop.

**Not verified live** -- same sandbox constraint as every round of this investigation. After
deploying, the meaningful signals are: `"Retrying rate-limited request"` should finally start
appearing in the logs (proof this code path is now actually being exercised), followed by
`"Rate limit recovered after N attempt(s)"` on most of them; the daily rejection-adjacent count to
now watch is `grep -c "candle refresh failed"` in `[AI][ORIGIN]` logs, which should drop sharply
if most of these now succeed on retry within a few seconds instead of failing outright every
cycle. If `"Retrying rate-limited request"` still doesn't appear, or the candle-refresh-failed
count doesn't drop, that means this exception-shaped rejection still isn't the whole story and
there's a third, still-undiscovered path -- worth re-running the same `grep -B5` context check
before guessing again.

```bash
sudo journalctl -u tradingview-bot --since today | grep -c "Retrying rate-limited request"
sudo journalctl -u tradingview-bot --since today | grep -c "candle refresh failed"
```

### Real root cause of the throttle rejections found: retries bypassed the gate entirely (13 Aug 2026)

The 12 Aug margin widening (1.05s → 1.3s, below) did not fix production. The next full session
came back at **55 rejections — worse than the original 38/day baseline**, despite confirming the
fix was correctly deployed and live the whole time. Multi-process deployment, `get_candles`
skipping the throttle, timestamp resets, lock races, and a sibling app (`~/tradingbot`, since
stopped and disabled — unrelated, no `SMARTAPI_API_KEY` of its own and didn't run that day) were
each investigated and ruled out in turn. None explained calls landing under 1.3s apart when the
throttle, read statically, should have prevented that unconditionally.

**Found by reading `_call_with_reauth`/`_retry_rate_limited`, not by the diagnostic logging this
entry was originally going to be about.** Angel One's rate-limit rejection is not itself an
exception — it's a normal-looking response dict `_call_with_reauth` detects and hands to
`_retry_rate_limited`, which retries with exponential backoff (0.5/1/2/4/8/15s). Every one of
those retries is a **real HTTP dispatch to the same rate-limited endpoint**, fired straight after
its backoff sleep with no relation at all to `_throttle_quote_call`'s shared lock/timestamp —
it neither waited on that gate nor updated it. So a retry from one rate-limited call could land
inside another thread's legitimately-throttled window at any moment: that other thread computes
its wait from `_last_quote_call_monotonic`, a timestamp the retry never touches. The 12 Aug fix
widened the margin on the gate itself, which does nothing for a dispatch that was never routed
through the gate to begin with — this is exactly why the numbers got *worse*, not better: a wider
minimum interval means more calls sitting in a longer retry backoff at any given moment, which is
more opportunity for one of those ungated retries to collide with a properly-throttled call from
another thread. The post-reauth retry path (a second, separate retry branch in
`_call_with_reauth` for expired-token recovery) had the identical bypass.

**Fixed**: `_call_with_reauth` and `_retry_rate_limited` now take an optional `throttle` callable,
invoked before every retry dispatch (backoff retries and the post-reauth retry alike) — not just
the initial call. The four quote-family call sites (`get_ltp`, `get_index_ohlc`'s and
`get_market_data`'s `getMarketData` calls, `get_option_greeks`, `get_candles`) now pass
`throttle=self._throttle_quote_call`, so a retry both waits its turn on the shared gate and
records itself as the last call, closing the gap for anything queued behind it. `place_market_order`
(the only non-quote-family caller of `_call_with_reauth`) passes no throttle and retries exactly
as before — order calls are a different rate-limit bucket and were never part of this problem.

**Also shipped, not superseded by the above**: the originally-planned temporary diagnostic log
line. `_throttle_quote_call()` now logs the measured gap and computed wait at INFO level
(`[THROTTLE] gap=...`) on every call, both the "sleeping" and "no wait needed" branches. This is
what would have surfaced the bypass directly instead of needing static analysis to find it — kept
deployed for a few sessions after this ships as confirmation the fix actually closes the gap
(gaps should never fall meaningfully under 1.3s once every dispatch is gated), then removed once
that's established. 6 new tests in `tests/test_smartapi_throttle.py` cover: the log line's two
branches, that `_retry_rate_limited` invokes `throttle` before every dispatch (including the
final successful one), that omitting `throttle` still works (the order-placement path), and that
`_call_with_reauth` threads `throttle` through to the retry it triggers.

**Not verified live** — same constraint as the 12 Aug fix: no network path to Angel One from this
sandbox, no way to reproduce the collision here. After deploying, watch for `[THROTTLE]` log
lines with `gap=` meaningfully under `1.30s` — if none appear, the bypass really was the whole
story and gaps should now consistently clear the minimum. If short gaps still show up in the log,
that would mean a further, still-unidentified path is dispatching quote-family calls outside
`_throttle_quote_call()` entirely (not just retrying around it), which would be the next thing to
grep for. Also re-run the rejection count check:

```bash
sudo journalctl -u tradingview-bot --since today | grep -c "Access denied because of exceeding access rate"
```

### SmartAPI quote-throttle margin widened, 1.05s → 1.3s (12 Aug 2026)

**Trigger:** 12 Aug 14:04, a `get_ltp` and a `get_candles` call landed 1.073s apart and Angel
One still rejected the second with "exceeding access rate" — 38 hits that session, sustained,
not a one-off. The incident report proposed either staggering the colliding jobs' schedules or
adding a shared global throttle inside `smartapi_client.py`.

**Investigated before building either.** Both of the report's own "check this first" items
came back negative — the gaps they were worried about don't exist:

- `get_candles` already calls `_throttle_quote_call()`, same as `get_ltp`/`get_market_data`/
  `get_option_greeks` (`smartapi_client.py:598`).
- The throttle's lock and last-call timestamp are already `self.` instance attributes, and
  `app/main.py:48` constructs exactly **one** `SmartAPIClient`, threaded into every consumer —
  `multi_strategy_manager`, `trade_manager`, and `originator_job` all share the same object. So
  the gate was already process-wide, not per-call-site, contrary to what "Option B" assumed
  needed building.

The real cause: the measured 1.073s gap was already *above* the old 1.05s margin — the
throttle had already done its job spacing the calls out, and Angel rejected the second one
anyway. 50ms of headroom over a nominal 1.0s/req limit doesn't survive real network/processing
jitter. That's a margin problem, not a missing-mechanism problem, and neither proposed option
(schedule staggering, a new lock) would have fixed it — staggering doesn't help once calls
already funnel through one shared gate regardless of which job triggered them, and the lock it
asked for already exists.

**Fixed**: `_MIN_QUOTE_INTERVAL_SECONDS` in `app/smartapi_client.py`, `1.05 → 1.3`. Widened
with real room to spare rather than tuned to the exact number that would have avoided one
specific incident. 4 new tests (`tests/test_smartapi_throttle.py`) cover the enforced minimum
spacing, the no-extra-wait case when calls are already spaced out, that the lock/timestamp are
shared instance state (the actual mechanism the 12 Aug incident depended on), and that the
margin clears the measured 1.073s rejection with room to spare.

**Not verified live** — this sandbox has no network path to Angel One and no real trading
traffic to reproduce the collision against. After deploying, per the task's own verification
steps:

```bash
sudo journalctl -u tradingview-bot --since today | grep -c "Access denied because of exceeding access rate"
```

should drop meaningfully from the 38/day baseline (not necessarily to zero — Angel's own side
can still throttle for reasons outside this app's control).

### `stall_exit_backtest.py` had a real timezone bug — retroactively affects two already-reported results (12 Aug 2026)

Found while building the stop-distance backtest below, which needed the same UTC-to-IST
conversion `stall_exit_backtest.py` already did for `exit_time`. That existing conversion
was wrong, confirmed empirically (not just from reading the code):

```python
exit_ts = datetime.fromisoformat(str(row["exit_time"]).replace("Z", "+00:00"))
exit_ist = exit_ts.replace(tzinfo=None) + (five_thirty) if exit_ts.tzinfo else exit_ts.replace(tzinfo=None)
```

The `+5:30` shift only applied when the parsed value carried a `tzinfo`. Writing a real
`StrategyTrade` row with `entry_time=utc_now()` (genuinely `tzinfo=utc`) through this app's
actual models, then reading it back with plain `sqlite3` (not the SQLAlchemy ORM, which
normalizes on read), produces a bare `'2026-08-12 12:42:01.118664'` string — **no `Z`, no
offset, ever**. `fromisoformat` parses that as naive, `tzinfo` is `None`, and the shift
never fires. Cross-checked against a real row from this session's own trailing-stop
discussion: raw `entry_time` `05:36:48`, independently reported as IST `~11:06` in the same
conversation — `05:36 + 5:30 = 11:06`, confirming the raw string is naive UTC text and the
existing code silently used it un-shifted.

**Consequence:** `bar[0] > exit_ist` was comparing the archive's real-IST bar timestamps
against a value still holding UTC numbers. Since UTC clock time is always earlier than IST
market hours (09:15–15:40), that threshold was satisfied by nearly every bar of the trading
day, not just the ones after the real exit — every `STALL_EXIT` counterfactual replay this
file has ever produced started from something close to market open, not from the actual
stall moment.

**This affects two already-reported findings**: the 6 Aug "STALL_EXIT is protective, net
-8.54%/trade" result, and the 12 Aug peak-MFE-conditioned exemption sweep (PR #17, "every
floor tested comes back negative"). Both need to be treated as unconfirmed until re-run —
the *sign* of the STALL_EXIT finding might still hold (holding on being worse matches the
general "index continuation is not premium continuation" pattern found elsewhere this
project), but the exact magnitudes were computed against the wrong starting point.

**Fixed**: added `db_timestamp_to_ist()` (`scripts/stall_exit_backtest.py`), which always
applies the shift — normalizing to UTC first if a value ever does carry an offset, trusting
the raw numbers as UTC when it doesn't (the only case real data hits) — replacing the old
conditional. 4 new tests including a direct cross-check against the real trade example
above. `load_premium_series` (the CSV loader) was unaffected — its timestamps come from the
archive itself, already naive IST, no conversion involved.

**Re-run both affected backtests** on the machine with real data before trusting their
numbers again:

```bash
python -m scripts.stall_exit_backtest --db data/trading.db
python -m scripts.stall_exit_backtest --db data/trading.db --peak-floors 2,3,4,5,6,7,8
```

**Re-run for real, same day.** The fix mattered, but didn't reverse the conclusion. Baseline
`NET` moved from the buggy -8.54%/trade to a corrected **-4.03%/trade** — smaller effect, same
sign, `STALL_EXIT` still protective. Bucket composition shifted too (`STOPLOSS` 8/19 vs the old
14/19, `TRAIL_EXIT` 6/19 vs 2/19), confirming the fix changed which bars were actually being
replayed, not just cosmetics. The peak-floor sweep moved the same way: every floor 2-8% still
comes back with a negative portfolio delta, just smaller in magnitude (-2.03% to -3.55%, was
-4.98% to -7.58%). **Verdict unchanged**: peak-conditioned exemption is still not supported,
and the corrected numbers are what's now trustworthy going forward.

### Stop-distance backtest (5% stop proposal) — run for real, 5% does not clear the bar (12 Aug 2026)

Proposal under discussion: tighten AI Origination's stop to 5%. Two pieces of existing
evidence already pointed against it before this ran — `stop_survivability.py`'s ~55-62%
noise-breach rate at a 10% stop (46-55% at 12%), and `scalp_stop_sweep.py`'s finding that
1-4% stops were net-negative after costs at every tested combination. Neither tested exactly
5% at AI Origination's own holding style (no fixed horizon — runs until stop/target/time-exit).

**Built**: `scripts/stop_distance_backtest.py`. Every closed AI Origination trade with
`sl_mode=FIXED` is replayed from its own real `entry_price`/`entry_time`, using its own real
target held fixed, against swept stop distances (5/7/8/10/12%, plus the trade's own actual
stop as the baseline row) — same real-premium-archive approach as `stall_exit_backtest.py`
(reuses its `load_premium_series` and the new `db_timestamp_to_ist`), not the elasticity
model, for the same reason: the model's own error margin is comparable to the effect being
measured at this distance. Trailing and `STALL_EXIT` are deliberately not simulated — this
isolates the stop-distance question from the trailing-width question investigated above,
rather than conflating them. Reports noise-hit rate, win rate, and net expectancy (real
entry/exit premium and real quantity through `app/trade_costs.py`, no coefficient needed)
per stop distance, CE and PE separately, on a chronological in-sample/out-of-sample split.
17 new tests (`tests/test_stop_distance_backtest.py`) cover the pessimistic intrabar
ordering, noise-hit classification, aggregation, and the SQL population filter (FIXED-mode
AI Origination trades only).

**Not run** — no `data/trading.db` or option-candle archive in this sandbox. Run after
deploying, and after re-running the two `STALL_EXIT` backtests above (unrelated code path,
but re-establishing confidence in the same archive/timestamp handling first is worth it):

```bash
python -m scripts.stop_distance_backtest --db data/trading.db
```

Per the task spec: if nothing clears both the net-expectancy and noise-hit-rate bars —
including 5% itself — that is the expected, useful answer, not a failure to find something.

**Run for real, same day.** Coverage was thin: 56 of 166 closed FIXED-mode trades
reconstructed (40 uncovered by the archive, 70 with no bars after entry same-day).

- **5% does not clear the bar on either side**, matching the pre-registered expectation. CE:
  every swept stop (5-12%) is net-negative in-sample, and gets *worse* as the stop widens
  (5% -1.46% → 12% -4.69%) — 5% is the least-bad CE option tested, but still negative. PE: 5%
  is the *worst* of everything tested (-1.33%, worse than the +1.01% actual baseline).
- **A real wrinkle, not a clean "no" for PE**: 7/8/10/12% all show positive net expectancy for
  PE, best at 8% (+1.66%, beating actual). But PE has **no out-of-sample split at all** — every
  PE trade landed in the same chronological bucket, so the script's own "CLEARS both bars"
  verdict for those levels rests on one slice, not genuine walk-forward confirmation. Read as a
  hypothesis worth a second look once more data accumulates, not a validated result — the same
  overreach this project has repeatedly guarded against elsewhere.
- CE's out-of-sample bucket (n=4) is correctly flagged `[THIN]` and excluded from any verdict.

**Bottom line: don't ship 5%.** Whether PE's stop should move toward 7-8% is a separate, still-open
question this run could not properly validate.

### Peak-relative STALL_EXIT exemption — run for real, still NOT SUPPORTED (12 Aug 2026)

Follow-up to the trailing-stop false alarm above. Measured against real production data
(query in chat, not repeated here): winning `STALL_EXIT` trades give back **~63% of their
peak MFE** on average, worse than `TRAIL_EXIT`'s ~50% — and because `STALL_EXIT` is gated on
`not trade.trailing_active`, **100% of that giveback happens on trades that never armed
trailing at all**. `STALL_EXIT`'s band (`abs(pnl_percent) <= 5%`, 60 minutes) is measured
against entry price with no awareness of how far a trade actually ran — a trade that peaked
at +8% and eased back to +4% reads identically to one that never moved, and both get closed
the same way.

**Hypothesis:** exempt a trade from `STALL_EXIT` once its own peak (MFE at the moment it
would stall) clears a floor well below full trailing activation, instead of the current
entry-relative-only band. Different question from the existing 6 Aug finding ("would
holding on unconditionally have been better" — no, net -8.54%/trade, protective) — this
asks whether a narrower, peak-conditioned subset behaves differently from the population as
a whole.

**Built**: extended `scripts/stall_exit_backtest.py` (not a new script — it already
reconstructs every `STALL_EXIT` trade's forward path from real archived option premium,
exactly the machinery this needs) with a `mfe_at_stall_percent` field on each reconstructed
`Replay` and a new `PEAK-MFE-CONDITIONED EXEMPTION SWEEP` section: for each candidate floor
(default 3/4/5/6/7%), buckets trades into "would be exempted" (uses the real-premium
counterfactual already computed) vs "still stalls" (keeps its real outcome), and reports
both the exempted subgroup's own delta and the whole population's mean P&L under that floor
vs. today's baseline. Every trade in the `STALL_EXIT` population has `mfe_at_stall` below
*its own* `trail_activate_percent` by construction, so these floors are real headroom below
each trade's individual activation, not an arbitrary constant. Reports the full surface,
picks no winning floor. 5 new tests (`tests/test_stall_exit_backtest.py`) cover the
exempt/non-exempt split, the portfolio-mean blending logic, and both boundary floors (0%
exempts everyone, above-every-peak exempts no one).

**Not run.** Same constraint as every backtest this cycle — no `data/trading.db`, no real
option-candle archive in this sandbox. Run on the machine with real data:

```bash
python -m scripts.stall_exit_backtest --db data/trading.db
python -m scripts.stall_exit_backtest --db data/trading.db --peak-floors 2,3,4,5,6,7,8
```

Read the sweep's own trust rule before acting on any floor: **positive portfolio delta AND
`n_exempt` at or above the trust minimum (5)** — a positive number from 2-3 trades is
exactly the single-anecdote error the break-confirmation-gate investigation earlier today
was written to avoid. If every floor comes back thin, that's itself the answer: the
`STALL_EXIT` population (tens of trades) may simply not support slicing this finely yet.

**Run for real, same day — after the timezone fix above.** Every floor from 2% to 8% still
comes back with a negative portfolio delta (-2.03% to -3.55%), sample sizes comfortably above
the trust minimum (9-16 of 19). Smaller in magnitude than the pre-fix numbers, same verdict:
peak-conditioned exemption is not supported. See the timezone-bug entry above for why the
magnitudes moved.

### AI Origination trailing stop "never activates on PE trades" — false alarm, not a bug (12 Aug 2026)

A report claimed the AI-Origination-specific trailing block in `monitor_open_trades`
(`app/multi_strategy.py`) only lives inside the long/CE branch, so `BUY_PE` trades fall
through to a bare stop/target check with no trailing at all. **Not what the code does.**

`is_short = trade.signal.startswith("SELL")` is unreachable-true for every trade this
function ever processes, PE or CE: `handle_signal` only opens a trade on `BUY_CE`/`BUY_PE`
(`SELL_CE`/`SELL_PE` are observation-only, never open a row — see the comment at
`multi_strategy.py` ~line 93), and a bought put is still long the premium, not short
anything. So CE and PE AI Origination trades already run through the exact same code path
(the `else` branch, `sl_mode == FIXED`, `trade.origin.startswith("AI_ORIGIN_")` block) —
there is no direction-keyed branch in this function to fix.

The report's own evidence table was the tell, once checked against real data
(`SELECT ... trail_activate_percent, trail_width_percent FROM strategy_trades WHERE
origin LIKE 'AI_ORIGIN%' ...`): every trade it flagged as "MFE exceeded activation, should
have armed" had in fact read `trail_width_percent` as if it were `trail_activate_percent`
— two adjacent, similarly-named columns. Real MFE was below the real activation threshold
in every single row; `trailing_active=0` was correct in each case, and the one row where it
correctly armed (MFE 29.13% vs. activate 11.59%, `trailing_active=1`, `trailing_stop`
populated) confirms the mechanism works as designed. No code change made.

**Watch this specifically when reading `strategy_trades` ad hoc**: `trail_activate_percent`
and `trail_width_percent` sit next to each other, sound similar, and swapping them makes a
perfectly healthy trade look like a stuck one.

### Break-confirmation gate for continuation entries — run for real, NOT SUPPORTED (12 Aug 2026)

**Trigger, one trade, not evidence on its own:** Bank Nifty 57700 CE, AI Origination/OpenAI,
lost -10.61% (STOPLOSS), MFE only 1.78%. `same_direction_entries_today` was 0, so the
repeat-entry gate from 11 Aug had nothing to catch here — this is a different failure mode.
The model's own reasoning: *"the move has already run for most of the session... price is
still inside the opening range."* Those two facts are close to contradictory — a move that
genuinely trended for 49 bars (`trend_duration_pct_of_session=100.0`) should, near-
definitionally, be outside the range that measures its own first 15-30 minutes. The model
stated both and didn't act on the tension.

**Hypothesis:** require a completed close beyond a structural level (opening range
high/low, or previous-day high/low — `app/market_context.py`'s already-computed
`ORB_BREAK_UP`/`ORB_BREAK_DOWN`/`PDH_BREAK`/`PDL_BREAK`) before a continuation entry is
allowed.

**Not shipped.** The task spec's own first instruction was explicit and unqualified: *"Do
not ship this without backtesting first... this trade is one data point."* Unlike the
11 Aug trend-age gate — which had a concrete, spec-endorsed anecdotal starting number
("start with 2") — this spec gave no such override and named the acceptable negative
outcome directly: *"If not supported... report that plainly... the 12 Aug trade stays a
single flagged anecdote."* This sandbox cannot run the backtest (same constraint as every
other backtest this cycle — no `data/trading.db`, no real candle history), so "supported or
not" cannot be answered here, and shipping the gate anyway would be exactly the single-day
overfitting error the spec was written to prevent. `app/ai/originator.py` is unchanged in
this pass.

**What was built:** `scripts/break_confirmation_backtest.py`, two independent parts, both
run automatically:

1. **Real AI Origination history** (`ai_origination_logs JOIN strategy_trades`) — the
   primary source, since it's the actual population the gate would act on. For every closed
   AI Origination entry, classifies `confirmed` (a direction-matched break setup was in the
   logged `setups` list at decision time — `ORB_BREAK_UP`/`PDH_BREAK` for `BUY_CE`,
   `ORB_BREAK_DOWN`/`PDL_BREAK` for `BUY_PE`) vs `unconfirmed`, and reports n / win rate /
   mean P&L / mean MFE per bucket (MFE derived from the already-stored `highest_price` vs
   `entry_price` — no new column), with a bootstrap CI on the P&L difference, overall and
   per index. At ~2 months of history as of 12 Aug, expect this bucket to be thin
   (`MIN_BUCKET_LIVE = 20`) — the script flags it explicitly rather than reporting a false
   CI on too few observations, and says so plainly rather than silently proceeding.
2. **2-year index-level fallback** — reuses the already-registered `ORB_BREAK`/
   `PDH_PDL_BREAK` setups from `scripts/backtest/setups.py` (no new setup code) as a
   direction-matched break signal, intersected with two continuation-style setups
   (`ST_ALIGNED`, `EMA_STACK` — the closest existing analogs to "ADX says trend, ignore
   duration") to ask a related question at a much larger sample: does forward edge differ
   between bars where the continuation setup fires WITH vs WITHOUT a same-direction
   structural break also active. Same `_edge`/session-block-bootstrap machinery as
   `setup_significance.py`/`trend_age_gate_backtest.py` (duplicated per this project's
   established per-script convention, not shared). This is index-direction-only — it
   cannot see AI Origination's actual entries or real premium P&L, the same limitation
   every setup-significance-style script in this project already has.

Both parts report full buckets, never pick a winning cell. 7 new tests
(`tests/test_break_confirmation_backtest.py`) cover the confirmed/unconfirmed
classification (including that a `BUY_PE` with only up-direction break setups active must
NOT count as confirmed), the MFE derivation against the trigger trade's own numbers
(101.78 vs entry 100 → 1.78% MFE, matching the incident report), the bootstrap helper, and
the break/continuation direction-matching mask. Run on the machine with real data:

```bash
python -m scripts.break_confirmation_backtest --db data/trading.db
```

Read PART 1 first — it's the actual population the gate would act on. Only fall back to
reading PART 2's verdict if PART 1's buckets are too thin to trust (expected at ~2 months
of history). If PART 1 alone shows a real, sample-adequate difference, that's sufficient to
act on without waiting on PART 2 to agree — they measure related but not identical
questions (real trades and real premium vs. index-direction-only proxies), so treat PART 2
as corroboration when available, not a requirement.

**Confidence-sizing (spec section 3) — flagged, not decided, explicitly not blocking.** The
trigger trade's own confidence (0.66) was already a discount ("cautious... rather than a
strong breakout") that changed nothing about stop, target, or size. Two options, not
mutually exclusive: tie position sizing to confidence, or set a confidence floor
specifically for entries lacking break confirmation (precisely where the 12 Aug trade
landed — 0.66, no break active). Both are a real design decision — how much size scales
per 0.1 confidence, or what floor value — that this pass deliberately leaves to the user
rather than picking a number with the same single-trade sample the rest of this entry
argues against. Worth its own dated follow-up once decided.

**Run for real, same day.** Both parts ran against production `data/trading.db`:

PART 1 (real history): 13 confirmed entries (win rate 38.5%, mean P&L −1.91%), **1**
unconfirmed entry (win rate 100%, +3.55%) — both explicitly flagged below the trust
threshold, no bootstrap computed (unconfirmed n=1 < 2 is not a comparison). The confirmed
bucket's own numbers are not good (well under 50% win rate, negative mean), but with
essentially no unconfirmed population to compare against, PART 1 cannot support or refute
the hypothesis either way, exactly as anticipated. It does surface something the spec
didn't ask about but is worth flagging: **13 of 14 closed AI Origination entries already
had some break setup active at decision time.** If that ratio holds, a hard gate requiring
break confirmation would rarely fire going forward — the population it would actually
filter is small by historical rate, independent of whether the trades in it are worse.

PART 2 (2-year fallback), the deciding read given PART 1's thinness:

- **BankNifty**: neither bucket clears zero, in either setup, and the point estimates
  actually run **backward** from the hypothesis — unconfirmed's edge is higher than
  confirmed's in both `ST_ALIGNED` (+1.80 vs +0.62pp) and `EMA_STACK` (+1.51 vs +0.48pp).
  Both CIs are wide and overlapping, so this isn't a reliable negative either — just not
  supportive.
- **Nifty**: confirmed clears zero (`ST_ALIGNED` +2.40pp `[+0.72,+3.88]`, `EMA_STACK`
  +2.62pp `[+1.13,+4.05]`), unconfirmed does not. But "confirmed is POSITIVE and
  unconfirmed is not significant" is a different, weaker claim than "confirmed is reliably
  better than unconfirmed" — unconfirmed's CI (`[-1.38,+3.90]` / `[-1.02,+3.91]`) overlaps
  confirmed's almost entirely, so the difference between the two buckets is not itself
  established.

**Verdict: NOT SUPPORTED.** This project's own stated standard for treating an effect as
real rather than noise is replication across both indices (`setup_significance.py`'s
docstring: *"consistency across partitions... beats any single low p-value... the only
defence that does not depend on a threshold"*). That fails here outright — BankNifty shows
no effect and leans the wrong way, Nifty shows a same-direction-only signal that doesn't
clear the higher bar of beating its own unconfirmed bucket. Per this task's own deliverable
3, reporting that plainly: **the gate is not being built.** The 12 Aug trade stays a single
flagged anecdote. `app/ai/originator.py` remains untouched by this investigation.

**One loose end, unresolved and worth checking directly**: the trigger trade itself
(-10.61%, 12 Aug) does not appear to be the one PART 1 counts as "unconfirmed" — that
bucket's single entry is a **win** (+3.55%), not the trigger trade's loss. Two
explanations, and this sandbox has no way to tell which: either the trigger trade hadn't
closed yet when this ran (unlikely, given it's reported closed above) or it actually *was*
classified `confirmed` — meaning some other break setup (e.g. `PDH_BREAK`) was active
alongside the opening-range containment its own reasoning cited, which would mean the
proposed gate, exactly as specified, would **not** have blocked its own trigger case. Worth
running directly against that trade before treating the incident as fully understood:

```sql
SELECT l.decision, l.setups, t.pnl_percent, t.entry_time
FROM ai_origination_logs l JOIN strategy_trades t ON t.trade_id = l.trade_id
WHERE t.index_symbol = 'BANKNIFTY' AND t.strike = 57700 AND date(t.entry_time) = '2026-08-12';
```

### Trend-age caution moved to a hard gate — partially, pending backtest (11 Aug 2026)

The soft trend-age caution added to `SYSTEM_PROMPT`/`_build_user_prompt` (~7 Aug) was
explicitly an observation window: *"if trades keep firing with
`same_direction_entries_today: 5+` and no change in behavior, that's the evidence needed
to justify the harder gate."* 11 Aug produced that evidence — 7 same-direction `BUY_PE`
entries across two indices, `same_direction_entries_today` already at 1 or 2 before four
of them opened, the model naming the exact risk in its own reasoning each time and trading
anyway. Soft caution doesn't reliably translate to behavior.

**Shipped:** `app/ai/originator.py`'s `_open_trade` now hard-blocks a new entry when
`same_direction_entries_today[decision.action] >= 2` (`_MAX_SAME_DIRECTION_ENTRIES_BEFORE_BLOCK`),
checked first, before any contract resolution or quote-budget spend — same position in the
function as the DTE floor, same log-and-`return None` pattern, so the skip shows up in
`ai_origination_logs` the same way every other declined/blocked decision already does
(no new plumbing needed; `record_decision()` is already called unconditionally regardless
of why `_open_trade` returned `None`). Threshold of 2 is the one concrete number the
incident review gave (today's losing entries were already at 1 and 2 same-direction
entries on the books) — **not backtested**, an anecdote-derived starting point, same
caution this project applies to every other single-day threshold choice.

**Deliberately NOT shipped:** a `trend_duration_pct_of_session` gate. Today's entries were
uniformly at 96–100%, and the incident review flagged trend duration as possibly the more
robust of the two signals — but it only gave a sweep range to validate (80/90/95%), never
a committed number, and picking one from a single day's data is exactly the overfitting
error this project has repeatedly guarded against elsewhere (see the holdout-discipline
entries below). The field is still fully computed, logged, and shown to the model — this
only withholds a second hard-coded threshold pending real validation.

**Validation tooling built, not run — same constraint as every backtest this cycle.** This
sandbox has no `data/trading.db` and no real candle history (confirmed again this session:
`sqlite3.OperationalError: no such table: candles`, identical to how every other backtest
script in `scripts/` already fails here). `scripts/trend_age_gate_backtest.py` computes two
proxies from the existing 2-year archive rather than needing new data: `same_direction_count_today`
(how many times a given setup already fired the same direction earlier that session, against
its own signal history) and `trend_duration_pct` (an exact re-derivation of
`app/market_context.py`'s `compute_trend_age`, run-length of the 5-min Supertrend direction
over bars elapsed since session open). Sweeps entries-thresholds 1/2/3/4 and trend-duration
80/90/95%, reports the full parameter surface (never picks one cell) on both an in-sample
slice and a chronological out-of-sample tail — **not** the locked holdout
(`data/holdout_record.json`); this is risk-control validation, not a search for a new
directional edge, so per the roadmap it doesn't spend that scarce resource. Run on the
machine with real data:

```bash
python -m scripts.trend_age_gate_backtest --db data/trading.db
```

Read the "PROTECTIVE-THRESHOLD CHECK" section first — it names any (setup, threshold) cell
where the at-or-above bucket is reliably worse than the below bucket in *both* the
in-sample slice and the untouched out-of-sample tail. If nothing clears that bar, that
doesn't necessarily mean the shipped gate is wrong: it protects against a rare,
high-severity pattern (7 correlated entries in one day) that a 2-year archive of ordinary
setup re-fires may not reproduce at a testable sample size — read alongside each cell's
`n` before concluding either way.

**Scope respected:** confirmed via `git diff` that this touched only `_open_trade` (42
insertions, 0 deletions) — `SYSTEM_PROMPT` and `_build_user_prompt` are byte-for-byte
unchanged, so the model's own trend-age reasoning still runs exactly as before; the gate is
a backstop, not a replacement. This ends the "soft-only" phase of the trend-age fix
specifically — it does not end the broader two-week observation window for anything else
(correlated-entry flag data collection continues).

**Cross-strategy correlation — flagged, not built.** The trigger evidence described BNV11
and AI Origination taking the same Bank Nifty PE thesis 40 minutes apart. **Could not
corroborate "BNV11" anywhere in this repository** — code, tests, or `CLAUDE.md`'s own
strategy list, which names only BNV5.1, BNV6, BNV7, and NV1 (see "The shared-FIXED-branch
hazard" above). It may be a live strategy configured directly via the TradingView
webhook/admin UI with no corresponding code (structurally possible — `StrategyTrade.strategy_name`
is just a label, nothing in Python requires a matching branch), or a name mismatch in the
review — this sandbox has no live trade data to check either way. Confirm which before
trusting that specific data point.

That said, the underlying question stands regardless of the name: the existing
`concurrent_correlated_entry` flag only tracks Claude-vs-OpenAI agreement *within* AI
Origination (`_find_correlated_entry` in `app/ai/originator.py`, scoped to
`origin.like("AI_ORIGIN_%")`). Every rule-based strategy (BNV5.1/BNV6/BNV7/NV1) and AI
Origination independently trade the same underlying instruments and the same CE/PE
direction space, with no shared view of each other's open positions or recent entries —
so the same failure mode this gate addresses (multiple independent systems reaching the
same conclusion and compounding exposure rather than diversifying it) is structurally
possible across strategies, not just within AI Origination's two providers. Worth a
follow-up gate if real trade data shows it happening with any frequency — deliberately not
built here, since it is a materially bigger change (touching strategy systems that are
currently profitable and under their own freeze) than this one's scope.

### AI Origination's entry signal does not work (30 Jul 2026)

A two-year backtest over ~37,000 five-minute bars per index found **no positive
directional edge** in the 45-minute drift rule the originator runs on — on either index,
at either horizon, in any drift band. Zero of 16 bands clear a Bonferroni threshold. Six
bands are reliably *negative*, and they cover the drift range where ~25,000 of ~37,000
bars sit, i.e. where almost every trade actually happens.

This supersedes the earlier "breakeven gross, uncompensated for costs" diagnosis. It is
not a cost problem and it is not a prompt problem. **Do not build entry gates, enriched
prompts, or setup filters on this signal without new evidence** — enriching the context
around a non-predictive input produces better-argued coin flips.

Full numbers, method and caveats: `docs/ai-origination-roadmap.md`.

### The rule-based strategies are NOT validated either (31 Jul 2026)

Measured on entry quality over the same two years, same test as everything else:

- **BNV7** is reliably *anti-predictive* — −5.95pp (Bank Nifty) and −4.37pp (Nifty) at
  30 min, CIs excluding zero on both. The worst performer of any setup tested. Caveat:
  this measures fixed-horizon direction, while BNV7's live exits are `v7_manager`'s
  trailing engine, which could rescue a poor-direction entry. Evidence about the entry,
  not proof about the strategy.
- **NV1** fires 46/62 times in 22 months — under three a month, untestable. Its claimed
  PF 5.18 rests on 19 trades.
- **BNV5.1 and BNV6** cannot be tested at all: both gate on VWAP, which needs volume, and
  index candles report volume as zero. **Archiving FUTIDX candles is the highest-value
  data task** — it is the only route to assessing half the live strategy set.

Do not assume the rule-based strategies are the validated component. Three of four are
unassessed and the fourth looks bad.

### The holdout was spent on 31 Jul 2026 and did NOT confirm

`data/holdout_record.json` records it. Window 2026-05-29 to 2026-07-28, candidates
`EMA_STACK@1100_1400` and `ORB_BREAK[hold=2]@1100_1400`. All four cells NOT CONFIRMED,
all net negative (−1.68% to −2.37% per trade after costs and decay).

Win rates were fine at 52–59%. The **win/loss ratio killed it: 0.53–0.68**, average win
~6% against average loss ~9–11%.

**The holdout is used. Re-running it turns it into in-sample data and retroactively
invalidates this result too.** `holdout_test.py` refuses without `--force`, and a forced
run is logged permanently. Any new hypothesis needs genuinely fresh out-of-sample data.

Net position: two years of candles, three independent signal constructions and a locked
holdout have not demonstrated a tradeable edge in intraday index direction.

### Walk-forward revised this: the problem is likely the EXIT, not the entry (31 Jul 2026)

Six consecutive windows over two years show the midday setups are **MOSTLY POSITIVE or
better almost everywhere** — Nifty `ST_ALIGNED` 6/6 windows positive (mean +3.96pp),
`ORB_BREAK[hold=2]` 5/6 with 4 significant (mean +4.41pp). The edge is *not* concentrated
in one period.

Caveat: those setups were selected on this same data, so read it as "conditional on that
selection, the edge isn't period-specific" rather than independent confirmation.

The reconciliation with the failed holdout is arithmetic, not contradiction. At +4.41pp
with symmetric ±12% payoffs: gross +1.06%, costs −0.56%, theta −1.00% → **net −0.50%**.
A 4pp edge does not survive costs plus decay under the current exit configuration.

The holdout showed the mechanism: hit rates were fine (52–59%), but average win ~6%
against average loss ~9–11%. Almost nothing reaches the 20% target because the 8%/5%
trail exits first, while losers run the full 12% stop.

**So the working conclusion is now: a small, real, stable entry edge exists, and the risk
construction spends more than it is worth.** Do NOT tune exits on the holdout (spent) or
on the two-year data (spent by selection). That needs fresh out-of-sample data, which is
what the live paper system accumulates — making "keep paper running" the highest-value
standing item.

### Indicator setups showed a fit-window edge — but only midday, and it did not hold up (30 Jul 2026)

Separate from drift, and this is the current live thread. Momentum and breakout setups
(`EMA_STACK`, `ST_ALIGNED`, `ORB_BREAK`, `PDH_PDL_BREAK`) show a replicated positive edge
**between 11:00 and 14:00** across both indices and both horizons, and are reliably
*backwards* after 14:00. `NIFTY 60min EMA_STACK 1100_1400` at +3.95pp is the only cell in
any analysis to clear a Bonferroni threshold (p = 0.000035 over 484 comparisons).

Economics are marginal: +3.95pp is ~0.95% gross against ~0.56% costs. Nifty is net
positive by a thin margin; Bank Nifty is not.

**Resolved 31 Jul.** Both follow-ups are done:

- The late-session momentum reversal was a **truncation artefact** — forward windows are
  clipped at session end, and every setup reversal in 14:00–15:15 vanishes at a 15-minute
  horizon. Withdrawn. `setup_significance.py` now flags truncated cells.
- Drift's late-session negatives **do** survive the same check, so low-drift late-session
  mean reversion is real. Two similar-looking findings, one artefact and one not —
  replication across indices did not distinguish them, only the horizon check did.
- Conditional drift: the low-drift negative holds at 15/30/60 min on both indices, which
  makes it the most robust result in the exercise. One narrow exception — Nifty 0.25–0.50%
  drift midday, positive at all three horizons but single-index.

**Whenever a forward-window analysis is sampled near a session boundary, check it at a
horizon that fits inside the window before believing it.**

### Put/call sensitivity asymmetry

ATM puts are 1.3–1.5× more sensitive than calls (Nifty λ −97 vs +64, Bank Nifty −72 vs
+56, fitted from real option candles and validated against first principles). So an
identical percentage stop is a **materially tighter index distance on a PE than a CE** —
12% on a Nifty put is ~0.11% of index movement versus ~0.18% on a call.

Nobody chose that asymmetry, and it persists under any entry rule. **Specify future risk
parameters in index points or ATR multiples, not premium percent** — a "12% stop" is
2.02 ATR on a Nifty call and 1.27 ATR on a put, which are different bets wearing the
same label.

The asymmetry shrinks with DTE: Bank Nifty ATM is 1.29 at 2–5 DTE but 1.11 at 21+. So
the rescale bites hardest on Nifty weeklies (1.59 at 2–5) and barely at all on the Bank
Nifty monthly.

### Fitted lambda is attenuated at long DTE, and it matters asymmetrically

Measured 3 Aug: ATM CE fits sit below `delta × spot / premium` by −3% (2–5 DTE), −11%
(6–10) and −20% (21+) on Bank Nifty. Monotonic with DTE and always negative — that is
the Epps effect, not a units error. A longer-dated contract prints less often, so its
1-minute close is more often stale against a fresh index close, biasing the slope toward
zero. `calibrate_premium` now detects and names this pattern.

Consequences differ by consumer, and this is the part to remember:

- `symmetric_premium_percent` uses a **ratio** of PE to CE lambda *within one bucket*.
  Both legs are attenuated alike, so the CE/PE stop rescale is essentially immune.
- `to_risk_units` **divides** by lambda. An understated lambda overstates index points
  and ATR multiples, so reported ATR distances on the longest-dated contracts read wider
  than they are. Treat Bank Nifty monthly ATR figures as an upper bound — part of the
  "Bank Nifty 5.16–6.2 ATR vs Nifty 2.5–3.7" gap seen on 3 Aug is measurement, not risk.

### Days-to-expiry materially affects stop survivability

Same 12% stop, breached by noise within 60 min: Bank Nifty calls 36.5% at 2–5 DTE versus
23.4% at 6–10 DTE. Longer-dated contracts carry higher premium, so the same percentage
is a wider index distance.

**Resolved 3 Aug.** AI Origination now passes a 5-DTE floor that *rolls forward* to the
next listed expiry rather than skipping the trade, and stop/target/trail percentages are
rescaled through `symmetric_premium_percent()` so a CE and a PE at the same nominal
setting are the same index distance. Confirmed holding in live data — all trades at 8 or
22 DTE, nothing under 5.

Coefficient coverage is the constraint on that rescale, and it is now a first-class
check: `calibrate_premium` reports covered vs missing DTE buckets per index and names the
`pull_option_candles` command to fill a gap. A missing bucket is not a rounding error
there; elasticity varies more than 2× across the traded range.

**Filling a DTE bucket means pulling the same contract more than once, as it ages.**
One pull only ever reaches the DTE the contract happened to be at. For a 25 Aug expiry:
pull around 27 Jul–3 Aug for `21+`, around 5–14 Aug for `11-20`, around 15–20 Aug for
`6-10`. `pull_option_candles` merges on re-run (it used to skip any contract whose file
existed, which made every pull after the first a silent no-op — that is why `11-20` was
empty). Today's date is always re-fetched, since a mid-session file holds a partial day.

### pandas is already in the live import graph (corrected 5 Aug)

`app/option_finder.py` imports pandas at module level, `app.main` constructs
`OptionFinder`, so pandas **and numpy** have always been loaded in the live process.
Several comments claimed otherwise; `tests/test_module_imports.py` now pins the actual
set so it cannot grow silently.

The rule was worth wanting — pandas is 50–80 MB on a 414 MB box already carrying ~106 MB
of app. `option_finder` uses it only to filter the instrument master, which
`app/option_chain.py` does with plain dicts for exactly this reason. Replacing it is a
genuine memory saving but sits in the live strike-selection path, so treat it as an
opportunity needing its own testing, not a cleanup.

### Backtest tooling

`scripts/backtest/` (numpy-only, isolated from `app.main`'s import graph — a stray pandas
import there costs 80 MB on a 414 MB box), plus `band_significance.py`,
`calibrate_premium.py`, `backtest_baseline.py`, `backfill_candles.py`.

Two traps already fallen into once each, both documented in the roadmap: overlapping
forward windows inflate significance by ~√(window/stride), and premium *elasticity* is
not *delta* (they differ by ~200× for Nifty).

### Scalping-horizon backtest tooling built, but NOT run -- no historical data in this sandbox, 10 Aug 2026

Built the tooling for the scalping-horizon roadmap (deliverables 1, 1a/1b, 2, 2b), but
**could not produce a single real result.** This sandbox's `data/trading.db` doesn't
exist, the checked-in dev DB's `candles` table has 0 rows, and `data/option_candles/`
doesn't exist either -- there is no historical candle data or option-premium archive
anywhere in this environment, and (as throughout this whole session) no SmartAPI
credentials or network path to fetch any. Everything below was built against
`scripts/backtest/`'s real machinery and verified with synthetic data
(`tests/test_ema_rsi_cross.py`, `tests/test_scalp_stop_sweep.py`,
`tests/test_scalp_breakeven.py`, 25 tests total) -- **run the commands below on the
machine that actually has `data/trading.db` and `data/option_candles/` for real numbers.**

**What got built:**

- **`EMA_RSI_CROSS`** (`scripts/backtest/setups.py`) -- EMA9/EMA21 crossover confirmed by
  RSI(14), a genuine crossover EVENT (fires once, on the bar the cross happens), distinct
  from `EMA_STACK`'s level check (true for as long as the stack holds). Two variants
  registered in `default_setups()`, `entry_offset=0` (same-bar close) and `entry_offset=1`
  (next-bar close) -- the explicit look-ahead-bias control the roadmap asked for. The
  offset variant is implemented by shifting the SIGNAL array forward one bar rather than
  patching the simulator, since `compute_outcomes()` already enters at `close[i]` for
  whichever bar carries the signal -- shifting the signal is sufficient and required no
  changes to the shared simulator. Dropped at session boundaries (a Friday-close cross
  cannot become a Monday-open entry).
- **`scripts/scalp_stop_sweep.py`** (new) -- the target/stop sweep, reusing
  `scripts/backtest/outcomes.py`'s `compute_outcomes()`/`RiskCombo` (the same target/stop/
  trail simulator other backtests already trust) rather than a second parallel one, masked
  to whichever bars a setup actually signals on. Sweeps the exact grid specified (target
  3% x stops 1/1.5/2/2.5/3%, target 5% x stops 1.5/2/2.5/3/4%) at configurable holding-
  period caps, reporting win rate, a noise-hit rate on stop-outs specifically (MFE within
  20% of the stop distance -- a documented judgment call, not a measured threshold),
  realized (not nominal) reward:risk, and net expectancy using the real fitted premium
  multiplier and `app/trade_costs.py`'s cost model against the archive's own median
  premium.
- **`scripts/scalp_breakeven.py`** (new) -- deliverable 1, meant to be run and read FIRST.
  Computes round-trip cost %% from the real cost model against the archive's own median
  premium (not the flat ~0.56-0.6%% figure quoted elsewhere -- that number should fall out
  of this, not be assumed into it), and the empirical distribution of NON-OVERLAPPING
  absolute premium moves at each holding period from the real archived 1-minute option
  data. Reports the fraction of windows where the move alone (regardless of direction)
  would clear costs -- an upper bound no directional signal can beat.
- **`scripts/walk_forward.py`**: added `--horizon-bars` (previously a hardcoded 60-minute
  constant) so it can validate the new setups at scalping horizons without a second
  walk-forward implementation -- `--setups EMA_RSI_CROSS --interval ONE_MINUTE
  --horizon-bars 5`, for instance.
- **`scripts/setup_significance.py`'s existing `--horizons` flag already covers the
  ORB_BREAK re-test** (roadmap item 1b) -- no new code needed. It takes forward-bar
  counts at whatever `--interval` is loaded; at the default `FIVE_MINUTE` that's
  `--horizons 1,2,3` for 5/10/15-min forward windows, or load `--interval ONE_MINUTE` for
  genuine 1-minute resolution including the 3-minute case. Its Bonferroni correction
  already scales the comparison count automatically with whatever `--setups`/`--horizons`
  are actually run, so including `EMA_RSI_CROSS` inflates it correctly with no extra work.
- **`scripts/holdout_test.py`** already takes an arbitrary `--candidate` label -- no new
  code needed there either, just a fresh holdout window (see caution below).

**Run in this order** (on the machine with real data):

```bash
python -m scripts.scalp_breakeven --candles data/option_candles
python -m scripts.setup_significance --setups ORB_BREAK --interval FIVE_MINUTE --horizons 1,2,3
python -m scripts.setup_significance --setups EMA_RSI_CROSS --interval ONE_MINUTE --horizons 3,5,10,15
python -m scripts.scalp_stop_sweep --db data/trading.db --setups EMA_RSI_CROSS
python -m scripts.walk_forward --db data/trading.db --setups EMA_RSI_CROSS --interval ONE_MINUTE --horizon-bars 5
python -m scripts.holdout_test --db data/trading.db --candidate "EMA_RSI_CROSS[entry_offset=0]" --holdout-start <fresh date>
```

**Real constraint worth flagging before any of this runs**: SmartAPI serves roughly 28
days of 1-minute history per request (see the option-candle-pull gotcha above), unlike
the two-year `FIVE_MINUTE` archive the 30/60-min work ran against. Genuine 1-minute
scalping analysis is therefore bounded to whatever rolling ~28-30 day window has actually
been backfilled at `ONE_MINUTE` resolution -- a much smaller sample than the two-year
history, which affects both statistical power and how tight a fit/holdout split can be.
Check actual `ONE_MINUTE` coverage in `data/trading.db` before trusting a "no edge"
verdict at this resolution the way the two-year 30/60-min verdict is trusted.

**Also flagged, not modeled**: a signal firing 30+ times a day has 30x the slippage/
execution-risk exposure of one firing once, and nothing in `compute_outcomes()` or this
sweep models that -- it's a real gap in what this tooling can answer, not something
papered over with an assumption.

**Scope respected**: no changes to entry/exit logic, risk parameters, the AI Origination
prompt, or the trend-age observation window. `EMA_RSI_CROSS` and the two new scripts are
backtest-only, never imported by `app.*` (confirmed by `tests/test_module_imports.py`'s
existing `scripts.` isolation check, which already fails the whole suite if that boundary
is ever crossed).

### Dashboard "Market Conditions" panel -- read-only, zero new calls, 10 Aug 2026

Added a per-index panel to `/` showing the same regime/ADX/CPR/setups snapshot the
`[AI][ORIGIN][CTX]` log line already prints, plus a 🟢/🟡/🔴 tradability read -- until
now the only way to see "is Bank Nifty trending right now" was grepping that log line.

`app/platform.py`'s `get_market_conditions()` is a pure read of the **most recent**
`AIOriginationLog` row per enabled index (Friday's per-decision persistence work) --
zero new computation, zero new SmartAPI calls, confirmed by log grep during local
testing. Both AI providers share one `market_context` per index per cycle (see the
originator.py loop structure), so picking the single latest row regardless of which
provider wrote it is correct, not an arbitrary choice.

**Tradability reuses `ADX_NO_TREND`/`ADX_TRENDING` directly from `app/market_context.py`
(imported, not re-declared)** -- TRENDING at ADX ≥ 25, MARGINAL at 20-25, NOT_TRADABLE
below 20, UNKNOWN when ADX hasn't warmed up yet. Deliberately keyed on the ADX bands
alone rather than the stored `regime` field: `regime` only reads TREND when CPR is
*also* NARROW (see `compute_cpr`'s classification in `market_context.py`), so a
wide-CPR day with ADX 30 would show MIXED even though the model's own system prompt
("Above 25 continuation is better supported") treats that ADX as meaningful on its own.
Showing `regime` alongside as separate data avoids losing that distinction while still
answering "is there a trend" primarily from the same numeric threshold the prompt itself
states in as many words. Labeled "Informational -- reflects AI Origination's own regime
read, not a second trading gate" in the template; nothing in the trading path reads it.

Verified live (seeded rows directly, no real SmartAPI/network needed for this one): a
NARROW/ADX 28.4 row renders TRENDING, a WIDE/ADX 14.2 row renders NOT_TRADABLE, a
MODERATE/ADX 22.5 + `data_stale=true` row renders MARGINAL with the stale badge, and the
panel correctly picks up the *latest* row when multiple exist for the same index rather
than the first. The panel rides the dashboard's existing 10s `/api/live-dashboard` poll
(pre-existing, not a new refresh cycle) -- the underlying content just doesn't change
until origination's own 5-min cycle writes a new row, same as the log line it replaces.

### "Shared candle store" proposal declined, real fix was the /active-trade-page LTP cache, 7 Aug 2026

A follow-up to the WebSocket feed asked for a single background job to refresh
`index_candle` on a fixed interval (recommended ≤1 min), replacing what it described as
"historical candle fetches... fetched independently by whichever consumer needs them,
whenever they need them."

**That premise is false. There is exactly one live candle-fetch call site in the entire
app**: `_load_market_context()` in `app/ai/originator.py`, called once per enabled index
per 5-minute origination cycle (confirmed by grepping every `.get_candles(` call site --
the only other two, `market_data.py`'s `capture_closing_auction` and the three
`scripts/*.py` backfill tools, run once a day at 15:45 IST or are market-hours-guarded,
neither contends live). Both AI providers share the one `market_context` built per index
per cycle; it is not re-fetched per provider. At two enabled indexes that is at most 2
candle calls per 5 minutes -- roughly 24/hour, already light, and `get_candles()`
already deliberately shares `_throttle_quote_call()` with every `get_ltp`/
`get_index_spot` call (see that method's own docstring), so it was never a separate,
uncoordinated contention channel to begin with.

**Building the proposal as specified would have made SmartAPI candle-call volume worse,
not better.** A background refresher at the recommended ≤1-minute interval, for 2
indexes, is up to 2 calls/minute -- roughly 120/hour, a 5x increase over origination's
current ~24/hour, competing for the exact same throttle bucket real trading's `get_ltp`
calls depend on. The proposal's own stated goal (call volume dropping to "a small, fixed
number independent of dashboard or origination activity") would not have been met by its
own design, because there was only ever one consumer for candles to share among.

**What actually still causes contention, identified in the previous PR and confirmed
here**: `app/dashboard_routes.py`'s `/active-trade-page` calls `smartapi.get_ltp()` once
per open trade, uncached, on every render -- a real, still-live, traffic-scaling
SmartAPI call path the WebSocket feed doesn't cover (that feed is index spot only, on a
small static token set; option premium per open trade is a different and dynamic
instrument set). This is the actual remaining dashboard-driven contention source, not
candles. Fixed with the same proven TTL-cache pattern used for `get_index_live_figures`
before the WebSocket feed replaced it: a 5s in-process cache keyed per contract
(`symboltoken`), so distinct trades don't collide. `trade.current_premium`/`pnl_percent`
writes on this route were already display-only -- `get_db()` never commits, so this
changes no persistence behavior, only which requests pay a fresh SmartAPI cost.

A genuinely shared, persistent option-premium feed (mirroring `app/live_feed.py` but for
a dynamic per-trade token set rather than 2 static index tokens) is a legitimate future
upgrade if TTL caching turns out insufficient under real load -- meaningfully bigger
scope than this fix, deliberately not bundled in here.

### Dashboard-driven SmartAPI rate exhaustion, 7 Aug 2026

`get_index_live_figures()` called `smartapi.get_index_spot()` fresh on every dashboard
render, sharing the same process-wide 1 req/sec quote throttle as AI Origination's own
candle-refresh calls (`_throttle_quote_call()` in `app/smartapi_client.py`). A burst of
dashboard requests could starve live trading of that budget for no reason -- the spot
price is already kept current independently by the 5-min origination cycle and the 30s
trade monitor, so the dashboard doesn't need a fresh broker call per view.
`_live_dashboard_data()` (`app/dashboard_routes.py`) now caches the result behind a 5s
in-process TTL (`_live_figures_cache`), safe because uvicorn runs this app single-process
(no `--workers`). Verified locally: 5 uncached requests took 4.22s (~0.84s/request, one
throttle hit each); 20 cached requests took 0.22s total.

**Correction to the incident report that prompted this:** the report's stated root cause
was "zero authentication on any route in `app/dashboard_routes.py`." That's wrong for
this codebase as it stands -- `app/auth.py`'s session-based admin auth
(`require_admin_page`/`require_admin_api`) has existed since the first commit and is
wired onto every route including `/` and `/api/live-dashboard`, confirmed by reading
every route in the file and by live `curl`: unauthenticated `GET /` returns 303 to
`/login`, `GET /api/live-dashboard` returns 401, neither reaches
`get_index_live_figures()` or SmartAPI. If the reported traffic (170 requests, 10+ IPs,
no login) really did trigger real broker calls, the deployed server is very likely
running older code than this repo -- **check what's actually live before assuming this
gap needs closing again.** The caching fix above is worth having regardless of that
question, since it also protects against a burst of *authenticated* dashboard traffic
(several tabs, a monitoring script) doing the same thing.

**Same "zero auth" claim resurfaced a second time, same day, after the caching fix
shipped and measurably worked (Nifty candle-refresh failures 24→8 across equivalent
windows).** The follow-up request's own suggested check --
`grep -rn "HTTPBasic\|Depends(get_current_user)\|verify_credentials\|APIKeyHeader"
app/*.py` -- returns nothing, which is exactly why it keeps reading as "zero auth": that
grep doesn't match this codebase's actual pattern name
(`require_admin_page`/`require_admin_api`). **If a future investigation reports missing
dashboard auth, check whether its grep included those two names before trusting it.**
Re-verified live with a fresh local run: `/` → 303, `/ops` → 303, `/ai-origination` →
303, `/api/live-dashboard` → 401, all unauthenticated; all four return 200 with a valid
session cookie. Also: **`/dashboard` is not a route this app has** -- the live dashboard
is `/`, a separate summary page is `/ops`. A `curl .../dashboard` in any verification
step will 404 regardless of auth state, which is itself a sign the check wasn't run
against this app's real routes.

Did not add a second (HTTPBasic) auth layer on top of the existing session-based one --
that would be exactly the "second, inconsistent auth pattern" the task itself said to
avoid if something already exists. The 8 residual candle-refresh failures post-caching
are more consistent with ordinary contention (a legitimate authenticated session polling
right as the cache expires, colliding with AI Origination's own cycle) than with bots
still getting through -- roughly one every 7 minutes, not the sustained hammering 170
blocked requests would produce. If this needs resolving further, the next real
diagnostic step is checking the live server's own access logs for the status codes on
those 170 requests (303/401 would confirm auth is doing its job and the traffic is
harmless noise), not adding more auth code.

**Candle-refresh failures then jumped sharply within the same day (22 → 128 in the same
14:00-15:15 window, re-checked), and the fix escalated to a persistent WebSocket feed
replacing per-request/cached calls entirely.** `app/live_feed.py` (new) wraps
`SmartWebSocketV2` (from the already-installed `smartapi-python` package) in a single
background thread, started once at app startup (`app/main.py`'s lifespan) and subscribed
to every enabled index's spot token. `LiveFeedStore` is the in-memory result --
thread-safe, one instance per process (uvicorn runs with no `--workers`, same reasoning
as the caching fix it replaces). `get_index_live_figures()` (`app/platform.py`) now reads
from this store instead of calling `smartapi.get_index_spot()`; it does NOT fall back to
a fresh SmartAPI call on a stale/disconnected feed, only on a feed that has never
produced a value for that index at all (still-fail-closed, matches this codebase's usual
philosophy) -- a real disconnect is served as the last-known price with `is_live: false`,
shown on the dashboard as a "stale" badge, and the reconnect loop handles recovery on its
own. `SmartAPIClient` gained two small read-only properties (`jwt_token`, `feed_token`)
so the feed can read them fresh on every reconnect attempt without reaching into private
state, since `_call_with_reauth` can rotate them independently while the feed runs.

**Not verified against the real Angel One feed** -- no network path to Angel One from
this sandbox (proxied HTTPS/WSS is blocked) and no real credentials, so this was built by
reading the installed SDK's actual source (`SmartApi==1.5.5`) rather than against a live
tick stream. Two assumptions specifically need confirming once deployed: (1) the WS
binary tick's `last_traded_price` is paise (Angel's documented convention) and gets
divided by 100 -- if wrong, every price reads exactly 100x off; (2) the outer reconnect
loop actually recovers from a real disconnect, not just from the fake `SmartWebSocketV2`
substitute the unit tests use. Verified locally with a running instance (dummy
credentials, no real feed reachable): the app starts cleanly with the feed unable to
connect, logs `[LIVEFEED] Waiting for SmartAPI authentication before connecting` rather
than crashing, and the dashboard renders "Live feed has not produced a price for this
index yet" rather than a stale guess. Confirmed the actual point of this change with 20
rapid authenticated dashboard requests producing **zero** `SmartAPI ltpData` log lines
(the caching fix above still produced roughly one call per 5s TTL window under the same
test; this produces none, ever, regardless of traffic volume).

**Found but out of scope for this change:** `/active-trade-page`
(`app/dashboard_routes.py`) calls `smartapi.get_ltp()` once per open trade, uncached, on
every render -- a separate, still-live uncached SmartAPI call path from the dashboard,
for option premiums rather than index spot. Not touched here since it's a different data
need (per-trade LTP, not index spot) and the task explicitly scoped this fix to index
prices; worth its own look if rate-limit contention continues after this ships.

### Two production incidents fixed, 5 Aug 2026

**Claude `max_tokens` truncation.** Live logs showed Claude returning `stop_reason=
'max_tokens'` on both BANKNIFTY and NIFTY origination cycles, with `output_tokens_
details.thinking_tokens` equal to the full 256-token cap and zero tokens left for the
JSON payload — extended thinking was consuming the entire budget before the model could
answer. `app/ai/originator.py`'s and `app/ai/exit_shadow.py`'s `_call_claude` (identical
duplicated code, same 256 cap) both raised to `max_tokens: 2048`, matching the headroom
`app/ai/claude.py`'s signal-review path already used for the same reason. Also added a
"keep reasoning brief" instruction to both Claude system prompts as a second line of
defence — raising the cap doesn't stop a verbose model from eventually re-hitting any
limit.

**Option-chain collector rate-limiting live trading.** Collection cycles at 10:55 and
11:00 IST were followed within seconds by AI Origination candle-refresh failures
(`Access denied because of exceeding access rate`) on both indices. `_should_yield_to_
live_trading()` only reacts *after* the live client has already been rate-limited that
cycle — it doesn't prevent the collision, only shortens it. This is the third incident
traced to running the collector in SHARED mode (no `SMARTAPI_ANALYTICS_*` configured):
31 July's 2,890-error storm, a crash loop the night after, and this one.
`option_chain_collection_enabled` now **defaults to `False`** (both the `Settings`
dataclass field and the `get_settings()` env-var fallback — the latter is what actually
governs runtime behavior, so both had to change). The dedicated-credentials isolation
path (`SMARTAPI_ANALYTICS_*` → `build_collector_client()` → `as_analytics_credentials()`)
was already fully implemented, just never provisioned with real second-key credentials.
**Do not re-enable `OPTION_CHAIN_COLLECTION_ENABLED` without `SMARTAPI_ANALYTICS_*` set
to a genuinely separate Angel One API key, and a full session run alongside live
origination showing zero rate-limit errors on either side.**

Neither fix could be verified live from this sandbox — no SmartAPI credentials here, and
this sandbox is not connected to production. After deploying: confirm the next live
Claude cycle produces a real decision (not another `max_tokens` stop), confirm no more
`[CHAIN]` log lines appear, and confirm candle-refresh failures stop. Also still needs
checking on production: whether `data_stale=True` fired correctly for AI Origination
decisions made during the 10:56:55–10:57:06 and 11:01:46–11:02:02 IST failure windows,
and whether any trade opened in those windows is flagged as suspect in the export.

### STALL_EXIT is protective — do not loosen it (tested 6 Aug 2026)

Prompted by three Bank Nifty trades on 6 Aug that stalled out while the index carried on
~360 points. `scripts/stall_exit_backtest.py` replays every STALL_EXIT forward against
the **real archived option premium** of the actual contract:

- 14/19 would have hit STOPLOSS (mean −12.75%), 3/19 TIME_EXIT, 2/19 TRAIL_EXIT
- holding on was better in **2 of 19**, mean **−8.54%** per trade, sign test p ≈ 0.0007
- the three 6 Aug trades that triggered the question all reconstruct as **STOPLOSS**

**Conditioning on ADX is refuted, not merely unsupported.** Every one of the 8 trades with
ADX ≥ 25 was worse held; the only winner sat at ADX 24.3. A "skip STALL_EXIT when ADX ≥ 25"
rule would have hurt in 8 of 8 applicable cases.

The lesson generalises past this rule: **index continuation is not premium continuation.**
A stall means the option was already failing to convert index movement into premium.
Holding extends the theta bleed rather than resuming the conversion, and a 10% stop is
reachable on an ordinary pullback inside a continuing trend. A chart-based read of
"the trend kept going, so we exited early" is exactly the intuition this measures against.

### Still unconfirmed

- Whether Nifty's spot token was corrected to `99926000` in Settings > Instruments.
- Whether the BNV6 Pine Script JSON comma bug (missing comma after `htf_confirmation`,
  trailing comma before the trend object's closing brace) was fixed on TradingView.

### Standing caution

Live-trade sample sizes remain small (tens of trades, days not weeks). Be honest about
that in any analysis; do not present a four-day result as established. The four-day
result that started this work read as "weak but real edge" and two years of data said
otherwise.
