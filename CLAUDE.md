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

### Does AI Origination's exit construction show the same win/loss asymmetry the holdout found? Tooling built, not run (31 Aug 2026)

**Requested**: second of the two "improve directional edge" angles agreed the same session ("Lets check
both one by one") -- the first (`scripts/validated_setup_window_backtest.py`, previous entry) checks
whether live decisions correlate with the one validated entry signal; this one checks the exit side. The
31 Jul holdout test on that same entry signal, replayed as a rule-based strategy over 2-year archived
candles, found win rate was fine (52-59%) but the win/loss RATIO was not (0.53-0.68) -- average win ~6%
because the 8%/5% trail/target exits fired well before the wider fixed stop, average loss ~9-11% because
the stop rarely got there first. That finding is about a simulated rule-based-strategy replay, a
different exit engine from AI Origination's own (`STOPLOSS`/`TARGET`/`TRAIL_EXIT`/`STALL_EXIT`/`TIME_EXIT`,
see CLAUDE.md's "Exit paths (AI Origination)" table) -- so whether the same asymmetry actually shows up
in AI Origination's real trades is a genuinely separate, unanswered question, not something the holdout
result can be assumed to transfer.

**Built**: `scripts/exit_construction_check.py`. Reads every closed AI Origination trade (`origin LIKE
'AI_ORIGIN_%'`) and reports, overall and broken down by `exit_reason`, the same shape the holdout finding
used -- win rate, mean win % (wins only), mean |loss| % (losses only), and their ratio -- so it is
directly comparable to the 0.53-0.68 figure. A bootstrap 90% CI on `mean(|loss|) - mean(win)` tests
whether any asymmetry found is reliable rather than noise, same `MIN_BUCKET_LIVE=20` trust minimum and
resampling shape as every other backtest in this project. **Deliberately descriptive, not a candidate-
threshold sweep** -- no gate, stop/target/trail parameter, or exit-logic change is proposed or built here,
only a measurement of whether the asymmetry is present, which is the real input a genuine exit-
construction decision would need before touching `app/ai/originator.py`'s stop/target/trail constants.

7 new tests (`tests/test_exit_construction_check.py`): the population filter (AI Origination only, closed
only, excludes still-open trades), `exit_reason`/`result` read correctly, the win/loss-ratio computation
against a hand-computed example, the zero-entries case, the bootstrap helper, and `run_check`'s empty-
population and mixed-exit-reason smoke paths. Full suite: 581 passed (was 574). `python -c "import
app.main"` and `python -c "import scripts.exit_construction_check"` both import cleanly; `python -m
scripts.exit_construction_check --help` renders without error.

**Not run** -- same standing constraint as every backtest script in this project. Run on the machine with
real history:

```bash
python -m scripts.exit_construction_check --db data/trading.db
```

Read the per-exit-reason breakdown and the bootstrap CI before concluding anything. If `TARGET`/
`TRAIL_EXIT` mean wins are meaningfully smaller than `STOPLOSS` mean losses, with the CI excluding zero
and both sides at or above the trust minimum, that's real, sample-adequate evidence the same asymmetry
the holdout found is costing AI Origination too -- and the next step would be a genuine, backtested
proposal for widening the trail/target relative to the stop, not a unilateral parameter change. If the
CI crosses zero or the sample is thin, "not yet enough evidence" is the correct, expected outcome at
AI Origination's current history depth, same as every other live-history backtest in this project. **No
change to `app/ai/originator.py`'s exit construction has been made or proposed from this pass.**

### Does AI Origination's live trading exploit the one validated setup+window edge? Tooling built, not run (31 Aug 2026)

**Requested**: after a real losing day (Bank Nifty -₹2226, 0W-1L; Nifty +₹24, 1W-0L), asked "what should we
do to improve directional edge." Answered from this project's own backtest history rather than proposing
something new: almost every candidate entry filter tried (ADX, DI-direction, trend-age, break-confirmation,
reasoning-hedge) came back unsupported or inconclusive -- the one signal that has actually survived every
check (31 Jul walk-forward, both indices, 6 windows) is midday setups (EMA_STACK/ST_ALIGNED/ORB_BREAK/
PDH_PDL_BREAK) between 11:00-14:00 IST. The holdout test on exactly that signal found the entry timing
wasn't the problem (win rate 52-59%) -- the risk construction was (win/loss ratio 0.53-0.68, average win
~6% capped by trail/target exits against average loss ~9-11%). Offered two next steps: (a) check whether
AI Origination's live decisions actually correlate with that validated combination, since the model already
sees these setups in its prompt but nothing has ever measured whether it's using them; (b) revisit the
exit/risk construction itself, since that's where the project's own numbers say the edge is being spent.
User: "Lets check both one by one." This entry is (a).

**Built**: `scripts/validated_setup_window_backtest.py`. For every closed AI Origination trade, reads its
own logged `setups` (JSON list, already persisted per decision by `record_decision()`) and its own decision
timestamp, and classifies it "validated" if BOTH a direction-matched setup from the 31 Jul finding was
active (`EMA_STACK_UP`/`ST_ALIGNED_UP`/`ORB_BREAK_UP`/`PDH_BREAK` for `BUY_CE`;
`EMA_STACK_DOWN`/`ST_ALIGNED_DOWN`/`ORB_BREAK_DOWN`/`PDL_BREAK` for `BUY_PE` -- `PDH_BREAK`/`PDL_BREAK`
carry no `_UP`/`_DOWN` suffix, each is inherently one direction per `app/market_context.py`'s own naming)
AND the decision fell inside 11:00-14:00 IST. Reuses the `db_timestamp_to_ist()` shift and MFE/MAE-from-
ticks derivation every other real-history backtest in this project already uses (plain `sqlite3` reads a
`DateTime(timezone=True)` column back with no offset marker, so the +5:30 shift always applies; ticks are
used over `highest_price`/`lowest_price` since those columns are only reliably maintained since the 24 Aug
fix and this population spans both sides of it). Same `MIN_BUCKET_LIVE=20` trust minimum and bootstrap
90% CI shape as every other backtest tonight.

Unlike most `setup_significance`-style scripts in this project, this is **not** an index-direction-only
proxy -- it reads real closed trades with real premium P&L, since it only needs data already attached to
trades that opened, not a reconstruction of what a blocked decision would have traded.

14 new tests (`tests/test_validated_setup_window_backtest.py`): the timestamp shift, `_is_validated`'s
direction-matching (including the no-suffix `PDH_BREAK`/`PDL_BREAK` asymmetry and the window's inclusive-
start/exclusive-end boundary), `_load_entries` correctly marking validated vs not, MFE/MAE from ticks,
the population filter (excludes `NONE` decisions and still-`OPEN` trades), the bootstrap helper, and
`run_backtest`'s empty-population and mixed-population smoke paths. Full suite: 574 passed (was 560).
`python -c "import app.main"` and `python -c "import scripts.validated_setup_window_backtest"` both import
cleanly; `python -m scripts.validated_setup_window_backtest --help` renders without error.

**Not run** -- same standing constraint as every backtest script in this project: this sandbox's
`data/trading.db` has no schema. Run on the machine with real history:

```bash
python -m scripts.validated_setup_window_backtest --db data/trading.db
```

Read the bootstrap CI before concluding anything. A reliably-better "validated" bucket (CI excludes zero,
both sides at or above the trust minimum) means the model is actually getting real value from the one
signal this project has confirmed works; a reliably-worse or null result would mean the other signals it
also weighs (most already found not predictive on their own) are diluting it, or that a real trade's
entry timing doesn't line up with the setup+window combination as cleanly as the archived-candle backtest
did. Given AI Origination's real history is still only a couple of months deep, expect a thin sample on
the first run -- "not yet enough evidence" is the expected, correct outcome here, same as every other
live-history backtest in this project. **Angle (b) -- revisiting the exit/risk construction -- is the
next piece, not started in this pass.**

### "Today's activity" replaced with "Today's Highlights" -- AI Origination only, since strategies are no longer used (28 Aug 2026)

**Requested**: "Today's activity just shows bnv7 started and bnv closed like messages. Dont show that
instead show some interesting stats about today's trades for both strategies and ai generation trades."
Investigated before building: `get_today_activity()` was hardcoded to `origin == "SIGNAL"` -- it could
never have shown AI Origination activity even if asked to, which is exactly why it read as dead noise:
the user confirmed in the same conversation they've dropped Claude (`AI_ORIGIN_CLAUDE`) and stopped
using rule-based strategies entirely, running AI Origination/OpenAI only. Talked through several ideas
before building (exploratory, per this project's convention of not jumping straight to code): a
Claude-vs-OpenAI head-to-head (dropped once single-provider was confirmed), an AI-vs-strategies
comparison (dropped once strategies were confirmed unused), then converged on: a Bank Nifty vs Nifty
head-to-head, a decision funnel, the day's most notable decision with its own reasoning, and a
near-miss tracker -- all AI-Origination-scoped, all real comparisons already latent in the data rather
than invented metrics. User: "Ok lets build it but display it in interesting way not just boring text."

**Implementation**: `get_today_activity()` deleted outright (`app/platform.py`) -- confirmed via grep it
had exactly one caller (`_live_dashboard_data`) and its own dedicated tests, both updated/removed with
it, per this project's established clean-removal pattern. Replaced with
`get_ai_origination_today_highlights()`, scoped throughout to `origin LIKE 'AI_ORIGIN_%'` and today
(IST calendar day, filtered in Python via `to_ist()` after a 30-hour-lookback query -- same pattern as
every other "today" filter in this codebase, since SQLite's `date()` on a UTC column doesn't line up
with the IST calendar day). Four pieces, all zero-new-computation reads of data already written on
every origination cycle:

- **index_comparison**: today's closed AI Origination trades per enabled index -- trades/wins/losses/
  win rate/net P&L. Uses `net_pnl` (net of cost), not gross `profit_loss` -- same fix as the Trade
  History KPI entry directly below this one, applied here too rather than reintroducing the same
  gross-wearing-a-net-label mistake in a brand new function.
- **funnel**: how many real decision cycles ran today, how many declined (`NONE`), how many opened
  (`trade_id` set), how many wanted to trade but got blocked (`BUY_CE`/`BUY_PE` with `trade_id` still
  null -- confidence floor or a gate). `SLOT_OCCUPIED` marker rows are excluded from every count, same
  as every other analysis in this project treats them -- they're not real decisions.
- **sharpest_call**: today's best closed trade (highest `pnl_percent`) and its own `ai_reasoning`; if
  nothing has closed yet today, falls back to the single highest-confidence `NONE` decline instead, so
  there's always something to show once cycles have run rather than an empty card all morning.
- **near_misses**: up to 5 most recent `BUY_CE`/`BUY_PE` decisions today that never got a `trade_id`,
  newest first, with confidence and reasoning -- the "wanted to trade, didn't" population this session
  spent a lot of time reading directly from `ai_origination_logs` by hand.

**Display, per the explicit "not just boring text" ask**: new CSS (`app/static/dashboard.css`) rather
than reusing plain metric tiles -- two side-by-side index cards each with a horizontal bar sized
relative to the larger index's net P&L magnitude (an actual visual comparison, not just two numbers
next to each other); a connected funnel of chips with arrows between them (Cycles → Declined → Opened
→ Blocked), colored green/red on the outcome chips; a left-accent-bordered "highlight card" for the
sharpest call, colored by win/loss/neutral, with the reasoning rendered as an italicized blockquote
rather than a plain paragraph; and a row of near-miss pills (time · index · direction · confidence)
carrying the full reasoning in a hover tooltip rather than spelling it out inline. The old
`.activity-feed`/`.activity-strategy` CSS rules were dead once the markup using them was removed, so
they were deleted too rather than left orphaned.

8 new tests (`tests/test_today_highlights.py`): empty-day defaults, funnel counting with `SLOT_OCCUPIED`
correctly excluded, index comparison using `net_pnl` not `profit_loss` and correctly excluding
yesterday's trades/`SIGNAL`-origin trades/still-open trades, the sharpest-call trade-vs-decline-fallback
branches, near-miss ordering and the 5-item cap, and a dedicated same-day-filter test. `tests/test_
strike_display.py`'s two `get_today_activity`-specific tests removed with the function they tested (its
other two tests, unrelated to activity, are untouched). Full suite: 560 passed (was 555).
`python -c "import app.main"` imports cleanly.

**Verified live with a real browser**, not just the JSON payload -- seeded a full realistic scenario
into a scratch SQLite DB (a Bank Nifty win, a Nifty loss, a mix of NONE/opened/blocked decisions, one
`SLOT_OCCUPIED` row) and screenshotted the actual rendered `/` page via Playwright/Chromium (pre-installed
in this sandbox). Confirmed: the index comparison bars render proportionally correct (Bank Nifty's
green bar visibly wider than Nifty's red one, matching ₹820 vs ₹225), the funnel shows 5 → 2 → 2 → 1
with the right colors, the sharpest-call card shows the winning trade's real reasoning in a styled
quote block, and the near-miss row shows the one blocked decision as a pill. No JS console errors, no
layout breakage.

**Not verified against real live traffic** -- this sandbox has no real trading day to observe. After
deploying, confirm the dashboard's "Today's Highlights" section updates correctly on the existing 10s
poll as real decisions accumulate through a real session, and that the sharpest-call card correctly
flips from a "decline" card to a "trade" card the first time a real AI Origination trade closes that day.

### Trade History KPIs: "Net return" could read the opposite sign of "Net P&L" -- fixed to be capital-weighted, not a naive percent sum (28 Aug 2026)

**Reported**: a real Trade History screenshot showing "Net return" +2.93% (green) sitting directly next
to "Net P&L (₹), this selection" -829.75 (red) for the same 3-trade selection -- asked to find the
mistake. Traced to `compute_performance_kpis()` (`app/platform.py`, added 15 Aug when the standalone
Performance page was folded into Trade History): `net_return_percent` was a naive SUM of each trade's
own `pnl_percent`, not a capital-weighted aggregate return. Since trades can have very different
investment sizes, summing raw percentages has no reliable relationship to the summed rupee total -- a
small trade with a big % gain and a large trade with a modest % loss can sum to a POSITIVE percent
while the actual money lost. Reproduced exactly: a 500-rupee trade at +10% next to a 20,000-rupee trade
at -4% summed to +6.00% naively while the real rupee outcome was -750.00 -- the identical shape of bug
the screenshot showed.

**A second, related mislabeling found while reading the same function**: `net_pnl_amount` (and every
other rupee figure in this function -- the daily P&L chart, the equity curve) summed `trade.profit_loss`,
which per this file's own "Costs" convention is explicitly **gross**, not net. A KPI card literally
labelled "Net P&L" was showing gross P&L wearing that label.

**Fixed**: `net_return_percent`, the per-day `pnl_percent` in the daily-P&L chart, and `cumulative_percent`
in the equity curve are now all computed as `net_pnl / investment_amount * 100` (capital-weighted),
guaranteed to share the same sign as the corresponding rupee figure since `investment_amount` is always
non-negative. Every rupee figure in the function (`net_pnl_amount`, daily `pnl_amount`, equity curve's
`cumulative_amount`) switched from `trade.profit_loss` (gross) to `trade.net_pnl` (net of
`estimated_cost`), so a KPI labelled "Net" is now actually net throughout, not gross in some places and
net in others. Guards against zero invested capital (a day or a trade set with no capital deployed reads
0.0%, not a `ZeroDivisionError`). Route (`app/dashboard_routes.py`) and template (`history.html`) needed
no changes -- same key names (`net_return_percent`, `net_pnl_amount`, `cumulative_percent`,
`cumulative_amount`, `pnl_amount`), only their computation changed.

3 tests updated/added in `tests/test_history_settings_merge.py` (5 -> 8): the existing win/loss test now
asserts the capital-weighted return and that `net_pnl_amount` reads from `net_pnl` not `profit_loss`; a
new test reproduces the exact real bug shape (small high-% winner + large modest-% loser) and asserts
the fixed return percent shares the sign of the rupee total (previously would have read positive); a new
test confirms zero invested capital doesn't crash and reads 0.0% rather than raising. Full suite: 555
passed (was 553). `python -c "import app.main"` imports cleanly.

**Verified live**: started the app against a scratch SQLite DB, seeded the exact reproduction shape (a
500-rupee trade at net +50 and a 20,000-rupee trade at net -800), logged in, and confirmed `/history`
now renders "Net return" as -3.66% (red) directly alongside "Net P&L" as -₹750.00 (red) -- both correctly
negative and sign-consistent, where the pre-fix formula would have shown +6.00% (green) next to the same
-₹750.00 (red), reproducing the exact contradiction from the reported screenshot.

### Chop gate now requires ADX to ALSO read below trending before blocking, not chop alone (27 Aug 2026)

**Requested**: after the user enabled the chop gate (previous entry) and asked whether it could block
an entry during a market the dashboard reads as TRENDING -- answered yes, since the gate only checked
`chop_efficiency_ratio` and never looked at ADX/regime at all. User: "Lets make choppy signal enabled
with adx and trending market. Make a system which has precise market condition so it can take better
trading decisions." Asked via `AskUserQuestion` how ADX/chop should combine (block on both bad vs.
either bad vs. a new weighted composite) and whether this should also reach the model's own prompt.
User answered the first with a question back -- "What needed exactly to define the market except adx
and chop filters?" -- and the second with "Do what is best."

**Answered the question directly before building anything**: this project has already backtested most
of the plausible additional signals as standalone gates, and nearly all of them came back unsupported --
DI-direction agreement (25 Aug: 95.6% of trades already agree with their own direction, almost no room
to discriminate), trend duration / move extent (11/19/25 Aug: investigated three times, backtest came
back inconclusive, never shipped as a hard gate), break confirmation (12 Aug: not supported, both
indices), reasoning-hedge language (19 Aug: not supported at any category). CPR is a static once-a-day
number, not a live read. The pattern across this project: only three gates have ever cleared a real
backtest (DTE floor, same-direction-loss, confidence floor), and none of them are technical indicators
-- stacking more untested technical signals into one composite score would mean inventing a new
unvalidated number out of ingredients that mostly failed validation individually. Recommended against a
bigger composite and against touching the model's prompt (it already receives ADX and chop as separate
lines and is already told to weigh them together, per the chop-signal `SYSTEM_PROMPT` paragraph, 27
Aug) -- the model-facing side needed no change, only the block itself was ADX-blind.

**Implementation**: the gate in `_open_trade` (`app/ai/originator.py`) now requires **both**
`chop_efficiency_ratio < configured floor` **and** `market_context.adx < ADX_TRENDING` before blocking
-- chop alone no longer blocks, and a weak ADX alone no longer blocks. Reuses `ADX_TRENDING` (25),
already imported into this module and already the exact threshold shown on the dashboard's own
tradability read (`_classify_tradability`) -- no new threshold invented. This directly fixes the case
the user asked about: the real Bank Nifty card (ADX 26.5, chop 0.20) would NOT be blocked under the new
logic, since ADX alone still clears 25. Fails open the same way as before on any missing reading -- a
`None` `market_context`, `None` `chop_efficiency_ratio`, or now also `None` `adx` never blocks.

Settings > AI's Chop Gate tooltip/description rewritten to state the AND condition plainly, including
in the checkbox label itself, rather than leaving the UI describing the old chop-only behavior.

4 new tests (`tests/test_chop_gate.py`, 11 -> 15): the exact real Bank Nifty case (chop bad, ADX still
trending) is not blocked; a weak-ADX-but-clean-chop market is not blocked; ADX exactly at the trending
threshold does not count as below it (strict `<`, same convention as the chop floor); a missing ADX
reading fails open even when chop is bad. The existing 11 tests were updated in place -- the test
helper's `_make_context` previously hardcoded `adx=26.5` for every case (always >= `ADX_TRENDING`),
which would have silently broken every existing "blocks" assertion under the new AND logic; `adx` is
now a required, explicit parameter at every call site so each test states its intent for both signals
rather than inheriting a hidden default. Full suite: 553 passed (was 549). `python -c "import
app.main"` imports cleanly; `settings.html` verified to still parse and render the new AND wording live.

**Not backtest-validated, same standing status as the chop-only gate it replaces.** This is still a
manual risk decision, still off by default in the code but the user has since enabled it in production
-- the AND combination is a more conservative rule than chop alone (strictly fewer entries get blocked,
since both conditions must now hold rather than one), but it is still not something
`scripts/chop_gate_backtest.py` has validated; that script tests the chop signal alone (PART 1/2), not
this specific AND-with-ADX combination, and was not extended to do so in this pass. If this needs a
real validation pass once enough post-27-Aug history exists, the backtest would need its own new
candidate-floor check reconstructing this exact AND rule rather than reusing the existing chop-only
sweep as a proxy for it.

**Not verified live** -- this sandbox cannot run a real origination cycle. After deploying, confirm a
choppy-but-still-ADX-trending market (the exact scenario that prompted this) no longer produces a
`chop_efficiency_ratio=... AND adx=...` skip line in the logs, and that a genuinely neither-trending-
nor-clean market still does.

### AI Origination gets a chop gate -- admin opt-in, OFF by default, shipped without a backtest behind it (27 Aug 2026)

**Requested**: after walking through the dashboard's Market Conditions panel live (a Bank Nifty card
reading TRENDING on ADX 26.5 while its own efficiency ratio read CHOPPY 0.20 at the same moment --
explained as two different signals over two different windows, not a contradiction), asked what a
hard chop gate would do to a decision like that one. Answered: nothing changes about what the AI
decides -- a gate only vetoes whether a decision becomes a real trade, the same mechanism the
same-direction-loss gate already uses. Flagged that no chop gate exists yet and that
`scripts/chop_gate_backtest.py` (27 Aug) was deliberately not run, since `chop_efficiency_ratio` had
zero days of real closed-trade history at that point. User: "Lets build the gate." Flagged plainly,
before building anything, that this would be the first gate in this project ever shipped without a
backtest behind it -- every other real gate (DTE floor, same-direction-loss, confidence floor) was
validated first, and every rejected candidate (ADX, reasoning-hedge, break-confirmation) was rejected
specifically because its backtest didn't support it. Asked via `AskUserQuestion` how to proceed; user
chose: **ship it now, admin-configurable, OFF by default** -- exists in code, doesn't change any live
behavior until an admin explicitly opts in via Settings > AI.

**Implementation**: two new `AISettings` columns (`app/db_models.py`, additive `_ensure_columns()`
migration), both defaulting to today's actual behavior so deploying this changes nothing on its own --
`ai_origination_chop_gate_enabled` (bool, default `False`) and
`ai_origination_chop_gate_min_efficiency_ratio` (float, default `0.3`, matching the existing CHOPPY
threshold already shown on the dashboard and in the model's own prompt via `_efficiency_ratio_text`,
not a separately-chosen number). Two new helper functions in `app/ai/originator.py`
(`_chop_gate_enabled`/`_chop_gate_min_efficiency_ratio`) mirror `_max_same_direction_losses`/
`_max_sl_percent`'s exact shape. The gate itself sits in `_open_trade`, checked immediately after the
same-direction-loss gate -- same early position (before any contract-resolution cost), since it only
needs `market_context`, already in hand. Blocks when `chop_efficiency_ratio < configured floor`
**strictly** (a reading exactly at the floor is not blocked).

**Fails open on missing data, deliberately**: a `None` `market_context` or a `None`
`chop_efficiency_ratio` (an index with under an hour of 5-min bar history, or the field simply not yet
populated) never blocks -- "no reading yet" is not "choppy," the same missing-value convention this
project applies everywhere else (see CLAUDE.md's own "new nullable columns mean not recorded" rule).
This mirrors the same-direction-loss gate's own `market_context=None` handling exactly.

Settings > AI gets a new "Chop Gate" section: a checkbox (default unchecked) and a "Min Efficiency
Ratio" number input (default 0.3, validated `0.0 <= value <= 1.0` in `/ai-settings` POST), with an
explicit tooltip/description stating this ships without backtest validation and pointing at
`scripts/chop_gate_backtest.py` for when enough history exists to check. `/ai-settings`'s form and
values dict both extended to match the same pattern every other AI Origination risk knob already
uses.

18 new tests (`tests/test_chop_gate.py`): the two settings-fallback helpers (defaults without a
settings row, reads an admin-configured value), and `_open_trade` integration -- disabled-by-default
ignores a choppy reading, enabled blocks below the floor (and short-circuits before contract
resolution, matching the loss gate's own test pattern), enabled allows at-or-above the floor, the
exact boundary (reading == floor is not blocked), enabled-but-no-`market_context` fails open,
enabled-but-missing-`chop_efficiency_ratio` fails open, and an admin-configured custom floor (0.5)
blocking a reading (0.40) that would clear the default. Full suite: 549 passed (was 538).
`python -c "import app.main"` imports cleanly. Migration verified against a simulated pre-migration
DB (built the full current schema, dropped both new columns via `ALTER TABLE ... DROP COLUMN`, ran
`_ensure_columns()` for real, confirmed both columns reappear, confirmed a second run is a clean
no-op).

**Verified live**: started the app against a scratch SQLite DB, logged in, confirmed `/settings?tab=ai`
renders the new section with the correct default (unchecked, 0.3), saved with the gate enabled and a
custom floor (0.45) and confirmed both values round-trip correctly on re-render, and confirmed the
`0.0-1.0` validation rejects an out-of-range floor (1.5) with a 400.

**Not backtest-validated -- stated explicitly in the Settings UI itself, not just here.** This is a
manual risk decision the user made knowingly, not a finding this project has confirmed. Per this
project's own standing discipline (unbroken until now), a floor only ships as *validated* once
`scripts/chop_gate_backtest.py` clears the same bootstrap-CI bar every other gate has been held to --
that has not happened, and won't be possible until real closed-trade history with a
`chop_efficiency_ratio` reading accumulates (the field is one day old as of this entry). If enabled in
production, re-run the backtest once enough history exists and revisit whether the 0.3 default (or
whatever an admin has set it to) is actually the right floor:

```bash
python -m scripts.chop_gate_backtest --db data/trading.db
```

### Loss-gate override backtest -- tooling built, not run; index-direction-only proxy by design (27 Aug 2026)

**Trigger**: same-day follow-up to the chop-signal dashboard work. 27 Aug: the same-direction
consecutive-loss gate (17 Aug, `_same_direction_consecutive_losses` in `app/ai/originator.py`)
correctly blocked `BUY_PE` on both indices after 2 consecutive losses. Walked through real data with
the user showing that hours later, both indices' `chop_efficiency_ratio` had climbed from CHOPPY
(`<0.3`) into CLEAN (`>=0.5`) and confidence/sub-scores were reading high 70s%, genuinely different
conditions from the losing entries -- yet the gate kept blocking every `BUY_PE` decision regardless,
since it only counts a loss streak, blind to whether the setup that produced it still resembles the
current one. User pushed back with "If i traded now i would have got profits" (hindsight, not
evidence) -- explained the gate's real tradeoff and offered a backtest instead of a unilateral
judgment call either way. User: "Yes, build that backtest."

**Question asked**: does the gate ever block a decision where conditions have genuinely diverged
from the triggering losses, and if so, does that divergence predict a better outcome than the
similar-conditions case -- i.e. is a conditions-aware override worth building, or does relaxing the
gate just let the same failure back in wearing a better-looking prompt.

**Real, load-bearing limitation, stated up front rather than glossed over**: a gate-blocked decision
never opens a trade, so there is no real premium P&L to read -- unlike every other gate backtest in
this project (ADX, freshness, hedge), which analyze real closed trades. `_open_trade`'s gates
(including this one) are checked *before* contract/strike resolution, so a blocked decision has no
resolved contract to look up archived option premium for without independently re-deriving strike
selection -- meaningfully more machinery than this pass. Built as an index-direction-only proxy
instead, from the live 1-minute `Candle` archive (populated continuously by AI Origination's own
candle refresh) as a stand-in for "would the thesis have been directionally right" over a 60-minute
horizon. This project has repeatedly found index continuation is not premium continuation (see the
STALL_EXIT entry below, 6 Aug) -- every number this backtest produces is evidence about the
directional thesis only, never a confirmed trading outcome. A real premium-reconstruction version is
a larger follow-up, not built here.

**Built**: `scripts/loss_gate_override_backtest.py`. Identifies "blocked by this gate" decisions
precisely: `ai_origination_logs` rows with `decision IN ('BUY_CE','BUY_PE')`, `trade_id IS NULL`
(never opened), confidence already clearing the 0.60 floor (checked in `run_origination_checks`
*before* `_open_trade` is ever called -- a floor-fail is a different, unrelated block and must not be
folded in here), AND reconstructing `_same_direction_consecutive_losses`'s own logic as of that exact
historical decision's timestamp (not "now") shows the streak was already at or above the configured
threshold (`AISettings.ai_origination_max_same_direction_losses`, default 2). A `BUY_CE`/`BUY_PE`
non-opener that doesn't reconstruct this way (a DTE floor with no future expiry, a live order
failure) is reported separately as "unexplained," never silently folded in as gate-caused. Reuses
`db_timestamp_to_ist()`'s exact shift logic from `stall_exit_backtest.py` (plain `sqlite3` reads a
`DateTime(timezone=True)` column back with no offset marker at all -- the wall-clock numbers are
always the UTC value regardless of what `fromisoformat` parses out), duplicated per this project's
per-script convention. The reconstruction adds one explicit guard the live function doesn't need: an
`entry_time < decision_raw_timestamp` filter, since replaying the past (unlike the live function,
which only ever runs in real time) needs an explicit look-ahead barrier against trades that hadn't
happened yet at the moment being reconstructed.

For each gate-blocked decision, compares its own `chop_efficiency_ratio`/`confidence` against the
mean of the *same losing trades* that produced the streak blocking it (read from their own
`ai_origination_logs` row via `trade_id`) -- what the gate is protecting against, in the model's own
terms, at the time it failed. "Diverged" if chop reads at least 0.15 higher or confidence at least
0.10 higher than that mean -- explicit starting points, not validated, same status every new
threshold in this project carries before a backtest looks at it. Losses that predate
`chop_efficiency_ratio`'s own existence (27 Aug) have no chop reading to compare against by
construction, not a gap in this script. Reports two buckets (diverged vs. similar) with win-direction
rate and mean forward return, plus a bootstrap 90% CI on the difference, same `MIN_BUCKET_LIVE=20`
trust minimum and bootstrap-resampling shape as every other gate backtest in this project.

20 new tests (`tests/test_loss_gate_override_backtest.py`): the timestamp shift, loss-streak
reconstruction (stops at first win, ignores trades that hadn't happened yet via the explicit
look-ahead guard, excludes yesterday's trades), the admin-setting fallback, the mean-readings helper
ignoring missing values, forward-return computation from real candle rows (None when a candle is
missing, never fabricated), the `diverged` classification's four cases (chop alone, confidence alone,
neither, missing readings), the end-to-end decision loader (confidence-floor decisions excluded,
"unexplained" reported separately, a real reconstructed streak+forward-return+diverged flag), the
bootstrap helper, and `run_backtest`'s empty-population/unexplained-count/mixed-population paths.
Full suite: 538 passed (was 518). `python -c "import app.main"` and
`python -c "import scripts.loss_gate_override_backtest"` both import cleanly;
`python -m scripts.loss_gate_override_backtest --help` renders without error.

**Not run** -- same standing constraint as every backtest script in this project: this sandbox's
`data/trading.db` is a 0-byte file with no schema, confirmed again this session
(`sqlite3.OperationalError`-equivalent: the script's own startup check reports "No
ai_origination_logs table found" and exits cleanly rather than crashing). Run on the machine with
real history:

```bash
python -m scripts.loss_gate_override_backtest --db data/trading.db
```

Read the bootstrap CI before concluding anything. Per this project's own standard: a reliably-better
diverged bucket (CI excludes zero on the positive side, both buckets at or above the trust minimum)
is real support for building a conditions-aware override; a reliably-worse or inconclusive result
means the gate's current blind streak-count is not costing anything worth trading away it own
simplicity for. Given the gate only started producing chop-comparable blocked decisions from 27 Aug
onward (`chop_efficiency_ratio` didn't exist before that), expect a thin sample on the first run --
"not yet enough evidence" is the expected, correct outcome here, not a failure of the tooling.
**No change to `app/ai/originator.py`'s gate has been made or proposed from this pass.**

### Market Conditions panel extended with chop + confidence sub-scores (27 Aug 2026)

**Requested**: a single place to watch the two things just built (the efficiency-ratio chop signal
and the four confidence sub-scores) live, without grepping logs or writing SQL. Asked first whether
this should be a new standalone page or an extension of the existing Market Conditions panel on `/`
-- chosen: extend the panel, consistent with this project's repeated pattern of consolidating
AI-related views into the main dashboard rather than adding separate pages (AI Origination page
removed 15 Aug, AI Settings merged into Settings, Performance merged into Trade History, Active
Trade tab removed -- all folded in for the same "one place to look" reason).

**Implementation**: `get_market_conditions()` (`app/platform.py`) already reads the latest
`AIOriginationLog` row per index for regime/ADX/CPR/setups -- extended to read the same row's
`chop_efficiency_ratio`, `confidence`, `setup_quality`, `entry_quality`, `risk_quality`,
`market_alignment` too. Zero new computation, zero new SmartAPI calls, same as the function's own
existing docstring already promises -- this is strictly more columns off a row already being read.
New `_classify_chop()` mirrors `_classify_tradability()`'s exact shape (same three-band pattern,
same duplicated-not-imported reasoning: `app/ai/originator.py` already imports FROM
`app/platform.py`, so the reverse import for `_efficiency_ratio_text` would be circular).

All five new fields are `None` on a `SLOT_OCCUPIED` marker row (the "Market Conditions panel
froze" fix's own synthetic decision) -- correct, not a gap: that marker carries real context/chop
data (built every cycle regardless of slot occupancy) but no real model decision, so confidence and
the sub-scores genuinely don't exist for it. The template (`live_dashboard.html`) omits both new
lines entirely when null, same omit-don't-fabricate convention as everywhere else in this project,
rather than showing a `--` that could be mistaken for a real reading.

`get_market_conditions()`/`_live_dashboard_data()` already power both the initial page render and
the existing 10s `/api/live-dashboard` poll, so this needed no new route, no new poll cycle, and no
frontend wiring beyond the two new lines in `renderConditions()`.

7 new tests (`tests/test_market_conditions.py`, extended in place): `_classify_chop`'s band
boundaries, the five new fields read correctly from a real-shaped row, all five correctly `None`
on a `SLOT_OCCUPIED` row while `chop_efficiency_ratio` itself still reads through, and the
no-log-yet placeholder case. Full suite: 518 passed (was 514).

**Verified live**, not just unit-tested: started the app against a scratch SQLite DB, seeded a real
`AIOriginationLog` row with the full new-field shape (`chop_efficiency_ratio=0.22, confidence=0.78,
setup_quality=82.0`, etc.), logged in, and confirmed `/api/live-dashboard` returns exactly those
values with `chop_label` correctly classified as `"CHOPPY"` (0.22 < 0.3), and that `/` itself
renders with a 200 (no Jinja error) rather than just trusting the unit tests. No browser available
in this sandbox, so the JS `renderConditions()` client-side rendering itself is not directly
screenshotted -- the JSON payload it consumes is confirmed correct, which is what would need to be
wrong for the rendered cards to be wrong.

### Live chop signal added (efficiency ratio) -- prompt + logging + backtest tooling, no gate (27 Aug 2026)

**Trigger**: a user watching a live index chart reported the market as "clearly choppy and not
tradable" while AI Origination kept taking same-direction BUY_PE entries anyway, each one citing
ADX above 25 / Supertrend / EMA-stack alignment as support. Traced to a real gap rather than a
bug: `app/indicators.py`'s `adx()` docstring says outright that ADX is deliberately lagging --
*"ADX typically crosses 20 well after a move is underway... a filter against trading in chop, not
an entry trigger."* Supertrend and the EMA stack are the same shape -- direction-only, no notion
of how noisy the path was. CPR is the only existing chop-adjacent signal in this codebase, and
it's a static, once-per-session prior computed from **yesterday's** range, not a live read of
today's actual path. None of the four signals the model has (ADX/Supertrend/EMA/CPR) can tell a
clean move from one that has gone choppy in just the last hour -- they all answer "which direction
and has that held," never "how cleanly is price actually getting there right now."

**Built**: `compute_efficiency_ratio()` (`app/market_context.py`) -- Kaufman's Efficiency Ratio
over the most recent ~1 hour (12 bars) of 5-min closes: net displacement divided by total
bar-to-bar path length. 1.0 = a dead-straight move, near 0 = as much back-and-forth as net
progress. Pure arithmetic on `bars_5m`, already fetched every cycle -- zero new SmartAPI cost,
same justification CPR's own docstring already gives for itself. Deliberately a SHORT window,
distinct from `trend_duration_pct_of_session` (which can span the whole session) -- this answers
a different question: is the *last hour* clean, not how long has the overall bias held.

New `MarketContext.chop_efficiency_ratio` field, threaded through `build_market_context()`,
`as_dict()`, the TREND AGE prompt section (`_build_user_prompt`, new `_efficiency_ratio_text()`
helper: `<0.3` choppy, `0.3-0.5` mixed, `>=0.5` clean -- a reasonable starting point, not
validated, same status `CPR_NARROW_MAX_PERCENT`/`CPR_WIDE_MIN_PERCENT` had before any backtest
looked at them), and a new `SYSTEM_PROMPT` paragraph telling the model to weigh it alongside ADX
specifically because ADX/Supertrend/EMA are lagging and can still read "trending" after the last
hour has gone choppy. Persisted on `AIOriginationLog.chop_efficiency_ratio` (own additive
`_ensure_columns()` entry, verified against a simulated pre-migration DB the same way the
confidence sub-scores' migration was) -- **descriptive only, does not gate or size anything**,
same status as `trend_duration_pct_of_session`/`move_extent_atr` when those were first added.

**Backtest tooling built, matching `adx_gate_backtest.py`'s exact PART 1/2 shape**: new
`scripts/chop_gate_backtest.py` buckets real closed AI Origination trades by
`chop_efficiency_ratio` (choppy/mixed/clean, the same bands the prompt itself shows the model),
reports win rate/mean P&L/mean MFE/mean MAE per bucket plus a bootstrap 90% CI on two candidate
hard floors (block `<0.3`, block `<0.5`) -- same `MIN_BUCKET_LIVE=20` trust minimum and
bootstrap-resampling shape as every other gate backtest in this project, duplicated rather than
imported per the established per-script convention. **No gate is shipped or proposed from this
pass** -- the field was only added today, so real history to backtest against does not exist yet
by construction; this is the tooling that will answer the question once it does, not a preview of
the answer. A 2-year index-level fallback (mirroring `adx_gate_backtest.py`'s PART 4) was
explicitly not built this pass -- it would need `compute_efficiency_ratio` threaded into
`scripts/backtest/data.py`'s shared `IndexArrays`, a larger change than this pass's scope; worth
a follow-up if the real-trade sample turns out too thin once there's history to look at.

21 new tests across `tests/test_efficiency_ratio.py` (formula correctness: straight move = 1.0,
pure back-and-forth with zero net progress = 0.0 exactly -- distinct from all-flat-bars which
correctly returns `None`, "no data" rather than a fabricated zero -- insufficient bars, a
hand-computed mixed case, and confirmation that only the recent window is considered, not the
whole bar history), `tests/test_market_context_efficiency_wiring.py` (the live `build_market_
context()` path reaches the same value the isolated formula does), `tests/test_chop_efficiency_
prompt.py` (bucket-label boundaries, the prompt line's omit-when-`None` convention, the new
`SYSTEM_PROMPT` paragraph, confirmation neighboring paragraphs survived), and `tests/test_chop_
gate_backtest.py` (population filter, MFE/MAE from ticks matching a real 27 Aug trade's own
figures, the bootstrap helper, and a full `run_chop_buckets()` smoke run). Plus one assertion
added to `tests/test_origination_log.py`'s existing trend-age persistence test. Full suite: 514
passed (was 493). Migration verified against a simulated pre-migration DB (built the full current
schema, dropped the new column via `ALTER TABLE ... DROP COLUMN`, ran `_ensure_columns()` for
real, confirmed the column reappears, confirmed a second run is a clean no-op).
`python -c "import app.main"` imports cleanly; `python -m scripts.chop_gate_backtest --help`
renders without error.

**Not verified live** -- this sandbox cannot call either provider's real API, so there's no way to
observe whether the model actually uses the new prompt paragraph the way the resolution-requirement
paragraphs do (weighing it, not just echoing the number). After deploying: spot-check a few real
`ai_origination_logs` rows for a populated `chop_efficiency_ratio` that varies independently of
`adx`/`trend_duration_pct_of_session` (a value that's always near-identical to what ADX would imply
would mean the model isn't getting new information out of it), and once enough real history exists
under trades opened after this deploy, run:

```bash
python -m scripts.chop_gate_backtest --db data/trading.db
```

Per the same standard as every other candidate gate in this project: a floor only ships if PART 2's
bootstrap CI excludes zero on both sides at or above the trust minimum -- "not yet enough evidence"
is the expected, correct outcome for a field with less than a day of history behind it.

### Four confidence sub-scores added to the AI Origination schema -- instrumentation only, no gating change (26 Aug 2026)

**Discussion, not an incident.** A design proposal argued against trusting the LLM's own `confidence`
number as a calibrated probability, and recommended decomposing it into independent sub-scores
(setup/entry/risk/market-alignment) plus a future calibration curve mapping raw scores to observed
outcome rates. Checked against what this project has already found rather than accepted at face value:
confidence is *already* not treated as a probability here -- its only consumer is
`_clears_confidence_floor()`, a single backtested pass/fail gate (0.60, from `confidence_sizing_
backtest.py`'s 185-trade run: `<0.60` reliably worse, `0.60+` flat with no further gradient -- `0.60-
0.75` is in fact the best-performing bucket, not `0.85+`). Position size is fixed at 1 lot regardless of
confidence; nothing else reads it. So the proposal's core warning was already the operating assumption,
just arrived at empirically. Its calibration-curve architecture is sound long-term direction but needs
real trade volume this project doesn't have yet (~200 trades total historically, `MIN_BUCKET_LIVE=20`
already strains for a single confidence-bucket split) -- building the calibration model now would fit a
curve to noise, the same trap `freshness_resolution_check.py`'s first real run just found (see the entry
below). Agreed scope: add the four sub-scores to the schema and persist them now, so a real calibration
attempt has raw material once enough history exists; leave `confidence`'s own gating untouched.

**Implementation**: `SYSTEM_PROMPT` (`app/ai/originator.py`) now asks for `setup_quality`,
`entry_quality`, `risk_quality`, `market_alignment` (each 0-100, independent of each other and of
confidence -- the prompt explicitly warns against restating confidence four times under different
names) alongside the existing fields, with one sentence per field describing what it means against
data the model actually sees (REGIME/STRUCTURE/TREND/EXTENSION sections from `_build_user_prompt`) and
an explicit statement that these do not currently gate or size anything. `_Decision` gained the four
fields as trailing optional attributes (after `latency_ms`, matching that field's own reason for being
last: several call sites still construct `_Decision` positionally on `ERROR`, e.g.
`_Decision("ERROR", None, None, None, response.error, response.latency_ms)`, and must stay valid without
knowing these fields exist). `_parse_response` parses and clamps each to `[0, 100]` the same permissive,
fail-to-`None` way `sl_percent`/`target_percent` already are -- a missing or malformed sub-score is "not
provided," never a synthetic 0, and never fails the whole parse.

Persisted on both existing decision-record paths: `StrategyTrade` gained `ai_setup_quality`/
`ai_entry_quality`/`ai_risk_quality`/`ai_market_alignment` (populated in `_open_trade`, mirroring
`ai_confidence`/`ai_reasoning` exactly), and `AIOriginationLog` gained `setup_quality`/`entry_quality`/
`risk_quality`/`market_alignment` (populated in `record_decision()` via `getattr(decision, ..., None)` --
defensive on purpose, so a caller still constructing an older decision-shaped object doesn't raise).
Both additive `_ensure_columns()` migrations (`app/database.py`) -- `ai_origination_logs` gets its own
new migration block; it never had one before despite several fields being added to the class over
multiple dated commits (`trend_duration_pct_of_session`, `concurrent_correlated_entry`, etc.), which
this pass does not retroactively fix (out of scope, and the real production table already has those
columns by some other path, confirmed by every real query run against it this session -- worth noting,
not investigating further here).

11 new tests: `tests/test_confidence_sub_scores.py` (new -- parsing including clamping/missing/invalid
sub-scores, the prompt schema and calibration-paragraph survival, `_open_trade` persisting all four
scores and a regression case confirming a decision with none of them still opens a trade normally with
the columns left `NULL`) and `tests/test_origination_log.py` (extended -- `record_decision` persists the
four scores when present, and defaults them to `None` without raising when handed an older decision
object that predates the fields). Full suite: 493 passed (was 482).

**Migration verified against a simulated pre-migration DB** (not just a fresh schema, since this table
class has a history of fields added without a corresponding `_ensure_columns()` entry, per the note
above): built the full current schema, dropped the 8 new columns via `ALTER TABLE ... DROP COLUMN` to
reproduce an already-deployed old-shape DB, ran `_ensure_columns()` for real, confirmed all 8 columns
appear, and confirmed a second run is a clean no-op. `python -c "import app.main"` imports cleanly.

**Not verified live** -- this sandbox cannot call either provider's real API, so there's no way to
observe whether OpenAI/Claude actually populate these four fields usefully (versus, say, just echoing
confidence four times despite being told not to) from here. After deploying: spot-check a few real
`ai_origination_logs` rows for non-null `setup_quality`/`entry_quality`/`risk_quality`/`market_alignment`
values that visibly differ from each other and from `confidence`, and watch Claude's `stop_reason`
specifically -- its cap was raised 256 -> 2048 on 5 Aug for exactly this class of risk (a longer expected
response outrunning the token budget), but four new numeric fields is still more output than before, so
worth confirming `stop_reason == "max_tokens"` hasn't reappeared rather than assuming the old fix still
has headroom. No calibration analysis is possible yet and none is planned until real history
accumulates -- this is instrumentation only, same standing status as `option_chain.py`'s archive.

### freshness_resolution_check.py's first real run found a detector bug, not 38 violations -- fixed, real result is clean so far (26 Aug 2026)

**Run for real, same day, hours after the tooling above shipped:**

```
Total decisions with reasoning: 47 (since 2026-08-26 06:02:42)
Decisions using freshness/newness language: 46 (97.9% of all decisions)
FLAGGED (freshness language + trend_duration_pct_of_session >= 70 or move_extent_atr >= 5.0): 38
```

Alarming at first read -- until the 38 printed rows were actually read, not just counted. **Every single
one was `decision=NONE`**, and every single one's reasoning used the flagged language NEGATED: *"there
is no fresh breakout: price is still inside the opening range and the move has already run the whole
session and 9.94 ATR, which makes continuation risky."* `_mentions_freshness` is a bare substring match
on `"fresh"` with no negation awareness, so `"no fresh breakout"` (correctly declining) and `"a fresh
confirmed break"` (the actual 26 Aug trigger-trade shape, wrongly trading) matched identically. The
check was flagging the self-consistency prompt working exactly as intended -- BankNifty repeatedly and
correctly declining because trend_duration_pct_of_session sat near 100% all session, in the model's own
words -- as if it were 38 violations of the instruction it was following correctly.

**Fixed**: a decision can only be a genuine violation if it actually opened a trade -- a NONE decision
definitionally cannot "trade on a contradicted fresh framing" if nothing was traded. `run_check` now
filters to `decision IN ('BUY_CE', 'BUY_PE')` before the freshness/context checks, added as an explicit
first condition rather than a smarter negation parser: real data gives zero evidence a BUY decision ever
uses negated freshness language about its own thesis, so building negation-detection NLP against a
pattern that hasn't been observed would be exactly the speculative-build this project avoids elsewhere.
`run_outcome_backtest()` was never affected -- it already only reads `strategy_trades` rows, which by
construction only ever contains trades that opened.

2 new tests (`tests/test_freshness_resolution_check.py`, extended in place): the exact real NONE-decline
shape (negated "no fresh breakout" + `trend_duration_pct_of_session=100.0`) is no longer flagged; NONE
and ERROR decisions are excluded from the trade count while a genuine BUY_CE violation in the same batch
is still caught. Full suite: 482 passed (was 480).

**Re-run against the same real data, same fix, same day:**

```
Total decisions with reasoning: 47 (since 2026-08-26 06:02:42)
Of those, decisions that opened a trade (BUY_CE/BUY_PE): 3
Trade decisions using freshness/newness language: 0
FLAGGED (opened a trade + freshness language + ... in the same context): 0
```

**Zero flagged violations, on the corrected detector, in the only window that has existed since PR #59
deployed.** All 3 real trades opened since deploy are clean by this check. This is a genuinely good
early signal, not a confirmed result -- n=3 is far below any trust minimum this project uses anywhere,
and the 44 correctly-declined BankNifty NONE cycles printed above (same trend stuck near 100% all
session, same correct decline every 5 minutes for hours) says more about one persistent BankNifty regime
than about general model behavior. Re-run `python -m scripts.freshness_resolution_check --db
data/trading.db --since "2026-08-26 06:02:42"` after real trade volume accumulates before treating this
as anything more than "no violations found yet."

### freshness_resolution_check.py extended with a real outcome backtest -- tooling only, not run (26 Aug 2026)

**Trigger**: a task document claimed "the gate confirmed deployed, still not firing" against two real
26 Aug trades (100.0% trend duration / 5.99 and 7.37 ATR, both "fresh"-framed, one loss one win) --
investigated and corrected (see this file's own repeated notes on this exact false premise): no hard
`trend_duration_pct_of_session`/`move_extent_atr` gate has ever existed in this codebase, in any commit,
ever (`git log -S`, repo-wide grep, both empty). Only PR #59's advisory `SYSTEM_PROMPT` paragraph and the
`freshness_resolution_check.py` diagnostic script are real. The follow-up request, correctly reframed:
stop asking whether a gate is "firing" and instead run the diagnostic tooling that already exists,
with the same bootstrap-CI discipline as every other backtest in this project, and report a real go/no-go
on whether a hard gate is now supported by evidence -- not build one from three anecdotes.

**A concrete, verifiable correction surfaced while checking this**: PR #59 merged to `main` at
`2026-08-26 11:32:42 +0530` (`2026-08-26 06:02:42` UTC), confirmed from the merge commit itself
(`git show --no-patch --format="%H %ai %s" 6d48e16`). Both of the request's own trigger trades (10:30 AM
and 11:25 AM IST) predate that merge -- the second by only 7 minutes. Deployment here is manual and
separate from a merge (this file's own ground rules), so the real production deploy can only be at or
after this timestamp, never before. **Neither trigger trade could have run against the new prompt
paragraph at all** -- they aren't evidence the advisory language failed, they're evidence of nothing
about it either way, since the code they'd need to have been influenced by did not exist yet when they
opened. Any `--since` filter evaluating this prompt's real effect must use `2026-08-26 06:02:42` (UTC)
as a hard floor, and later once the actual manual production-deploy timestamp is confirmed.

**Built**: `freshness_resolution_check.py`'s existing decision-level audit (`run_check` -- flags a
candidate violation from `ai_origination_logs`, unchanged) is now paired with a new
`run_outcome_backtest()`, reading CLOSED `AI_ORIGIN_%` trades directly from `strategy_trades`
(`ai_reasoning`/`market_context_json` -- the same `MarketContext.as_dict()` shape
`ai_origination_logs.context_json` already uses, since both are written from the same `market_context`
object at entry time) rather than the decision-level table, since only opened trades have a real
`pnl_percent` to compare. Same two-part flag test as the existing audit (freshness language present AND
`trend_duration_pct_of_session >= 70` or `move_extent_atr >= 5.0` in the same context), bucketed
flagged vs not-flagged: win rate, mean P&L, mean MAE (from `strategy_trade_ticks`, not the stored
`lowest_price` column -- same documented reason `reasoning_hedge_backtest.py`'s `_load_entries` already
gives: `lowest_price` is pinned at entry price for this always-long population), and a bootstrap 90% CI
on the mean P&L difference. Same `MIN_BUCKET_LIVE=20` trust minimum and the same bootstrap-resampling
shape as `reasoning_hedge_backtest.py`'s `_bootstrap_mean_diff`, duplicated rather than imported per
this project's established per-script convention. `main()` now runs both sections back to back.

9 new tests (`tests/test_freshness_resolution_check.py`, extended in place): the flag applied correctly
to the trigger-trade shape, MAE derived from ticks matching the real trigger trade's own -13.09% MAE,
the population filter excluding non-`AI_ORIGIN_%` origins and still-`OPEN` trades, the `--since` filter,
the bootstrap helper on a fully-separated synthetic gap (deterministic: constant-valued groups collapse
to an exact CI with zero variance) and an overlapping-groups null case, and three `run_outcome_backtest`
integration cases (a reliable-but-thin synthetic difference correctly flagged both "reliably WORSE" and
"below trust minimum" in the same line, the no-closed-trades case, and the one-empty-bucket case). Full
suite: 480 passed (was 471). `python -c "import scripts.freshness_resolution_check"` imports cleanly;
`--help` renders without error.

**Not run** -- same standing constraint as every backtest script in this project: this sandbox has no
real `data/trading.db` (`data/` here holds only an unrelated `trades.csv`). Run on the machine with real
history, using the confirmed deploy floor above:

```bash
python -m scripts.freshness_resolution_check --db data/trading.db --since "2026-08-26 06:02:42"
```

Read `FRESHNESS-FLAGGED OUTCOME BACKTEST`'s reported bucket sizes first -- given PR #59 merged only
today, the post-floor population is very likely thin (`[BELOW MIN SAMPLE]`/`[below trust minimum]`) at
first read; per this project's own standard, that is the expected, reportable "not yet enough evidence"
outcome, not a failure of the check. **No gate has been proposed or built from this pass.** If the CI
ever excludes zero on a sample that clears the trust minimum, that is the trigger to bring a *specific*
numeric gate proposal back for review as its own follow-up -- not to build one from this backtest run
directly, per the discipline every other gate in this project (DTE floor, same-direction-loss,
confidence floor) was held to before shipping.

### Market Conditions panel froze per-index whenever every configured provider held an open trade there (26 Aug 2026)

**Reported**: a screenshot showing Bank Nifty's Market Conditions snapshot "4m ago" (fresh) next to
Nifty 50's "59m ago" (stale), with exactly one open Nifty 50 AI Origination trade visible (OpenAI,
long put). No fix requested yet, just "Nifty50 is not updated for more than hour."

**Root cause, confirmed by reading, not guessed**: `run_origination_checks` (`app/ai/originator.py`)
had an index-level early exit --

```python
if all(_has_open_origination(session, index.symbol, provider_name) for _, provider_name, _ in provider_order):
    continue
```

-- added purely to save the spot-price fetch when no configured provider had a free slot. `_has_
open_origination` is checked per-provider (`app/ai/originator.py`'s own comment: "each provider gets
its own independent trade per index"), but with `secondary_enabled=False` (a single-provider config,
matching the screenshot's one OpenAI trade), `provider_order` has exactly one entry -- so `all(...)`
over a one-element list is trivially true the moment that one provider has an open trade. The `continue`
fires before `_load_market_context`, before the `[AI][ORIGIN][CTX]` log line, and critically before
`record_decision()` -- which only runs inside the per-provider loop further down, never reached. `get_
market_conditions()` (`app/platform.py`) reads the *latest* `AIOriginationLog` row per index, written
exclusively by `record_decision()` -- so once a provider's only slot filled, that index's dashboard
snapshot stopped getting new rows entirely and froze at whatever it last wrote, for as long as the
trade stayed open. Bank Nifty had no open trade blocking any slot, so it kept cycling and logging
normally -- exactly the fresh-vs-stale contrast reported.

**Fixed**, per the chosen option (asked via AskUserQuestion: keep building context every cycle,
skip only the trade decision): the index-level early exit is removed -- price/`_load_market_context`/
the CTX log line now run every cycle regardless of slot occupancy, same cost as before this
optimization existed. The per-provider loop still skips a provider whose slot is occupied (unchanged,
`_has_open_origination` check per provider, zero LLM calls saved before were still saved). New: if
every provider in `provider_order` was skipped this way (`provider_evaluated` stays `False`), a single
context-only marker row is now written via `record_decision()` with a synthetic `_Decision(action=
"SLOT_OCCUPIED", confidence=None, reasoning="")` -- this is what keeps the dashboard panel updating.
`confidence=None` and `reasoning=""` are deliberate: every existing backtest/check script in `scripts/`
filters on one or the other (`confidence IS NOT NULL`, or `reasoning IS NOT NULL AND reasoning != ''`),
so this marker is automatically excluded from every population those scripts analyze -- it was never a
real model decision and must never be counted as one. `AIOriginationLog.decision` is a plain
`String(16)` with no enum constraint; nothing else in `app/` reads that column outside `originator.py`
itself and `get_market_conditions()` (which never inspects the `decision` value, only regime/adx/cpr/
setups), confirmed by grep before shipping this.

2 new tests (`tests/test_stale_market_conditions_gate.py`): a full `run_origination_checks` cycle with
a single-provider config and an open trade on the only slot confirms a `SLOT_OCCUPIED` marker row is
written with real regime/adx data and the LLM is never called (an exploding stand-in for `_call_
provider` would fail the test if it were); a control case with no open trade confirms the provider is
still called normally and a real decision is recorded, unaffected by this change. Full suite: 471
passed (was 469). `python -c "import app.main"` imports cleanly.

**Not verified live** -- this sandbox cannot run a real origination cycle end-to-end. After deploying,
confirm the Market Conditions panel keeps updating (a fresh `last_updated` on every ~5-min cycle) for
an index with an open AI Origination trade on its only configured provider slot, and spot-check
`ai_origination_logs` for `decision='SLOT_OCCUPIED', confidence IS NULL, reasoning=''` rows appearing
during that window:

```sql
SELECT timestamp, index_name, decision, confidence, regime, adx
FROM ai_origination_logs WHERE decision = 'SLOT_OCCUPIED' ORDER BY timestamp DESC LIMIT 10;
```

### Confidence-scoring instruction gets a resolution requirement for self-consistency (26 Aug 2026)

**Trigger**: a real trade (Nifty PE, confidence 0.89) resolved a self-stated exhaustion risk with
*"the fresh confirmed break and continued negative drift make the bearish continuation case the
clearest setup"* -- but the same logged context showed `trend_duration_pct_of_session = 100.0`. The
trend had already consumed the entire session; "fresh" was directly contradicted by data already in
the same prompt the model reasoned from. MFE -1.20% -- straight to stop, never moved favorably. This
is a sharper version of the 12/14/19 Aug pattern (state a risk, dismiss it with a bare "but") the 19-20
Aug hedge-resolution fix already targets: the model now produces resolution-*shaped* language, but
nothing checks whether the resolution is actually *true* against fields already in its own context.

**A third instance of the same false premise, checked and rejected before anything else.** The
request's Section 4 claimed "the emergency trend-extension gate shipped after 19 Aug" blocks entries at
`trend_duration_pct_of_session >= 95 OR move_extent_atr >= 10`, and asked to verify it's deployed. It
still isn't real. `git log -S"trend_duration_pct_of_session >="` across the **entire history** of
`app/ai/originator.py` returns nothing -- not "not deployed," never written, in any commit, ever. The
code comment at that exact spot (line ~222) states the real, deliberate decision: this was investigated
11 Aug, `trend_age_gate_backtest.py` was built for it, the result was inconclusive (not "not supported,"
genuinely insufficient data), and it was deliberately not shipped rather than gated on a single day's
anecdote -- this project's own repeatedly-stated standard. Flagged plainly rather than built around,
same as the two earlier instances of this exact claim (documented in the "Reasoning-hedge detector"
entry below).

**Did not ship the hard gate anyway, despite a second real anecdote now existing.** This trade is a
second data point consistent with the same 11 Aug pattern, which is worth knowing -- but shipping a
hard `trend_duration_pct_of_session`/`move_extent_atr` gate from two anecdotes is exactly the
overfitting error this project has repeatedly guarded against, and the existing backtest for this
specific question came back inconclusive rather than supportive. Re-running
`scripts/trend_age_gate_backtest.py` against current (larger) history is the right next step if this
gate is worth revisiting -- not done here, since that's a distinct decision from the prompt fix below
and wasn't asked for as clearly.

**What was actually built: the self-consistency prompt paragraph (the request's Section 1), which
needs no backtest** -- same discipline as every other prompt-only change this cycle (confidence scale,
hedge resolution): ship, then verify with a before/after distribution check over real elapsed time, not
a pre-deployment gate. New `SYSTEM_PROMPT` paragraph in `app/ai/originator.py`, inserted directly after
the existing hedge-resolution paragraph and before the confidence-calibration one (same grouping the 19
Aug entry established): tells the model not to call a move "fresh" or "newly confirmed" when trend
duration is roughly 70-80%+ of the session, and not to call a breakout "new" when the cumulative move is
already several ATR -- phrased against the exact human-readable labels the model actually sees in its
own prompt ("~X% of session elapsed", "Cumulative move since trend start: Y ATR"), not the internal
snake_case field names, since the model never sees those literally. Same NONE-not-downgraded escape
hatch as the original resolution requirement.

7 new tests (`tests/test_hedge_resolution_prompt.py`, extended in place): the new paragraph's presence,
the 70-80% and "several ATR" language, and that it forces NONE the same way the original resolution
requirement does. New `scripts/freshness_resolution_check.py` (the request's Section 3/deliverable 4):
flags any `ai_origination_logs` decision whose reasoning uses freshness language ("fresh", "newly
confirm", "just confirm", "new breakout") while its own logged `context_json` shows
`trend_duration_pct_of_session >= 70` or `move_extent_atr >= 5.99` (`FRESHNESS_ATR_FLOOR`, taken from
this trigger trade's own reading as a starting point, explicitly not a validated threshold -- this is a
diagnostic flag for manual review, not a gate). 8 new tests
(`tests/test_freshness_resolution_check.py`) including the exact trigger-trade shape reproduced from
its real reasoning text and context values. Full suite: 469 passed (was 461).
`python -c "import app.main"` and `python -c "import scripts.freshness_resolution_check"` both import
cleanly.

**Sandbox note**: mid-session, this sandbox's installed packages were reset (pytest, SQLAlchemy,
FastAPI etc. all gone, confirmed via `pip list`) while the filesystem and git state were unaffected --
reinstalled from `requirements.txt` before continuing. Not a code or data issue, just an environment
hiccup worth naming in case it recurs.

**Not verified live** -- this sandbox cannot call either provider's real API. After deploying, log the
exact deployment timestamp, then run the check script now for the baseline and again after 1-2 weeks:

```bash
python -m scripts.freshness_resolution_check --db data/trading.db
python -m scripts.freshness_resolution_check --db data/trading.db --since "<deploy timestamp, UTC>"
```

If flagged decisions drop to near zero after the change, the prompt fix is working. If they don't,
that's a real result too -- per the same standard the hedge-resolution fix itself established, it
would mean the model can't reliably self-check a resolution against its own numeric context even when
explicitly told to, which is worth knowing regardless of whether the underlying hard-gate question
(re-running `trend_age_gate_backtest.py`) is ever revisited.

### AI Origination trailing-stop activation percent made admin-configurable (25 Aug 2026)

**Reported**: a real trade (Nifty PE, entry 102.55, high 111.9, MFE 9.12%) never armed its trailing
stop. Traced with a real DB query against `strategy_trades.trail_activate_percent`: the stored value
was **11.59%**, not the 8% the hardcoded `_TRAIL_ACTIVATION_NOMINAL` constant in `app/ai/originator.py`
implies -- the same CE/PE `symmetric_premium_percent()` rescale already covered for stop-loss also
widens the trailing-activation nominal for puts (8.0 * ~1.449 = 11.59, matching to two decimals and
independently confirming the rescale factor inferred from the same day's stop-loss investigation
above). The trade's high of 111.9 was genuinely short of the 114.44 needed to arm -- not a bug, working
exactly as designed, and confirmed identically on both of that day's Nifty PE trades.

**Asked directly what "fix it" should mean** (AskUserQuestion) since there are several different
plausible changes with real behavior implications -- remove the CE/PE widening for trail activation
specifically, lower the nominal in code, or make the nominal admin-configurable while keeping the
rescale. Chosen: admin-configurable, same pattern as `ai_origination_max_sl_percent`.

**Implementation**: new `AISettings.ai_origination_max_sl_percent`-shaped column,
`ai_origination_trail_activate_percent` (float, default 8.0 -- matches the value this used to be
hardcoded to, so deploying changes nothing until an admin edits it), additive `_ensure_columns()`
migration. New `_trail_activate_nominal(db)` helper mirrors `_max_sl_percent(db)` exactly, falling
back to the original `_TRAIL_ACTIVATION_NOMINAL` constant only if no `AISettings` row exists.
`_open_trade`'s `trail_activate, _ = symmetric_premium_percent(...)` call now reads this admin value
as its input instead of the hardcoded constant -- **the rescale itself is untouched**: a put still
needs a wider move than a call for the same activation, only the nominal fed into that rescale is now
tunable from Settings > AI instead of a code constant. `trail_width_percent` (the 5% trail-back
distance once armed) is deliberately left alone -- the question was specifically about why trailing
never *activated*, not about the trail's width once it has.

Settings > AI gets a new "Trail Activation % (nominal)" field next to Max Stop-Loss %/Max
Same-Direction Losses, validated `0.5 <= value <= 50`; `/ai-settings` POST and its values dict updated
to match.

4 new tests (`tests/test_same_direction_loss_gate.py`): the fallback-without-settings-row case, the
admin-configured-value read, and two `_open_trade` integration tests (admin value flows through to
`trade.trail_activate_percent`, and the original hardcoded default is used when no `AISettings` row
exists) -- both isolate the nominal-selection logic from the rescale math via an identity-rescale
monkeypatch, the same technique the stop-loss clamp tests already established. Full suite: 458 passed
(was 454). `python -c "import app.main"` imports cleanly; `settings.html` verified to still parse.

**Not verified live** -- this sandbox cannot place a real trade to observe the new setting take effect
end to end. After deploying, confirm Settings > AI renders the new field with the correct default
(8.0), confirm editing it and re-running the same `trail_activate_percent`/`trail_width_percent` SQL
query from the diagnostic above shows the new nominal (rescaled for puts) on the next AI Origination
trade opened after the change.

### AI Origination now included in every periodic report, not just Daily (25 Aug 2026)

**Requested**: "Daily report should include ai origination trades in daily analysis as well." Before
this, `generate_daily_summary()` (`app/reports.py`) only queried `origin == "SIGNAL"` trades (via
`_closed_trades_between`) -- AI Origination's own numbers only ever appeared in the separate, on-demand
"AI Origination Summary" report type (`ReportType.ORIGINATION`, 15 Aug), never inside the daily digest.

**Implementation, scoped to Daily only** (weekly/monthly untouched, per the request's own wording):
`generate_daily_summary` now also runs `_origination_trades_between()` (already existed, reused rather
than duplicated) and nests its output under a new `stats["origination_stats"]` key -- **not** merged
into the same population as the SIGNAL trade stats. Kept structurally separate for the same reason
CLAUDE.md's "origin field is the isolation mechanism" section has stated repeatedly: AI Origination
trades have no `StrategyConfig`-backed strategy name, and merging them into `by_strategy` would either
crash or misrepresent them. Nested under `origination_stats` specifically (not `trade_stats`, which
`_template_narrative`'s own dispatch already reserves for routing to the Pattern Discovery template) so
the existing flat daily-report structure -- and every existing reader of `ReportType.DAILY`'s
`stats_json` -- stays unaffected; this is a pure addition, not a restructuring.

Two consumers updated to actually surface the new data, not just carry it as inert JSON:

- **The OpenAI-narrative prompt** (`_generate_narrative`) gained one added sentence asking the model to
  summarize `origination_stats` as its own point when present, separate from the strategy numbers --
  the model already saw the whole `stats` dict as raw JSON before this, but had no instruction to
  actually mention it.
- **The template-fallback narrative** (`_template_narrative`, used when no AI provider is configured)
  gained a new AI Origination paragraph. Required restructuring the existing early-return: the old code
  returned immediately after "No closed trades were recorded" if the SIGNAL population was empty --
  which would have silently dropped any AI Origination summary on a day with AI trades but zero SIGNAL
  trades. Fixed so both sections are always evaluated independently.

7 new tests (`tests/test_daily_report_origination.py`): `origination_stats` populated correctly
alongside SIGNAL stats, the zero-origination-trades case, the exact zero-SIGNAL-but-real-origination
regression the early-return fix targets, the template narrative mentioning origination when populated
and when empty, confirming weekly/monthly-style stats dicts (no `origination_stats` key) are a true
no-op, and confirming `origination_stats`'s own inner `by_provider` key doesn't leak into the top-level
dispatch check and misroute the daily narrative into the Origination report's own template (which would
have printed the wrong report title). Full suite: 449 passed (was 442). `python -c "import app.main"`
imports cleanly; no template (`reports.html`) changes needed -- it already renders `stats` as a raw
JSON dump alongside the narrative text, confirmed by reading `app/dashboard_routes.py`'s `reports_page`.

**Not verified live** -- this sandbox cannot generate a report against real trade history end to end
through the UI. After deploying, generate a Daily Summary (Reports page "Generate Now", or wait for the
scheduled job) on a day with at least one closed AI Origination trade and confirm the narrative text
mentions it and the raw stats JSON includes a populated `origination_stats` block.

**Extended same day: "Make ai origination as part of every summary."** `generate_weekly_report` and
`generate_monthly_report` gained the identical `origination_stats` addition (same
`_origination_trades_between`/`_origination_trade_stats` calls, just over each function's own
`start, end` window). `generate_pattern_discovery` gained it too, nested alongside its existing
`trade_stats`/`ai_correlation`/`time_patterns` keys rather than disturbing that structure.

The shared paragraph-building logic was pulled into one new `_origination_narrative_lines()` helper
so Daily/Weekly/Monthly's default template branch and Pattern Discovery's own `_template_pattern_
narrative` render the AI Origination section identically rather than maintaining two near-duplicate
copies. Building this shared helper surfaced the exact same early-return bug in `_template_pattern_
narrative` that the original Daily fix addressed -- it also returned immediately on zero SIGNAL trades,
which would have dropped any AI Origination mention from a Pattern Discovery report over a lookback
window with AI trades but no SIGNAL trades. Fixed the same way, in the same pass.

`ReportType.ORIGINATION` (the dedicated on-demand AI Origination Summary) is unchanged and still
exists separately -- this addition means AI Origination numbers now also surface inside every other
report type, not that the dedicated report is redundant; it still has provider-vs-provider detail
(`by_provider`, `avg_confidence`) the periodic summaries only surface as `best_provider`.

5 new tests (`tests/test_daily_report_origination.py`, same file -- extended in place rather than
renamed, since the underlying feature and its risks are the same across all four report types):
Weekly and Monthly both populate `origination_stats` correctly; Pattern Discovery populates it while
leaving its existing `trade_stats` nested structure intact; the `_template_pattern_narrative`
early-return fix specifically (zero SIGNAL trades, one AI Origination trade, both must appear); and
Pattern Discovery's narrative confirmed unaffected when `origination_stats` is absent entirely. Full
suite: 454 passed (was 449). `python -c "import app.main"` imports cleanly.

**Not verified live**, same constraint as above -- after deploying, generate a Weekly Report, a
Monthly Report, and a Pattern Discovery report (each with at least one closed AI Origination trade in
their respective windows) and confirm all three now surface it, the same check already described for
Daily.

### AI Origination Max Stop-Loss % now caps the REALIZED loss, not just the AI's nominal input (25 Aug 2026)

**Reported**: a real trade lost -18.81% despite `AISettings.ai_origination_max_sl_percent` set to 12.0.
Traced with the real CSV export row: stored `SL %` was **17.39**, not 12 -- the admin ceiling is
checked in `_open_trade`'s `_stop_is_sane()` against `decision.sl_percent`, the AI's *raw, pre-rescale*
proposed number. `symmetric_premium_percent()` (`app/premium_model.py`) then widens that number for PE
contracts (puts are more index-sensitive than calls, so an unadjusted percentage would stop a put on a
smaller move -- the rescale exists specifically to equalize that), and its own docstring gives the
almost-exact match: *"A 12% call stop becomes an ~18% put stop -- same bet, honestly labelled."* A
further ~1.4pp came from ordinary 30-second monitor-tick execution slippage on top, the same pattern
already diagnosed in the NV1 1-DTE-floor entry above. Neither piece was a bug -- both are documented,
intentional behavior -- but they meant the admin-facing "Max Stop-Loss %" label didn't do what it says
for puts specifically. Asked directly whether to leave this as-is or make the setting cap the real loss;
chosen: make it real.

**Fixed**: `_open_trade` now clamps `sl_percent` down to `max_sl_percent` *after* the CE/PE rescale,
not just checking the pre-rescale nominal value beforehand (the existing `_stop_is_sane` FIXED-vs-
TRAILING decision is untouched -- that's still evaluating whether the AI's own nominal risk judgment is
trustworthy, a different question from what ceiling to enforce on the number that actually gets used).
The clamp only ever tightens `sl_percent`, never widens it, so an already-compliant call's stop cannot
be affected, and it applies uniformly whether the trade ends up on the AI's own FIXED numbers or the
TRAILING fallback's initial stop (`_TRAILING_INITIAL_SL_PERCENT`, which gets the same rescale and can
therefore also need the same clamp). `target_percent` is deliberately untouched -- the admin's stop
ceiling was never meant to cap upside, and the existing target/stop sanity split (17 Aug) already
established that principle.

Settings > AI's tooltip for this field updated to say what it now actually does (caps the realized
loss, enforced after the CE/PE adjustment) rather than the old, technically-narrower "caps the AI's
proposed stop-loss" wording that read as covering more than it did.

4 new tests (`tests/test_same_direction_loss_gate.py`): a rescaled PE stop is clamped back to the
admin ceiling (mirrors the real 12%->17.4% production case via a fake rescale, since this sandbox has
no fitted coefficients file to exercise the real rescale); the target is unaffected by the same clamp;
a stop that rescales to something still under the ceiling is left alone (the clamp never widens); and
the TRAILING fallback's own initial stop gets the same clamp. Full suite: 442 passed (was 438).
`python -c "import app.main"` imports cleanly; `settings.html` verified to still parse.

**Not verified live** -- this sandbox cannot place a real trade to observe the clamp firing end to end.
After deploying, confirm on the next PE trade that its stored/exported `SL %` never exceeds the
configured `ai_origination_max_sl_percent`, even when the AI's own reasoning or the CSV export shows a
wider *nominal* number would otherwise have applied.

### ADX gate backtest extended with a 2-year index-level fallback -- tooling only, not run (25 Aug 2026)

**Requested**: "I want it to be run on last 2 years data," after the entry directly below settled the
ADX gate question against real AI Origination history (NOT SUPPORTED). Real AI Origination history
cannot itself become 2 years deep -- the feature has existed a couple of months, ~45 closed trades as
of this run -- so this is a different data source, not a bigger pull of the same one.

**Built**: `scripts/adx_gate_backtest.py`'s new PART 4, following the exact precedent
`break_confirmation_backtest.py`'s PART 2 and `trend_age_gate_backtest.py` already established for
this class of question -- the 2-year index-candle archive (`scripts/backtest/`), asking a related but
not identical question: among bars where an already-registered setup (`default_setups()`) fires, does
forward index-direction edge differ between `ADX < floor` and `ADX >= floor`, at a 60-minute horizon.
Index-direction-only, same limitation every `setup_significance`-style script in this project already
carries -- no real trades, no real premium P&L, no confidence score. `IndexArrays.adx14`
(`scripts/backtest/data.py`) was already computed for the whole archive; only the threshold sweep is
new, reusing `_evaluate`'s session-block-bootstrap shape (duplicated per this project's own per-script
convention, not shared).

`--skip-live-history` added so PART 4 can run alone against a candle-only environment.

4 new tests (`tests/test_adx_gate_backtest.py`): `_eligible_index` correctly excludes bars before
ATR/EMA/ADX have warmed up and bars outside the 09:45-15:15 window even once warm, `_edge_index`
matches a hand-computed value and returns 0 for an empty population. Also smoke-tested the full PART 4
CLI path against ~120 sessions of synthetic candles for both indices (2 years of real data would be
too large to construct in a sandbox) -- ran clean, correctly reported every setup/floor/bucket
combination with `-` verdicts (expected: synthetic random-walk price data carries no real embedded
edge). Full suite: 438 passed (was 434). `python -c "import scripts.adx_gate_backtest"` imports
cleanly.

**Not run against real 2-year data** -- same standing constraint as `break_confirmation_backtest.py`'s
own PART 2 and every other `scripts/backtest/`-based script in this project: no real candle archive in
this sandbox. Run on the machine with the real archive:

```bash
python -m scripts.adx_gate_backtest --db data/trading.db
python -m scripts.adx_gate_backtest --db data/trading.db --skip-live-history   # PART 4 alone, faster
```

Per this project's own stated standard (`setup_significance.py`'s docstring, already quoted elsewhere
in this file): a `(setup, floor)` cell is worth trusting only if `below` is reliably worse than
`at_or_above` on **both** indices, not a single-index result with a CI that happens to exclude zero.
No gate is added to `app/ai/originator.py` by this pass -- reported here per the same discipline as
every other candidate gate in this project.

**Run for real, same day -- NOT SUPPORTED, the full parameter surface.** Every registered setup, both
floors, both indices, `HORIZON_BARS=12` (60 min). Scanned every `(setup, floor)` row-pair for the
project's own bar: `below`'s bootstrap CI fully separated from `at_or_above`'s, in the direction the
hypothesis predicts. **None exist.** Every row's `below`/`at_or_above` confidence intervals overlap on
both indices -- not one setup/floor combination clears even a single-index "reliably worse" reading,
let alone replicates on both.

Two `BACKWARDS` verdicts appear (`EMA_RSI_CROSS[entry_offset=0]` and `EXTENDED_FADE[atr_mult=2.0]`,
both `<20`, both Bank Nifty only) -- Bank Nifty's `below` bucket edge is reliably negative on their
own. Checked against Nifty for the same setups: `EMA_RSI_CROSS[entry_offset=0] <20` reverses direction
entirely (Nifty's `at_or_above` bucket is the worse one, not `below`); `EXTENDED_FADE[atr_mult=2.0]
<20` is directionally the same sign on Nifty (-3.18pp) but not reliably so (CI `[-8.21, +1.93]`
crosses zero) and has no reported `at_or_above` bucket to compare against at all (too few signals).
Neither replicates cleanly enough to count as real, per this project's own standard.

A separate, unrelated observation worth naming: Nifty shows broad positive edge across several
trend/breakout setups (`ORB_BREAK`, `PDH_PDL_BREAK`, `ST_ALIGNED`, `EMA_STACK`) regardless of which
ADX bucket a bar falls in -- both `below` and `at_or_above` often read `POSITIVE` on the same setup.
That is a fact about Nifty's setups carrying edge generally (consistent with this project's own
earlier walk-forward findings, which already found Nifty setups more consistently positive than Bank
Nifty's), not evidence that ADX is what discriminates it -- if ADX were the discriminator, `below`
would read differently from `at_or_above`, and on Nifty it usually doesn't.

**Final verdict: no ADX gate, at any granularity tested this cycle** -- real AI Origination history
(PARTS 1-3, ~45 trades) and the full 2-year index-level archive (PART 4, every registered setup, both
floors, both indices) agree. `app/ai/originator.py`'s three hard gates (DTE floor, same-direction
consecutive-loss gate, 0.60 confidence floor) remain unchanged. This closes out the ADX-gate
investigation that started with the 25 Aug trigger trade -- worth reopening only if a materially
different mechanism (the still-unbuildable ADX slope, once logged and observed) or a much larger real
trade sample changes the picture.

### ADX hard-gate backtest tooling built -- NOT wired into originator.py yet, pending real data (25 Aug 2026)

**Trigger (25 Aug, one trade, not evidence on its own)**: Nifty 50 `BUY_PE`, AI Origination/OpenAI,
confidence 0.66. The dashboard's own tradability read showed ADX 19.6 -- "Not tradable" -- at
decision time. Asked directly why the trade fired despite that: nothing in `_open_trade`
(`app/ai/originator.py`) gates on ADX at all. `_classify_tradability`'s TRENDING/MARGINAL/
NOT_TRADABLE read (`app/platform.py`, added 10 Aug) is explicitly informational-only, shown on the
dashboard, never consulted by the trading path -- confirmed by the template's own comment: *"Nothing
in the trading path reads this."* The only three real hard gates today are the DTE floor, the
same-direction consecutive-loss gate, and the 0.60 confidence floor. This trade cleared all three,
so it fired despite a caution the model itself named and the dashboard itself flagged.

**Not a new finding -- restates a gap CLAUDE.md's own 11 Aug entry already left open.** "Trend-age
caution moved to a hard gate" deliberately did NOT hard-gate `trend_duration_pct_of_session` (closely
related to ADX -- both describe "is there a real trend"), because the incident review that prompted
it gave a sweep range to validate (80/90/95%) rather than a committed number, and picking one from a
single day's anecdote was judged exactly the overfitting error this project guards against elsewhere.
Same standard applies here, so the same discipline: backtest before shipping, per every other gate
this project has added (DTE floor, same-direction consecutive-loss gate, confidence floor, the
declined break-confirmation and reasoning-hedge gates).

**Built**: `scripts/adx_gate_backtest.py`, same shape as `break_confirmation_backtest.py`/
`confidence_sizing_backtest.py` (`ai_origination_logs JOIN strategy_trades`, MFE/MAE from
`strategy_trade_ticks` -- not `highest_price`/`lowest_price`, since this population spans trades from
both before and after the 24 Aug `lowest_price` fix, so ticks are the one source correct across the
whole history). Buckets on `_classify_tradability`'s own bands (`ADX_NO_TREND=20`,
`ADX_TRENDING=25`, imported as literal values matching `app/market_context.py`, not reinvented):
`<20`, `20-25`, `>=25`. A trade with no recorded ADX (predates the field) is reported in its own
bucket, excluded from every comparison rather than defaulted to a side. Sweeps two candidate hard
floors (block `<20`, block `<25`) via the same floor-bootstrap shape that validated the 0.60
confidence floor -- 90% CI on `mean_pnl(blocked) - mean_pnl(kept)`, flagged when either side is below
`MIN_BUCKET_LIVE=20`.

6 new tests (`tests/test_adx_gate_backtest.py`): ADX read correctly off the joined
`ai_origination_logs` row, a missing ADX doesn't exclude the trade (just excludes it from bucket/floor
comparisons), MFE/MAE derived from ticks matches the established pattern, the population filter
excludes `SIGNAL` trades (no joinable log row) and still-`OPEN` trades, and the bootstrap helper
detects both a real synthetic gap and a null case. Also smoke-tested the full CLI path against 60
synthetic trades (thin buckets, the no-ADX bucket, per-index breakdown, both floor comparisons) to
catch formatting bugs the unit tests wouldn't -- ran clean. Full suite: 428 passed (was 422).
`python -c "import app.main"` and `python -c "import scripts.adx_gate_backtest"` both import cleanly.

**No gate added to `app/ai/originator.py` in this pass -- deliberately.** Per the same standard this
project has applied to every other candidate gate: a floor ships only if it clears the trust minimum
with a bootstrap CI that excludes zero. This sandbox has no real `data/trading.db`, so there is no
result to validate against yet (confirmed again: `sqlite3.OperationalError: no such table:
strategy_trades`, the same standing constraint every other backtest script in this project hits
here). Run on the machine with real history:

```bash
python -m scripts.adx_gate_backtest --db data/trading.db
```

Read PART 2 (the candidate-floor bootstrap) for the actual go/no-go signal. If either floor (20 or
25) shows a reliably-worse blocked population with both sides at or above the 20-observation trust
minimum, that floor is validated and should be wired into `_open_trade` as a real hard gate, the same
place the DTE/same-direction/confidence gates already live -- report back and I'll build that next.
If neither clears the bar, or the sample is too thin (likely at the current history size, per every
other live-history backtest in this project), that's the correct, reportable answer too: "no ADX gate
yet" is not a failure to find one, it's what the standing discipline requires until the data says
otherwise.

**Run for real, same day -- NOT SUPPORTED, on two separate grounds.**

- **The `<20` floor is untestable from closed history: n=0.** Zero closed AI Origination trades have
  ever had ADX below 20 at decision time. The 25 Aug trigger trade itself (the one that prompted this
  whole investigation, ADX 19.6) was still `OPEN` when this ran -- it is not yet in this population,
  and would be the very first observation in this bucket once it closes. This is itself a real,
  useful fact: entries below ADX 20 are apparently rare enough in practice that a `<20` gate may have
  had little to act on regardless -- worth re-running once the trigger trade (and any others like it)
  closes and the bucket actually has data.
- **The `<25` floor's point estimate runs backwards from the hypothesis.** The would-be-blocked
  `20-25` band (n=6, below the trust minimum) had mean P&L **+6.05%**, actually *better* than the
  kept `>=25` band's **-1.59%** (n=38, clears the minimum). Bootstrap 90% CI on the difference is
  `[-1.67, +17.26]` -- crosses zero, so not reliable either way, but the direction offers no support
  for blocking the `20-25` band. If anything, on these numbers `>=25` ("Trending" per the dashboard's
  own label) is the weaker-performing bucket, not the stronger one.

**Verdict: no ADX gate ships.** Neither candidate floor clears this project's own bar (a bootstrap CI
that excludes zero, both sides at or above the trust minimum). `app/ai/originator.py` remains
unchanged -- the DTE floor, same-direction consecutive-loss gate, and 0.60 confidence floor stay the
only three hard gates. Worth revisiting specifically once the `<20` bucket has real closed-trade data
to look at; until then this is a genuinely unanswered question, not a settled "ADX doesn't matter."

**PART 3 added same day: DI-direction agreement, prompted by an external ADX-gate design document.**
The doc proposed a fuller entry gate -- ADX above a floor, `+DI`/`-DI` direction agreeing with the
trade's own direction, ADX sloping upward, and an EMA/VWAP breakout trigger. Scoped down before
building anything: confirmed via `AskUserQuestion` this stays a gate layered in front of AI
Origination's existing LLM decisions (not a new standalone rule-based strategy), and entry-side only
(no `>40` exhaustion exit, which would touch the shared-FIXED-branch exit logic this file already
flags as high-risk to change).

Of the doc's four legs, only DI-direction is newly testable from history: `app/indicators.py`'s
`adx()` already returns `plus_di`/`minus_di` alongside ADX, and `market_context.py` already carries
both into `MarketContext.as_dict()` -- exactly what `ai_origination_logs.context_json` stores
verbatim for every past decision, so no new logging was needed to backtest it. **ADX slope is NOT
testable** -- nothing has ever recorded ADX's trend over time, only a single snapshot per decision;
it would need a new logged field and a real observation window first, the same path
`trend_duration_pct_of_session` and `same_direction_entries_today` both took before either was
gated. The **EMA/VWAP breakout trigger is not re-tested** -- VWAP has no index-instrument data in
this codebase (index candles report zero volume, the same wall BNV5.1/BNV6 hit), and the
EMA-alignment alternative is what `break_confirmation_backtest.py` already tested via
EMA_STACK/ORB/PDH/PDL setups on 12 Aug (verdict NOT SUPPORTED) -- re-testing it here would just
duplicate that result.

`scripts/adx_gate_backtest.py` extended with `_di_agrees()` (parses `context_json`, `None` when
either DI value is missing rather than defaulted to a side) and a new PART 3: DI-agrees vs
DI-disagrees bootstrap, plus a combined-gate check (`ADX >= 20 AND DI agrees`) against everything
else. 6 new tests (`tests/test_adx_gate_backtest.py`): DI values read correctly from `context_json`,
missing DI handled without crashing, `_di_agrees`'s BUY_CE/BUY_PE logic and its `None` case, and the
full report function running clean on a realistic mixed population (some trades with DI, some
without). Also re-smoke-tested the full CLI against synthetic data including DI values -- ran clean.
Full suite: 434 passed (was 428). `python -c "import app.main"` and
`python -c "import scripts.adx_gate_backtest"` both import cleanly.

**Not run against real data yet.** Same standing constraint as every other backtest script here.
Run on the machine with real history:

```bash
python -m scripts.adx_gate_backtest --db data/trading.db
```

Read PART 3 alongside PART 2's already-negative floor result -- if DI-agreement clears the trust
minimum with a CI excluding zero (in either direction), that's real evidence either for or against
that specific leg of the doc's proposed gate, independent of the floor question. ADX slope stays
unbuildable until it's logged going forward; if the doc's design is worth pursuing further, adding
that field to `AIOriginationLog` (pure instrumentation, no trading-path change, same shape as every
other field in that table) would be the next concrete step -- not done in this pass.

**Re-run for real, same day -- STILL NOT SUPPORTED, all three checks.** The `<20` bucket now has its
first real observation: the 25 Aug trigger trade itself closed at **-18.81%**, the single worst
result in the whole dataset. Still `n=1`, still below the trust minimum, still not something a
bootstrap comparison can run against 44 other trades ("too few observations on one side").

- **`<25` floor**: now `n=7` blocked (the original 6-trade `20-25` band plus the new `<20` loss),
  mean P&L +2.50%, versus the kept `>=25` band's -1.59% (`n=38`). Bootstrap 90% CI `[-5.59, +14.27]`
  -- still crosses zero, still no reliable difference, even with the single worst trade in the
  dataset now inside the blocked bucket.
- **PART 3, DI-direction**: `agrees` `n=43` (mean -1.47%), `disagrees` `n=2` (mean +9.98%, both
  trust-minimum-thin). CI `[-2.21, +25.07]` crosses zero. **The real, useful finding here isn't the
  CI -- it's the population split itself: 43 of 45 trades (95.6%) already had `+DI`/`-DI` agreeing
  with the model's own chosen direction.** DI almost never disagrees with what AI Origination
  decides to trade, which means this leg has very little room to discriminate outcomes at all --
  not "DI doesn't matter," but "the model's direction and DI direction are already almost never in
  conflict," a different and more specific finding than a null correlation would suggest on its own.
- **Combined gate** (`ADX >= 20 AND DI agrees`): `passes` `n=42` (-1.05%), `fails` `n=3` (+0.38%).
  CI `[-13.81, +16.79]` crosses zero, `n=3` on the fail side.

**Verdict unchanged, now on real data for every leg tested: no gate ships.** DTE floor,
same-direction consecutive-loss gate, and the 0.60 confidence floor remain the only three hard
gates in `_open_trade`. ADX slope is still the one leg of the original doc with no path to testing
without new logging -- everything else has now been measured against real history and come back
without a reliable signal in either direction.

### Live-market prevClose fixed to use the CAS-corrected candle close -- confirmed with real data, same day (25 Aug 2026)

**Reported**: dashboard change/% mismatch against the broker app on an expiry day -- Nifty off by ~36 points (-0.12% vs broker's -0.27%), Bank Nifty off by ~107 points (-0.02% vs broker's -0.21%). Live LTP itself matched closely; only the change figure was wrong. The request's own framing named a "24 Aug market-hours gate patch" and "Lightsail" as context -- neither is real: `git log` shows no market-hours-gate change landed 24 Aug (that date's only change is the `lowest_price` fix directly above, unrelated), and production is EC2/systemd, not Lightsail. Flagged plainly rather than built around.

**Root cause, traced through both real prevClose code paths (they genuinely differ, confirming that part of the request was a fair question):**

- The figure actually showing the mismatch is the **Live market panel** (`get_index_live_figures()`, `app/platform.py`) -- the separate "Market Conditions" panel (`get_market_conditions()`) shows regime/ADX/CPR labels only, no numeric change% at all, so that panel name in the request doesn't point at the right function.
- `get_index_live_figures()`'s `reference` is the most recent `IndexPriceTick` row from before today -- a deliberate approximation shipped 21 Aug specifically to fix an earlier open-vs-previous-close mismatch, whose own docstring already names this exact residual gap: tick recording is gated to `check_market_hours()`'s 09:15-15:30 IST window (`app/signal_validation.py`), which ends before the Closing Auction Session actually settles (~15:35, see "Closing Auction Session" below). So "previous day's last tick" is whatever price a dashboard poll happened to catch just before 15:30, not guaranteed to be the true settlement print.
- AI Origination's own previous-close (`compute_levels()` in `app/market_context.py`, feeds CPR/PDH/PDL, not shown as a numeric % anywhere) is a **separate, candle-based** computation, corrected by the `closing-auction-capture` job (15:45 IST, `capture_closing_auction()` in `app/market_data.py`) built for exactly this class of gap on 3 Aug. That path is very likely accurate; it isn't what's showing the mismatch.
- Today's magnitude (36/107 points) is smaller than 3 Aug's measured CAS gap (200-567 points) but the same mechanism -- consistent with, not necessarily proof of, this being the cause. Expiry-day settlement dynamics plausibly widen the final-auction-window move beyond an ordinary day's.

**Fixed**: nothing computational -- explicitly scoped to diagnosis only, per the request's own "don't ship the fix yet." Added one temporary diagnostic log line in `get_index_live_figures()`, logged on every call: `[PREVCLOSE] {symbol}: reference={value} ({previous-day tick|today's first tick}, recorded_at={ts}) current={value}`. This makes the exact tick this function is choosing, and when it was recorded, directly diffable against the broker's own previous-close on a future trading/expiry day, rather than staying a plausible-but-unconfirmed diagnosis. Remove once the gap is confirmed or the real fix (below) ships.

**Proposed fix, not built**: point the Live market panel's reference at the same corrected candle-based close `compute_levels()`/`capture_closing_auction()` already produce, instead of the `IndexPriceTick` approximation -- reusing a fix this project already built for the AI Origination path rather than inventing a second one for this consumer.

Full suite: 418 passed (unchanged from the `lowest_price` fix -- no new tests, this is a log line only, nothing to assert against without a real dashboard poll). `python -c "import app.main"` imports cleanly.

**Not verified live**. After deploying, watch for `[PREVCLOSE]` log lines on the next trading day and compare `reference`/`recorded_at` against the broker's own displayed previous close directly:

```bash
sudo journalctl -u tradingview-bot --since today | grep "\[PREVCLOSE\]"
```

If `recorded_at` consistently lands meaningfully before 15:30 IST on the previous session (rather than right at the CAS settlement print), that confirms this diagnosis with real data and the candle-based fix above should be built next.

**Confirmed with real production data, same day.** First `[PREVCLOSE]` lines after deploy:

```
BANKNIFTY: reference=57419.45 (previous-day tick, recorded_at=2026-08-24 09:55:00) current=57350.40
NIFTY:     reference=24182.80 (previous-day tick, recorded_at=2026-08-24 09:55:01) current=24146.55
```

`IndexPriceTick.recorded_at` is stored via SQLite's `server_default=func.now()`, which is UTC -- confirmed by `record_index_tick_if_stale()`'s own read-back path, which converts through `to_ist()` before using it. So `09:55:00 UTC = 15:25:00 IST`: the previous day's (itself an expiry day) last recorded tick was captured **4-10 minutes before the CAS settlement actually finalises (~15:29-15:35)**, exactly the gap named above -- not a coincidence of matching magnitude, but the mechanism directly observed. The gap size also matched the original report almost exactly (Bank Nifty ~106.5 points vs. the reported ~107; Nifty ~36.25 vs. ~36), a second independent confirmation on top of the timestamp evidence.

**Fixed for real**: `get_index_live_figures()` now tries the CAS-corrected 1-minute `Candle` close first -- the same table and interval `capture_closing_auction()` writes and `compute_levels()` (AI Origination's own previous-close) already reads -- via `Candle.index_symbol == index.symbol, Candle.interval == ONE_MINUTE, ts_ist < today`, most recent row. Falls back to the previous-day-tick approximation, then today's first tick, only when no candle history exists yet for an index (new index, or candle backfill hasn't run) -- same fail-soft order as before, just with the accurate source tried first. This reuses the fix already built for AI Origination's path rather than maintaining two previous-close mechanisms; `compute_levels()`/`capture_closing_auction()` themselves are untouched.

The `[PREVCLOSE]` log line is kept (not removed) -- now reports which source won (`candle` vs `previous-day tick` vs `today's first tick`) alongside the reference value and timestamp, so a future gap is immediately diagnosable instead of needing this same investigation repeated.

4 new tests (`tests/test_index_live_figures_feed.py`): the candle close is preferred over a simultaneously-present, older/wrong `IndexPriceTick`; the most recent of several previous-day candles is picked, not the earliest; a `FIVE_MINUTE`-interval candle is correctly ignored (only `ONE_MINUTE` rows, matching `capture_closing_auction()`'s own write); and the `[PREVCLOSE]` log line correctly reports `candle` as the source with the right value. All 15 pre-existing tests in this file still pass unmodified -- none of them seed `Candle` rows, so they continue exercising the (unchanged) tick-based fallback path. Full suite: 422 passed (was 418). `python -c "import app.main"` imports cleanly.

**Not yet verified against a second live trading day** -- the fix was deployed and this file's fixed logic is exercised by the new tests, but there's no live re-run of `[PREVCLOSE]` since the fix went in (this same-day round only observed the pre-fix behaviour). After the next deploy cycle, confirm the log line now reports `candle` as the source and that the dashboard's change% matches the broker within a few points on an ordinary day and on the next expiry day specifically, since that's when the old gap was largest.

### `lowest_price` never updated on any long trade -- structurally unreachable code, not missing code (24 Aug 2026)

**Reported**: real `strategy_trades` query evidence, both historical (27-28 Jul) and live (24 Aug), showing `lowest_price` pinned at `entry_price` for every AI Origination trade regardless of how far the premium actually fell. Requested: confirm the missing update logic and add it; verify post-deploy that it now moves; re-run the same historical query pattern going forward; and explicitly confirm (not assume) that MAE% in exports was unaffected, tracing its actual source rather than trusting the existing 14 Aug note that flagged this same column as unreliable.

**More precise than "missing logic."** `monitor_open_trades` (`app/multi_strategy.py`) already has a `lowest_price` update -- it lives inside an `if is_short:` branch (line 415) that mirrors the long-side `else:` branch's `highest_price` tracking exactly. The bug is that `is_short = trade.signal.startswith("SELL")` (line 410) is unreachable-true for every trade this function ever processes: `handle_signal` only opens a position on `BUY_CE`/`BUY_PE` for every non-V7 strategy (`SELL_CE`/`SELL_PE` are observation-only, see "Exit paths" and the shared-FIXED-branch section above), and AI Origination only ever issues `BUY_CE`/`BUY_PE` too. A bought put is still long the premium, not short anything -- so `is_short` is `False` for literally every trade in this population, and the branch that updates `lowest_price` has never once executed. This is the same class of finding as the 12 Aug "trailing stop never activates on PE" false alarm below, except this time the code really was dead, not misread. `app/v7_manager.py` was checked separately and has its own independent, already-correct, unconditional `lowest_price` tracking (`if trade.lowest_price is None: ... else: min(...)`, no `is_short` gate at all) -- V7 trades were never affected by this bug, confirming it as a genuinely separate execution path, as documented elsewhere in this file.

**Fixed**: added the same `min()`-tracking line to the long-side (`else:`) branch, directly after the existing `highest_price` line, mirroring its exact shape. Does not touch the `is_short` branch, any exit-decision logic, the trailing-stop mechanism, or stop/target construction -- `highest_price` alone already drives every long-side trailing/exit decision in this function, so this change only restores a real running-low value to a column that was previously cosmetic.

**MAE% in exports traced directly, not assumed unaffected.** `_excursion()` (`app/dashboard_routes.py`) computes MFE/MAE from `StrategyTradeTick` history (`tick_extremes`, built as `best = high if direction==1 else low; worst = low if direction==1 else high` per tick), never from `trade.lowest_price`/`highest_price` -- confirmed by reading the function directly per the request's own instruction not to trust the existing note alone. The raw `trade.lowest_price` column does appear as its own separate, previously-always-`entry_price` cosmetic field in CSV exports (`dashboard_routes.py` line 388) -- that field starts reflecting real data going forward, which is a side benefit, not a change to any already-computed MAE%.

4 new tests (`tests/test_lowest_price_tracking.py`): a real running low is recorded on a single tick, the minimum (not the latest) price is kept across multiple ticks including a bounce back up, `highest_price` tracking is unaffected by the fix running alongside it, and `lowest_price` stays at `entry_price` (not above it) when premium only ever rises. Full suite: 418 passed (was 414). `python -c "import app.main"` imports cleanly.

**Not verified live** -- this sandbox has no real open trades to observe ticking down. After deploying, per the report's own step 2/3: confirm `lowest_price` moves below `entry_price` on a real open trade that draws down at all, then re-run the same historical query pattern used to find this bug against trades opened after deploy:

```sql
SELECT trade_id, entry_price, lowest_price, highest_price, status
FROM strategy_trades
WHERE entry_time > '<deploy timestamp, UTC>' AND status = 'OPEN'
ORDER BY entry_time DESC;
```

`lowest_price` should now be strictly less than `entry_price` on any trade that has genuinely traded below entry at any point, not pinned at the seed value the way every pre-fix row in the original report was.

### Dashboard "change" now measured against previous close, not today's open -- traced from a real TradingView mismatch (21 Aug 2026)

**Reported**: a screenshot comparison showing StrikeVault's dashboard and TradingView disagreeing on both indices' change figures -- Bank Nifty +64.75 (+0.11%) vs TradingView's +179.50 (+0.31%); Nifty −34.35 (−0.14%) vs TradingView's +1.50 (+0.01%). The live **prices** themselves matched closely (within a couple of points), so this was specifically a "change" calculation mismatch, not a stale/wrong price.

**Root cause**: `get_index_live_figures()` (`app/platform.py`) computed `change_abs`/`change_percent` against **today's first recorded `IndexPriceTick`** -- effectively "change since today's open." TradingView, and every standard market data source, shows change against **the previous trading day's close**. These are two different, both-legitimate numbers that happen to coincide only when an index doesn't gap overnight -- Indian indices routinely do, and the reported mismatch magnitudes (~115 points on Bank Nifty, ~36 on Nifty) are consistent with exactly that kind of overnight gap, not a bug in the live feed or price accuracy.

**Fixed with the previous-day-last-tick approximation, not the fully accurate CAS close.** The truly correct reference is what `capture_closing_auction` (the 15:45 IST job) stores in the candle archive specifically because the naive last-bar-before-close was found to be wrong by hundreds of points on 3 Aug (see "Closing Auction Session" below) -- but that requires a candle fetch, not a cheap `IndexPriceTick` read. Explicitly asked which to build; the previous-day-last-tick approximation was chosen for now. Reference is now the most recent `IndexPriceTick` recorded strictly before today, falling back to today's first tick (the old behaviour) only when no prior-day tick exists at all. `day_low`/`day_high` are untouched -- still computed from today's ticks only, confirmed by a dedicated test that the previous-day reference doesn't leak into the day-range figures.

**Known gap in this approximation, stated rather than hidden**: `IndexPriceTick` recording is gated on `check_market_hours` (09:15-15:30 IST), which ends a few minutes before the CAS settles (~15:35) and the exchange's official close is published (~15:29-15:30 in practice, per the Closing Auction entry). So the "previous day's last tick" this reads is very close to, but not guaranteed identical to, the official closing print -- the same class of small residual gap the CAS job was built to close for AI Origination's own previous-close reads, just smaller in magnitude since tick recording runs later into the session than the old candle-fetch cutoff (15:15) did before that fix.

3 new tests (`tests/test_index_live_figures_feed.py`): change computed against the previous day's last tick rather than today's first, falls back to today's first tick when no prior-day tick exists, and day_low/day_high still computed from today's ticks only. Full suite: 414 passed (was 411). `python -c "import app.main"` imports cleanly.

**Not verified live** -- this sandbox has no real Angel One feed to compare against a live TradingView session. After deploying, the check is: open the dashboard during trading hours and confirm the change figures now track TradingView's within a few points, rather than the previous whole-percent-off mismatch.

### Instrument-master fetch gets a retry + stale-cache fallback -- 5 real timeouts in 7 days (21 Aug 2026)

**Reported**: NV1 `BUY_CE` failed with `[STATE] FAILED_ENTRY ... "HTTPSConnectionPool(host='margincalculator.angelbroking.com', port=443): Read timed out."`, escalated to a Telegram "System Error" alert and an HTTP 500 back to TradingView. Investigated with the user before building: confirmed via `journalctl` that this exact timeout happened **5 times in the prior 7 days**, not a one-off.

**Root cause**: `OptionFinder._load_instruments()` (`app/option_finder.py`) refreshes the instrument-master file **once per IST calendar day** (`_cache_is_fresh` only checks the cached file's date, not its age) -- so every day's *first* `find_atm_contract` call, whichever signal happens to arrive first, has to do a real network `GET` against Angel One's `OpenAPIScripMaster.json` (a large, shared file) with a 20s timeout, no retry, and no fallback. A single slow response on that one daily fetch failed the entry outright.

**Also traced the full failure chain, not just the network call**: `handle_signal`'s try/except around `find_atm_contract`/`get_ltp` logs `FAILED_ENTRY` then re-raises; the outer `webhook()` handler in `app/main.py` catches *any* exception there, logs `"Webhook processing failed"` as an ERROR, sends a **"System Error"** Telegram alert, and returns a 500 -- treating an ordinary third-party timeout identically to a genuine internal bug.

**Fixed, scoped to the confirmed cause only**: `_load_instruments` now retries the fetch once (`_INSTRUMENT_FETCH_ATTEMPTS = 2`, 2s backoff) and, if every attempt still fails, falls back to whatever instrument file is already cached on disk -- even from a prior day -- rather than failing the entry. A day-stale contract list is virtually always fine (contracts don't change day to day except at expiry rollover); failing the trade outright on a transient timeout is the larger, more certain cost. Only raises (and therefore still reaches the loud System-Error alert path) if every retry fails *and* no cached file exists at all -- a genuinely rare, genuinely alert-worthy case, so that behaviour is deliberately left as-is.

**Deliberately NOT changed: the webhook handler's re-raise-on-any-exception behaviour, more broadly.** Checked `git log` first -- this has been the default since the project's earliest commits, with no documented rationale either way. Since the fallback above means this specific failure mode will now rarely reach that path at all, and there's no evidence the *other* exception types that can occur in the same try block (e.g. `get_ltp` failing for its own broker-side reasons) are causing false alarms, broadening the fix to suppress alerting more generally was scoped out -- that would risk quieting a genuine broker-outage alert with no reported problem to justify it.

5 new tests (`tests/test_option_finder_instrument_fetch.py`): succeeds on first attempt and writes cache, retries once then succeeds, falls back to a stale cached file when every attempt fails, re-raises when every attempt fails and no cache exists at all, and confirms `_load_instruments` never touches the network when the cache is already fresh for today. Full suite: 411 passed (was 406). `python -c "import app.main"` imports cleanly.

**Not verified live** -- this sandbox has no network path to Angel One's real endpoint. After deploying, the check is: `sudo journalctl -u tradingview-bot --since "7 days ago" | grep -c "margincalculator.angelbroking.com.*timed out"` should keep counting raw timeout occurrences (that's Angel's side, unaffected by this fix), but `grep -c "FAILED_ENTRY"` for the same cause should drop to roughly zero, and a `"falling back to cached file from"` WARNING line should appear in its place when a timeout does occur.

### Confidence-scoring instruction gets a resolution requirement for self-stated risks -- prompt-only, arrived despite the hedge backtest's null result (19 Aug 2026)

**Requested**: force the model to resolve any risk it names in its own reasoning before
trading on it, rather than stating the risk and proceeding anyway (the `[risk], but
[trades anyway]` shape seen across the 12/14/19 Aug trigger trades). Framed explicitly as
not another gate/detector/threshold -- every other mechanism this cycle (confidence floor,
same-direction consecutive-loss gate, the just-completed hedge-language backtest) acts on
the model's output after it's produced; this changes what the model is instructed to do
*before* producing a decision.

**A second false premise, same as the previous request.** This one repeats "the emergency
trend-extension gate shipped 19 Aug" -- still does not exist (see the entry above). Worse,
it explicitly builds on the reasoning-hedge investigation *despite* that investigation's
same-day conclusion: NOT SUPPORTED at any category, aggregate or isolated. The request's
own framing tries to route around that null result ("that's a statement about detecting it
after the fact, not about whether the underlying behavior is a real defect") -- a coherent,
defensible *qualitative* argument (self-contradictory reasoning is bad practice regardless
of whether a keyword search can prove it correlates with P&L), but a different kind of
claim than the empirical one the backtest just tested and found unsupported. Built on that
basis -- a prompt-only change grounded in the qualitative argument, explicitly not
represented as backed by the (null) outcome data -- rather than silently accepting the
"emergency gate" framing.

**Why this doesn't need the same pre-deployment discipline as a gate.** A gate blocks a
trade outright and carries real risk of blocking a working thesis, which is why every gate
this cycle (DTE floor, consecutive-loss gate) was backtested before shipping. A prompt
change doesn't block anything -- it tries to shape what the model produces -- so it follows
the same precedent as the confidence-scale prompt fix earlier today: ship it, then verify
with a before/after distribution comparison over real elapsed trading time, not a
pre-deployment backtest.

**Implementation.** New paragraph in `SYSTEM_PROMPT` (`app/ai/originator.py`), inserted
after the EMA21-extension caution and before the confidence-calibration paragraph added
earlier today -- grouping every "treat X as a caution" instruction together with one
closing rule for how any such caution must be handled:

> "If your reasoning identifies a specific risk to the trade -- extension beyond a normal
> range, an already-mature trend, absence of a fresh confirming breakout, conflicting
> signals across timeframes, or any other concrete caution -- resolve that risk explicitly
> before deciding to trade. A genuine risk you cannot articulate a specific, concrete
> reason to set aside must result in NONE, not a trade at reduced confidence. A resolution
> names something additional and specific to this setup -- a fresh volume/momentum signal,
> a level just broken and held, a specific reason this trend's exhaustion pattern differs
> from a typical one -- not a restatement of the risk followed by 'but,' and not a general
> appeal to the structure still favoring continuation. If you notice yourself writing
> 'but,' 'however,' or 'although' immediately after describing a risk and immediately
> before a decision to trade, treat that as a signal to stop and re-evaluate: either the
> risk should change your decision, or you have not yet identified why it doesn't."

Applies identically to both providers (single `SYSTEM_PROMPT` constant, confirmed by the
same source-inspection pattern the confidence-calibration change already established).
Does not touch the confidence floor, any gate, entry/exit mechanics, or stop/target
construction.

**Post-deployment check, tooling built, not yet run.** New `scripts/hedge_resolution_check.py`,
explicitly reusing `classify_hedge()` from `reasoning_hedge_backtest.py` rather than
re-implementing the categorization (so "hedged" means the same thing in this check as it
did in the outcome backtest), reads `ai_origination_logs` (decision-level, including NONE
-- trade-level data alone can't show a hedge being resolved into a decline) and reports,
per `--since`-filterable window:

- the **hedge-then-trade rate** -- of decisions containing hedge language, what fraction
  became a trade rather than NONE. This should fall after the change if the model is
  actually converting more hedged setups to declines.
- trade volume in the window -- expected to drop somewhat (that's the mechanism working),
  flagged if it drops to near-zero (overcorrection).
- win rate on trades from the window that have since closed -- the actual goal: if the
  model only trades once it can articulate a real resolution, the trades that remain
  should be higher quality.

4 new tests (`tests/test_hedge_resolution_prompt.py`) pin the new paragraph's presence, the
three named contradiction markers, the no-bare-restatement clause, and that the
confidence-calibration paragraph from earlier today survived intact. 6 more
(`tests/test_hedge_resolution_check.py`) cover the hedge-then-trade rate computation, the
n/a case with zero hedged decisions, the `--since` filter, win-rate computed only from
CLOSED trades, the no-trades-in-window case, and empty reasoning excluded. Full suite: 406
passed (was 396). `python -c "import app.main"` imports cleanly.

```bash
python -m scripts.hedge_resolution_check --db data/trading.db
# after 1-2 weeks on the new prompt:
python -m scripts.hedge_resolution_check --db data/trading.db --since "<deploy timestamp, UTC>"
```

**Not verified live** -- this sandbox cannot call either provider's real API. After
deploying: log the exact deployment timestamp (same requirement as the confidence-scale
fix), run the check once immediately for the baseline, then again after 1-2 weeks. Per the
request's own standard, do not judge this from a handful of decisions -- wait for enough
post-change decisions to compare distributions, not individual outcomes. If the
hedge-then-trade rate doesn't move, that's a real result too: it would mean the model
either doesn't reliably follow this more prescriptive instruction either, or genuinely
can't distinguish a real resolution from a restatement -- worth knowing either way, and
would leave the reasoning-hedge question closed on the null result already recorded above
rather than reopened by a prompt change that didn't change behavior.

### Reasoning-hedge detector rebuilt with a sharper classifier, backtest tooling only -- gate NOT shipped (19 Aug 2026)

**Requested**: five real trades on record where the model's own reasoning states a caution
or a direct contradiction ("but"/"however") and trades anyway regardless -- e.g. "this is
not an ideal fresh entry, but the bearish structure still outweighs the exhaustion risk"
(19 Aug, confidence 0.78). Requested a hedge-language detector on `ai_reasoning`, a
backtest of hedged-vs-not outcomes (win rate/mean P&L/mean MAE, split by provider), and --
if validated -- a gate. A fourth, explicitly non-blocking item (make the model resolve its
own hedge at the prompt level, rather than detect it after the fact) was deferred by the
request itself to a separate change/testing cycle.

**This revisits already-tested ground -- said plainly rather than presented as new.**
`confidence_sizing_backtest.py`'s PART 2 (14 Aug) already tested hedge language with a flat
5-keyword match ("cautious," "moderate," "extended," "already run," "mature") and came back
NOT reliable: hedged mean P&L -2.08% (n=77) vs not-hedged -0.09% (n=108), bootstrap 90% CI
`[-4.68, +0.68]`, crosses zero. What's different in this pass is the DETECTOR, not the
underlying question -- the old flat match is diluted by words like "moderate" appearing in
unrelated contexts. This pass targets the specific failure shape from the trigger examples
(a stated risk clause followed by a contrastive conjunction the model then argues past)
via three categories instead of one flat list. A sharper detector on the same population
could reveal a real effect the old one diluted, or could reproduce the same "not reliable"
verdict on a cleaner signal -- which would itself be a stronger negative than the original,
not a wasted re-test.

**Built, deliverables 1-2**: `scripts/reasoning_hedge_backtest.py`. `classify_hedge()`
categorizes matched phrases into `direct_hedge` ("not ideal," "not an ideal" -- added
alongside the request's own "not ideal" since the literal trigger quote uses the "an" form
and would otherwise have been missed by the request's own phrase list -- "not a strong,"
"not a high-conviction," "moderate rather than," "cautious rather than,"
"moderate-confidence"), `contradiction_marker` ("but"/"however"/"although"/"despite"), and
`risk_acknowledgment` ("the main caution is," "the main risk is," "already extended,"
"already run," "no fresh breakout"). `contradiction_marker` is a deliberate simplification
of the request's own "clause before states a risk, clause after states a decision to
proceed" -- true clause-role parsing needs real NLP, which the request explicitly
authorizes deferring ("start with a keyword/phrase pass... upgrade if needed"). This v1
flags the conjunction's mere presence, which will overmatch some reasoning where "but"
doesn't introduce a real contradiction -- documented in the script rather than quietly
accepted, and worth tightening if that category's own bucket looks materially different
from the other two once real data is available.

A trade is `reasoning_hedged=True` if any category matches at least one phrase; matched
phrases are retained per trade (not just the boolean) for the auditability the request
asked for. `run_backtest()` reports the same three metrics (win rate, mean P&L, mean MAE)
overall, per provider (Claude vs OpenAI -- reusing this cycle's `_provider_from_origin`
pattern, duplicated per this project's established per-script convention), a matched-phrase
frequency table, and a per-category outcome breakdown so a verdict can be traced to a
specific category rather than treated as one signal.

17 new tests (`tests/test_reasoning_hedge_backtest.py`): each category's phrase matching
including case-insensitivity and multi-category matches, the population filter (AI
Origination only, closed only, non-empty reasoning required), MFE/MAE from ticks (same
established pattern as every other confidence/outcome script), provider tagging, the
bootstrap helper detecting a real synthetic gap, and that `run_backtest` reports OpenAI/
Claude sections independently and surfaces the matched-phrase frequency table. Full suite:
394 passed (was 377). `python -c "import app.main"` imports cleanly.

**Deliverable 3 (the gate) is deliberately NOT built this pass.** The request's own
instruction is explicit: "same discipline as every gate this cycle -- validate before
shipping as a hard block." This sandbox cannot run the backtest (no real `data/trading.db`,
same standing constraint as every other backtest script here), so there is no result to
validate against yet. Run on the machine with real history:

```bash
python -m scripts.reasoning_hedge_backtest --db data/trading.db
```

Read the per-category breakdown before concluding anything -- if `contradiction_marker`
alone looks meaningfully worse (or better) than `direct_hedge`/`risk_acknowledgment`, that
would mean the conjunction-presence simplification is carrying real signal despite its
coarseness, worth knowing before deciding whether to invest in real clause-role parsing.
If every category comes back thin or inconclusive the way the 14 Aug flat-keyword pass did,
report that plainly -- per the request's own framing, that would mean hedge language is
decorative rather than informative here, which is a real, useful finding, not a failure to
build the right detector.

**Section 4 (the prompt-level "resolve your own hedge" fix) is out of scope for this pass**,
per the request's own sequencing note: "ship the detector/gate from sections 1-3 first...
the prompt-level fix... is worth pursuing but not blocking on." Not started here. If deliverable
3's gate ships and is later judged insufficient on its own (an after-the-fact block doesn't
stop the model from generating the hedged reasoning in the first place, only from acting on
it), this is the natural follow-up -- and per the request, needs the same before/after
distribution-check discipline as the confidence-scale prompt fix above (hedge-frequency and
hedge-then-trade-anyway frequency, pre- and post-change).

**Not run** -- same constraint as every backtest script in this project. `app/ai/originator.py`
is entirely untouched by this pass; the same-direction consecutive-loss gate, the DTE floor,
and the confidence floor are all unmodified.

**Correction to the request that prompted this**: it referred to "the new trend-extension
gate shipped today" as an existing, live gate. No such gate exists anywhere in this
codebase's history. `app/ai/originator.py:207-214` documents the opposite explicitly --
`trend_duration_pct_of_session` was deliberately kept as a soft prompt caution only, never
turned into a hard gate, because the 11 Aug incident review gave a sweep range to validate
(80/90/95%) rather than a committed threshold, and picking one from a single day's anecdote
was judged exactly the overfitting error this project guards against. The only real hard
gates in `_open_trade` today are the DTE floor (`_MIN_DTE_TO_TRADE`), the same-direction
consecutive-loss gate, and the confidence floor (`_MIN_CONFIDENCE_TO_ACT`) -- flagged here
so this entry doesn't repeat the false premise as if it were established fact.

**Run for real, same day.** 200 closed AI Origination trades with recorded reasoning.
Headline: the aggregate hedged-vs-not-hedged comparison is still **NOT reliable**
(bootstrap 90% CI `[-1.17, +4.09]`, crosses zero) -- and the point estimate actually
**reversed direction** from the 14 Aug flat-keyword pass (that one leaned hedged-worse;
this one leans hedged-slightly-better). A sharper detector on a bigger sample (200 vs 185)
still can't confirm the aggregate hypothesis, and the reversal is itself a second,
independent strike against treating hedge language as a single monolithic signal.

**But the per-category breakdown showed why the aggregate might be hiding something
rather than just confirming nothing**, so the script gained a second pass the same day:
a per-category bootstrap (category vs. everything NOT in that category), since the three
categories perform very differently and pooling them can dilute a real, narrower effect
into aggregate noise.

| Category | n | win rate | mean P&L |
|---|---|---|---|
| `contradiction_marker` (mostly bare "but") | 105 | 38.1% | -0.14% |
| `direct_hedge` | 19 (below trust min) | 31.6% | -3.60% |
| `risk_acknowledgment` | 25 | 36.0% | -3.63% |

`contradiction_marker` alone is 105 of the 120 hedged trades (dominated by 73 raw "but"
matches) and sits close to breakeven -- confirming the docstring's own stated risk that the
bare-conjunction simplification would overmatch and dilute. `direct_hedge` and
`risk_acknowledgment` both traded meaningfully worse and land close to each other;
`risk_acknowledgment` clears the 20-trade trust minimum on its own. Per-provider: OpenAI
(n=175) shows essentially no hedged/not-hedged gap at all (-0.41% vs -0.65%); Claude (n=25)
shows an unexpectedly *reversed* lean (hedged n=3, 66.7% win rate, -0.25%; not-hedged n=22,
27.3% win rate, -5.17%) but the hedged side is pure anecdote at n=3.

**The per-category bootstrap this motivated is built but not yet re-run against this same
data** -- added to `scripts/reasoning_hedge_backtest.py` after seeing the above, isolating
each category against every trade not in it rather than only comparing the pooled hedged
flag. 2 more tests (`tests/test_run_backtest_per_category_bootstrap_*`) confirm it can
detect an effect a large near-neutral category would otherwise mask, and handles a
below-trust-minimum category gracefully. Full suite: 396 passed (was 394).

```bash
python -m scripts.reasoning_hedge_backtest --db data/trading.db
```

**Verdict so far: the aggregate hedge flag does not clear the bar for a gate.** Whether
`risk_acknowledgment` specifically does is the open question the per-category bootstrap
(now built) will answer on the next run -- read that section's output before concluding
anything either way. `direct_hedge` sits one trade below the trust minimum (19 vs 20), so
even a clean bootstrap result there should be read as suggestive, not confirmed, until a
few more trades land. No gate has been added to `app/ai/originator.py`.

**Per-category bootstrap run for real, same day -- FINAL VERDICT: NOT SUPPORTED, at any
level of granularity tested.** All three categories, isolated against every trade not in
them:

- `direct_hedge`: 90% CI `[-7.47, +1.89]` -- crosses zero, and n=19 still one trade short
  of the trust minimum.
- `contradiction_marker`: 90% CI `[-0.75, +4.40]` -- crosses zero comfortably, confirming
  its near-breakeven point estimate (-0.14% mean P&L) is real and not an artefact of
  dilution.
- `risk_acknowledgment`: 90% CI `[-6.19, +0.11]` -- the closest miss of the three (upper
  bound barely above zero), but still crosses zero. By this project's own standard (a
  reliable effect needs a CI that excludes zero), this does not clear the bar either.

**Conclusion: hedge language in `ai_reasoning`, even with a sharper category-isolated
detector purpose-built to rescue a signal the 14 Aug flat-keyword pass might have diluted,
does not predict AI Origination outcomes at the current sample size (n=200).** This is the
outcome the original request itself flagged as acceptable and important if it happened --
"if it doesn't [correlate], that's a real and important finding... the hedge language is
decorative, not informative" -- reported plainly rather than reached past. `risk_
acknowledgment`'s near-miss is the one thread worth revisiting once more trades accumulate
past its current n=25, but it is not evidence today. No gate added to `app/ai/originator.py`;
`same_direction_entries_today`, the DTE floor, and the confidence floor remain the only
real hard gates in `_open_trade`. Section 4 (the prompt-level "resolve your own hedge" fix)
stays out of scope per the original request's own sequencing, and is now on weaker footing
than before this run -- there is no validated hedge-outcome relationship left to build a
prompt-level resolution mechanism around.

### Confidence-scoring instruction rewritten to reduce the Claude/OpenAI scale gap -- prompt-side fix, floor value untouched (19 Aug 2026)

**Requested**: following the per-provider backtest tooling above, fix the likely mechanism
rather than just work around it with two floors -- since both providers receive the same
shared system prompt, check whether the scale mismatch is a prompt-interpretation gap
before accepting it as a fixed model difference. Explicitly scoped to prompt wording only:
does not touch the floor value, entry/exit logic, stop/target construction, or gate logic.
Also explicitly noted by the request itself and worth restating: confidence does NOT
predict outcome for either provider individually (OpenAI Pearson r ~ -0.04 on 170 trades;
no reliable relationship for Claude either) -- this change is about making the two
providers' outputs *comparable to each other*, not about making confidence a better
quality signal. Those are separate questions.

**Deliverable 1 -- exact wording, quoted.** Before this change, the *entire*
confidence-scoring instruction anywhere in `SYSTEM_PROMPT` (`app/ai/originator.py`) was:

> `{"decision": "BUY_CE"|"BUY_PE"|"NONE", "confidence": 0-1, "sl_percent": number, ...}`

plus one contrastive clause earlier in the prompt ("...unlike confidence, which IS 0-1").
That's it -- no numeric anchor example, no qualitative description, no worked example of
what 0.3 vs 0.7 vs 0.9 should mean for this task.

**Deliverable 2 -- diagnosis.** Since there is no numeric anchor anywhere in the prompt
(no "output 0.3 if uncertain" or similar), hypothesis (a) from the request -- a model
anchoring on a literal example value present in the prompt text -- cannot be the
mechanism; there is no anchor value to latch onto. The evidence points to hypothesis (b):
each model falls back to its own internal, differently-calibrated default mapping of
certainty to a 0-1 output for an unfamiliar, completely unanchored scoring task. This
means the fix is not "remove a bad anchor" (there wasn't one) but "add a needed shared
reference frame where none existed" -- a real fix, but one that may not fully close the
gap the way removing a genuinely bad literal anchor would have. Framed accordingly in the
rewrite: relative/behavioural guidance and an explicit instruction to use the tails,
rather than a claim that a specific wording change will make the scales identical.

**Deliverable 3 -- rewritten instruction, identical for both providers.** New paragraph
inserted into `SYSTEM_PROMPT`, in the same imperative/technical register as the rest of
the prompt, between the EMA21-extension guidance and the existing "NONE remains the
correct answer most of the time" line:

> "Confidence must be genuinely calibrated across the full 0.0-1.0 range, not compressed
> toward a cautious middle value. Score each setup relative to the full range of setups
> you could see, not relative to how personally certain you feel in the abstract: reserve
> values below 0.3 for setups you would call genuinely weak or ambiguous, values above 0.8
> for setups with multiple confirming factors and no conflict you flag in your own
> reasoning, and use the full range in between for everything else. A model that never
> uses the tails of the range is not being cautious, it is compressing information the
> downstream trading gate needs."

Deliberately range-based (`below 0.3`, `above 0.8`) rather than a single-value-to-adjective
mapping like the request's own cautioned-against `"0.3 = uncertain"` -- that shape is what
creates a sticky anchor point in the first place. `_call_openai` and `_call_claude` both
still reference the single `SYSTEM_PROMPT` constant (confirmed unchanged: exactly one
`SYSTEM_PROMPT = (` assignment in the module, both call sites read it by name) -- no
per-provider variant was introduced, which would have reintroduced the exact
cross-provider comparability problem this exists to fix. The JSON schema clause itself
(`"confidence": 0-1`) is untouched; the new paragraph is calibration guidance layered
before it, not a replacement.

**Deliverable 4 -- post-deployment distribution check, tooling built, not yet run.** New
`scripts/confidence_distribution_check.py`: reads `ai_origination_logs` directly (the
decision-level table the original 393/394 vs 2/258 counts came from, not just closed
trades -- Claude's closed-trade sample is far too thin on its own) and reports
min/max/mean/distinct-value-count per provider, optionally filtered with `--since` to a
date. Meant to be run twice: once now to log the pre-change baseline (already known from
the diagnosis: Claude 0.10-0.75, mean 0.304; OpenAI ~0.55-0.97, mean ~0.75-0.83) and again
after 1-2 weeks of live decisions on the new prompt, per the request's own instruction not
to judge success from a handful of trades. 4 new tests
(`tests/test_confidence_distribution_check.py`) cover the min/max/mean/distinct
computation, the `--since` filter, and null-confidence rows being excluded. 4 more
(`tests/test_confidence_prompt_calibration.py`) pin the new paragraph's presence, confirm
no single-value-to-adjective anchor pattern was reintroduced, confirm the JSON schema
clause survived unchanged, and confirm `SYSTEM_PROMPT` is still the one shared constant
both providers read. Full suite: 377 passed (was 369).

```bash
python -m scripts.confidence_distribution_check --db data/trading.db
# after 1-2 weeks on the new prompt:
python -m scripts.confidence_distribution_check --db data/trading.db --since <deploy-date>
```

**Success criterion, restated from the request**: Claude's range widening meaningfully
toward OpenAI's -- ceiling moving well above the old 0.75, real use of both tails -- not a
uniform shift and not "looks the same after a few trades." If the range doesn't widen
after a genuine sample, that would mean hypothesis (b) (general calibration difference)
dominates strongly enough that prompt wording alone can't close it, which is itself a
useful, reportable result, not a failure to write the right words.

**Deliverable 5 -- explicit, so this isn't miscast.** This change does NOT address, and
was never expected to address, Claude's separate lower observed win rate (36% on n=25 vs
OpenAI's ~50% on n=106 at time of writing). If Claude's win rate stays lower after a wider,
better-calibrated confidence range accumulates, that is a real, separate finding about
trade quality -- not evidence the prompt fix failed. The two questions (is the confidence
*scale* comparable; is Claude's underlying *trade quality* comparable) must be evaluated
independently. `_MIN_CONFIDENCE_TO_ACT` remains the single shared `0.60` constant,
untouched -- deciding whether/how to use a per-provider floor is still gated on the
backtest from the previous entry, now doubly so since the input distribution it would be
computed against is about to shift.

**Not verified live** -- this sandbox cannot call either provider's real API, so there is
no way to observe how Claude actually responds to the new wording from here. After
deploying: watch the next few days of `[AI][ORIGIN]` logs for any `confidence=` values
that look implausible (e.g. clustering at exactly 0.30 or 0.80, which would suggest the
new range boundaries became new sticky anchors -- the same failure mode in a different
shape), then run the distribution-check script for the real before/after comparison once
1-2 weeks of live decisions have accumulated.

### Per-provider confidence backtest tooling built -- Claude/OpenAI confirmed on different scales, floor value NOT yet decided (19 Aug 2026)

**Requested**: root-cause writeup showed the shared 0.60 confidence floor (14 Aug) gates
Claude out of trading almost entirely -- 393/394 of its post-floor decisions fall below
0.60 (mean 0.304), versus 2/258 for OpenAI (mean 0.827). Claude's own max observed value
(0.75) sits barely above OpenAI's *average*. Requested: a per-provider backtest to check
whether Claude needs its own, lower floor calibrated to its own scale, or whether its
confidence field isn't informative at all -- plus, if supported, implement a per-provider
floor; and separately, flag (not fix) why the two providers' scales differ in the first
place.

**Built, deliverable 1**: `scripts/confidence_by_provider_backtest.py`. Same
`_load_entries`/bootstrap-CI/`MIN_BUCKET_LIVE=20` machinery as
`confidence_sizing_backtest.py` (duplicated per this project's established per-script
convention), split by provider via a new `_provider_from_origin()` parse of the `origin`
suffix (`AI_ORIGIN_OPENAI`/`AI_ORIGIN_CLAUDE` -> `openai`/`claude`; anything else under
`AI_ORIGIN_%` is skipped with a loud warning rather than silently mis-bucketed, in case a
third provider is ever added). Two independent reports:

- **OpenAI reconfirmation**: reruns the *exact* original bucket boundaries (`<0.60,
  0.60-0.75, 0.75-0.85, >0.85`) and the original 0.60 floor bootstrap check, filtered to
  `provider='openai'` only -- same bins on purpose, so the result is directly comparable
  to the already-validated pooled backtest rather than a new measurement.
- **Claude analysis**: bins sized to Claude's own observed range (`<0.20, 0.20-0.35,
  0.35-0.50, >=0.50`, per the request's own suggested bins) rather than reusing OpenAI's,
  which would dump nearly everything Claude produces into one bucket. Sweeps three
  candidate floor cuts (0.20/0.35/0.50) via the same bootstrap-mean-diff comparison the
  original 0.60 pick used, reporting the full surface rather than picking a winner. Warns
  explicitly if Claude's entire population sits below the trust minimum, noting the same
  structural constraint as the `same_direction_entries_today` backtest: post-floor, Claude
  opens almost no new trades, so this population will not grow from further paper trading
  alone -- unlike most "insufficient evidence, keep watching" calls elsewhere in this file.

12 new tests (`tests/test_confidence_by_provider_backtest.py`): provider-suffix parsing
(including an unrecognised third-provider suffix being skipped and warned, not
miscounted), MFE/MAE derivation reused from the ticks table, the floor-bootstrap helper
detecting a real gap / no gap / too-thin-to-compare, and that each report function reads
only its own provider's entries. Full suite: 369 passed (was 357).
`python -c "import app.main"` imports cleanly (scripts/ stays outside app.main's import
graph, per `tests/test_module_imports.py`'s existing isolation check).

**Not run** -- same constraint as every other backtest script in this project: no
`data/trading.db` with real schema in this sandbox. Run on the machine with real history:

```bash
python -m scripts.confidence_by_provider_backtest --db data/trading.db
```

**Deliverables 2 and 3 (a recommended Claude floor, and implementing a per-provider floor
in `app/ai/originator.py`) are deliberately NOT done in this pass.** The task's own
instruction is explicit that this is evidence-gated in either direction -- a real gradient
within Claude's range supports a lower Claude-specific floor, no gradient means confidence
isn't the right lever for Claude at all, and both are acceptable, reportable outcomes. Only
the actual backtest output (from the command above, run against real
`ai_origination_logs`/`strategy_trades` history) can distinguish them; picking a Claude
floor from this sandbox would be exactly the single-anecdote overfitting error this
project's own standing discipline guards against elsewhere. `_MIN_CONFIDENCE_TO_ACT`
remains a single shared `0.60` constant in `app/ai/originator.py`, unchanged, until real
results come back.

**Deliverable 4 (flag the confidence-scale-mismatch question), answered now -- this part
doesn't need production data, just reading the prompt.** Confirmed by grep: the *entire*
confidence-scoring instruction anywhere in `SYSTEM_PROMPT` is the bare JSON-schema clause
`"confidence": 0-1` -- no numeric anchors, no worked examples, no guidance distinguishing
what 0.3 vs 0.7 vs 0.9 should mean for this specific task. `_call_openai` and `_call_claude`
both send the identical `SYSTEM_PROMPT` constant (confirmed by grep -- no per-provider
prompt variant exists), so the miscalibration is not from different instructions reaching
each model. With zero anchoring, each model falls back to its own internal, differently-
calibrated sense of "confidence" for an unfamiliar scoring task, and that is a plausible,
sufficient explanation for two models landing on structurally different scales from the
same three-word instruction. **Not fixed here, per the task's own scope** -- worth a future
pass adding explicit numeric anchors (e.g. "0.5 = a plausible but unremarkable setup, 0.8 =
a setup with multiple confirming factors and no self-flagged conflict") to `SYSTEM_PROMPT`
and re-measuring whether that narrows the gap between providers, which would be a more
durable fix than maintaining two permanently separate floors.

### Settings > General gets a configurable Trading Start Time; Square Off Time turned out to be a decorative field until now (19 Aug 2026)

**Requested**: "Add trading start timing for strategies and ai origination trades 9:45 am by
default and it should be editable like closing time and it should honour both starting and
closing time for strategies and ai origination trades."

**Investigated before building, and found the request's own premise ("editable like closing
time") was only half true.** `PlatformSettings.square_off_time` (default `"15:15"`) has been
editable in Settings > General since early in this project, but grepping every consumer found
it was **never actually read anywhere**:

- Rule-based strategies' TIME_EXIT check in `monitor_open_trades` (`app/multi_strategy.py`) had
  the cutoff hardcoded as a literal `now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute
  >= 15)`.
- `app/scheduler.py`'s `daily-square-off` cron trigger is a separately hardcoded
  `CronTrigger(hour=15, minute=15)`.
- AI Origination's own entry/exit window (`_TRADING_START_HOUR/_MINUTE`,
  `_TRADING_END_HOUR/_MINUTE` in `app/ai/originator.py`) was two more hardcoded module
  constants, entirely independent of the `settings` table.
- Rule-based strategies also had **no entry-time floor at all** -- `check_market_hours()` at the
  webhook layer (`app/main.py`) only ever logged a WARNING on an out-of-hours signal, never
  rejected it, so a `BUY_CE`/`BUY_PE` arriving at any hour the broad market-hours check permitted
  would open a real trade regardless of an admin's intended window.

So the admin-facing "Square Off Time" field changing something the user could point to and see
take effect was never actually true; this task fixes that as well as adding the requested new
start-time field, since both halves needed to be real for either to mean what the request implied.

**Implementation:**

- New `PlatformSettings.trading_start_time` column (default `"09:45"` -- matches AI Origination's
  previous hardcoded start exactly, so deploying the column changes nothing until an admin edits
  it), additive migration in `_ensure_columns()`.
- New `parse_hhmm(value, default) -> (hour, minute)` in `app/time_utils.py` -- shared by both
  `multi_strategy.py` and `originator.py` rather than duplicating "HH:MM" parsing twice. Falls
  back to `default` on anything malformed, matching this codebase's fail-safe-not-fail-crash
  philosophy for a runtime read of an admin setting.
- **Rule-based strategies** (`app/multi_strategy.py`): `handle_signal` gained a real, rejecting
  entry-time gate for `BUY_CE`/`BUY_PE` (checked right after the existing enabled/paper/live
  checks, before any contract resolution) -- this is the first genuine entry-time enforcement
  rule-based strategies have ever had. `monitor_open_trades`'s hardcoded `15:15` TIME_EXIT literal
  is replaced with a dynamic comparison against `square_off_time`, fetched once per monitor tick
  (not per trade in the loop).
- **AI Origination** (`app/ai/originator.py`): `_still_observing`/`_past_trading_end` changed from
  reading hardcoded module constants to accepting explicit `(hour, minute)` tuples; `_TRADING_
  START_HOUR/_MINUTE`/`_TRADING_END_HOUR/_MINUTE` renamed to `_DEFAULT_TRADING_START`/`_DEFAULT_
  TRADING_END` and kept only as the fallback `parse_hhmm` uses. `run_origination_checks` reads
  `PlatformSettings` once per 5-min cycle (session already open there) and threads the parsed
  window through both gates and into `_build_user_prompt` (new `end_hm` parameter, replacing its
  own hardcoded `_TRADING_END_HOUR/_MINUTE` read for the prompt's "minutes to close" line).
- **Settings > General** gets the new "Trading Start Time" input next to the existing "Square Off
  Time" (both `<input type=time>`); the Notifications tab's form (which also POSTs to `/settings`)
  gained a matching hidden passthrough field so saving from that tab doesn't reset the trading
  window to blank. `/api/settings` (the separate JSON API surface) gained the same field on both
  GET and POST for parity.
- **Validation, new**: `update_settings_page` now rejects malformed times and enforces `09:15 <=
  start < close <= 15:15`. The `09:15` floor matches NSE open; **the `15:15` ceiling on close is
  deliberate and load-bearing, not arbitrary** -- `daily-square-off`'s cron trigger is still
  hardcoded at 15:15 as a safety net (`monitor_open_trades`'s dynamic check is the real,
  continuous enforcement; the cron catches anything that check somehow missed). Capping the
  configurable close at 15:15 means that safety net can never fire *before* an admin's configured
  close time and force-close positions early -- allowing a later value without also making the
  cron dynamic would have created exactly that conflict. Rescheduling the cron itself from a
  settings-save call was considered and rejected: the scheduler is constructed at module import
  time in `app/main.py`, before the DB is guaranteed initialized, so wiring a live
  `reschedule_job` call into the settings route would have been meaningfully more moving parts for
  a case (wanting a square-off later than 15:15) this app doesn't otherwise support anywhere.
- **Noted, not changed**: `app/market_context.py`'s opening-range window (09:15-09:45, used by
  `ORB_BREAK`/`PDH_PDL_BREAK` setups) is NOT tied to this setting and stays fixed. An admin
  setting a start time earlier than 09:45 means early entries get considered against an opening
  range that isn't complete yet -- flagged in a code comment at `_DEFAULT_TRADING_START` rather
  than hard-blocked, since a start time before 09:45 is a legitimate (if riskier) admin choice,
  not a malformed one.

21 new tests (`tests/test_trading_window.py`): `parse_hhmm` valid/malformed/out-of-range;
`update_settings_page`'s four validation branches (start-after-close, close-after-15:15,
start-before-09:15, valid-and-persists) plus `apply_settings`; `_still_observing`/`_past_trading_
end` honouring explicit tuples including non-default values; `handle_signal` rejecting entry
before start and at/after close, allowing entry inside a custom configured window (proving DB
values are read, not the hardcoded fallback), and falling back correctly when no `PlatformSettings`
row exists yet; `monitor_open_trades` closing a trade at a configured close time earlier than the
old hardcoded 15:15, and correctly leaving it open before that configured time. 3 existing tests
in `tests/test_nv1_dte_floor.py` needed a frozen clock added (`monkeypatch` on `app.multi_strategy.
datetime`) since they now pass through the new trading-window gate before reaching the option
finder they were actually testing. Full suite: 357 passed (was 336). `python -c "import
app.main"` imports cleanly.

**Not verified live** -- this sandbox has no deployed server. After deploying: confirm Settings >
General renders both fields with the correct defaults (09:45/15:15), confirm saving an invalid
window (e.g. start after close, or close at 15:30) returns a 400 rather than silently accepting
it, confirm a real TradingView webhook signal outside the configured window is now actually
rejected (previously only warned), and confirm an open rule-based trade actually closes at a
custom configured `square_off_time` earlier than 15:15 rather than waiting for the old hardcoded
time.

### NV1 gets a 1-DTE floor -- traded 0 DTE today and lost beyond even its own correctly-rescaled stop (18 Aug 2026)

**Requested**: "We have to minimise losses as they are bugger [bigger] than wins", following a
review of 18 Aug's 7 real trades. Investigated with the user before building: NV1's `BUY_PE`
lost -25.52% on a config with `SL % = 15.0`, a 7-minute hold, `exit_reason=STOPLOSS`.

**First confirmed what did NOT go wrong.** A diagnostic script
(`nv1_stop_check.py`, run against real `data/trading.db`) showed the CE/PE symmetric-premium
rescale (`app/premium_model.symmetric_premium_percent`, see "Put/call sensitivity asymmetry"
below) computed and stored exactly what it should: nominal 15% -> rescaled 23.9% for a Nifty PE
at the traded DTE, `stoploss` column matching to the cent (entry 84.85 -> stoploss 64.57). The
extra -1.62pp beyond that (actual exit 63.2) is ordinary execution slippage -- the 30s monitor
tick can't fill exactly at the computed stoploss price if premium gaps past it between checks on
a fast-moving contract. Neither of those is a bug.

**What actually happened: `dte=0`.** NV1 traded a same-day-expiry contract. `stop_survivability.py`
already measured that a fixed stop's noise-breach rate rises sharply as DTE shrinks -- 36.5% at
2-5 DTE versus 23.4% at 6-10 DTE on Bank Nifty calls, the worst bucket that analysis even tested.
0 DTE is more extreme than either: gamma and theta both spike hardest on expiry day, which is
exactly the shape of "correctly-computed stop, still overshoots on a fast move" seen here.
AI Origination has carried an equivalent floor (`_MIN_DTE_TO_TRADE = 5` in `app/ai/originator.py`)
since 3 Aug for this precise reason. **Rule-based strategies never got one** -- `handle_signal` in
`app/multi_strategy.py` never passed `min_dte` to `find_atm_contract` at all, so it silently
defaulted to 0 for BNV5.1/BNV6/BNV7/NV1 alike.

**A real wrinkle surfaced before shipping anything**: NV1's "Expiry ITM" setting
(`StrategyConfig.expiry_itm_strikes`) turns out to be an expiry-day-specific mitigation, not an
unrelated field -- `option_finder.py`'s `find_atm_contract` only applies the ITM strike shift
`if is_expiry_day and expiry_itm_strikes > 0`. That means 0 DTE trading for NV1 looks
deliberately anticipated, with a partial mitigation already built for it (an ITM strike retains
more intrinsic value than ATM on the last day, which is less all-or-nothing). Flagged to the user
before proceeding: a hard `min_dte` floor doesn't tighten that mitigation, it removes the only
scenario it exists for, since `find_atm_contract` rolling past today's expiry means
`is_expiry_day` can never be true for NV1 again.

**Shipped anyway, deliberately** -- today's real result is the deciding evidence: even with
whatever the ITM shift contributed, the loss overshot the correctly-computed 23.9% stop by
another 1.62pp on a contract that had zero days of time value left to cushion a fast move. Not
trading 0 DTE at all is the more direct fix for what was actually measured, and the ITM-shift
mitigation is not proven to be enough on its own. New `_NV1_MIN_DTE = 1` in
`app/multi_strategy.py`, threaded through as `min_dte=min_dte if strategy.name == "NV1" else 0`
at the `find_atm_contract` call site -- `find_atm_contract`'s existing `min_dte` behaviour
(inherited from the AI Origination floor) **rolls forward to the next listed expiry rather than
declining to trade**, so NV1 keeps firing on an ordinary day, just never against today's expiry.

**Scoped to NV1 alone, not all four rule-based strategies.** BNV5.1/BNV6/BNV7 are the
currently-profitable strategies under this file's own "shared-FIXED-branch hazard" change-freeze;
NV1 is the one that actually hit this, fires under 3x/month per the existing backtest note, and
is already the least statistically tested of the four. Widening this to the other three would be
a materially bigger, unvalidated change to strategies explicitly flagged elsewhere as
off-limits -- not done here.

3 new tests (`tests/test_nv1_dte_floor.py`): NV1's entry passes `min_dte=1` to the option finder;
a different strategy (BNV7) still passes `min_dte=0`, confirming the floor is genuinely scoped
and not a global default change; a similarly-named strategy (`NV1B`) does NOT inherit the floor,
confirming the match is exact-name, not a prefix. Full suite: 336 passed (was 333).
`python -c "import app.main"` imports cleanly.

**Deployed and verified same day.** PR #39 pulled to production and `tradingview-bot.service`
restarted cleanly. Before deploying, a second diagnostic script confirmed the trigger trade's
`expiry_itm_strikes=1` mitigation had fired exactly as designed -- recorded strike 24300 vs.
computed ATM 24250, a clean +50 (one strike interval) shift ITM for a PE, matching
`option_finder.py`'s `atm_strike + itm_shift` rule. **The ITM shift was not the gap.** It worked
correctly and the trade still overshot its correctly-rescaled 23.9% stop by 1.62pp to close at
-25.52% -- the strongest possible confirmation that the DTE floor, not a broken mitigation, was
the right fix. Still not verified against a real roll-forward: this confirms the *diagnosis*, not
that `min_dte=1` actually re-selects a later expiry against a live Nifty expiry calendar. That
check is still pending NV1's next Nifty expiry day -- confirm the log line
`"%s: rolled from %s to %s to satisfy the %s-DTE floor"` (from `option_finder.py`) appears for
NV1 specifically, and that the resulting trade's `expiry` column is not today's date.

### AI Origination job moved off a 24/7 IntervalTrigger; three more scheduled jobs gained NSE-holiday awareness (18 Aug 2026)

**Requested**: "I want nothing should run after market hours and holiday. It is wasting resources."
Follow-up to a question about why `[AI][ORIGIN] Skipped: Signal received outside NSE trading
hours` kept appearing in the log every 5 minutes overnight -- the answer at the time was "nothing
real happens, the market-hours gate inside the job fires first and returns before touching
SmartAPI" (true, and already the state of the world since the 14 Aug fix), but the *scheduler job
itself* still woke up every 5 minutes around the clock to run that check, which is real scheduler
overhead and log spam even though the work inside is free. This pass tightens the outer trigger
itself, not just the inner gate, plus closes two smaller holiday-only gaps found while auditing
every scheduled job against the request's literal "and holiday."

**`ai-origination-check`**: was `IntervalTrigger(minutes=5)`, unconditional, 288 firings/day
every day including weekends. `IntervalTrigger` has no day/time-of-day option, which is exactly
why this was never fixed at the trigger level before -- the 14 Aug fix could only add an in-job
gate. Replaced with `CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5")`, the same
pattern `option-chain-collect` already uses one function below it in this same file. The in-job
`check_market_hours()` call stays exactly as it was -- it's still the real, fine-grained gate
(covers the 09:00-09:15/15:30-15:55 slop this range leaves on both sides, and NSE holidays, which
`CronTrigger` has no calendar for at all) -- but the job now only wakes up ~84 times on an
ordinary trading day and zero times on a weekend, instead of 288 times every single day.

**`pre-market-health`**: fired on every NSE holiday landing on a weekday (`CronTrigger`'s
`day_of_week="mon-fri"` doesn't know the holiday calendar), making one real broker health-check
call each time. Wrapped in a new module-level `_run_pre_market_health_if_trading_day()` that
checks `trading_day_reason()` first -- deliberately NOT added to `HealthManager.run()` itself,
since the "Run Health Check" button on the SmartAPI Health page also calls `run()` directly, and
a holiday is exactly a day an admin might legitimately want to run it manually (e.g. checking
credentials ahead of the next trading day). Kept module-level rather than a closure inside
`create_scheduler()` specifically so it's directly unit-testable.

**`ai-daily-summary` / `ai-weekly-report` / `ai-monthly-report`**: same gap -- all three
`CronTrigger`s fire on any weekday including NSE holidays, and if a real narrative provider is
configured, that means a genuine LLM API call to summarize a day nothing traded. Their
`run_*_job()` scheduler-only wrappers (in `app/reports.py`, already separate from the
`generate_*` functions the Reports page's own "Generate Now" buttons call, so this doesn't touch
the manual path) now check `trading_day_reason()` first. `run_monthly_report_job()`'s existing
`is_last_trading_day_of_month()` check only ever accounted for weekends by its own docstring --
if the last weekday of the month happens to also be an NSE holiday, the month now simply goes
without a report rather than generating one for a day nothing traded.

**Also fixed, prompted by the original "why does it say signal" question**: the log line itself.
`check_market_hours()`'s return text ("Signal received outside NSE trading hours...") is worded
for its other caller -- validating an incoming TradingView webhook, a real event -- and
AI Origination's own periodic self-check reused it verbatim, which read as though a trading
signal had actually arrived at 11pm. Both call sites (`originator.py`'s `[AI][ORIGIN] Cycle
skipped -- ...` and `live_feed.py`'s `[LIVEFEED] Market closed (...)`) now strip the leading
"Signal received " phrase before logging, rather than changing `check_market_hours()`'s actual
return value (which is still correct for its real webhook-validation caller).

**Deliberately NOT touched: `trade-monitor` (30s, still `IntervalTrigger`, still only
weekday/holiday-gated, not hour-gated).** This is unchanged from the 14 Aug decision, restated
here because the current request's blanket "nothing should run after hours" phrasing would
naturally extend to it too. It stays as-is on purpose: this job must keep running through every
hour of an actual trading day, including right up to and past 15:15, so it can still catch a
trade the square-off missed for some reason rather than going silent on it. Its own
empty-open-trades early return already makes an off-hours firing next to free (one indexed DB
query, no SmartAPI). Narrowing its firing window would trade away a real safety net for savings
that are already close to zero. A new test (`test_trade_monitor_stays_a_24_7_interval_trigger`)
pins this down explicitly so a future pass doesn't "fix" it by accident while sweeping the rest
of this file.

**Also confirmed unaffected, no change needed**: `daily-square-off` and `closing-auction-capture`
already fire once/day at a fixed cron time rather than repeatedly, so their residual holiday cost
is a single already-cheap call (`square_off_all`'s own empty-open-trades early return) rather
than something worth adding a calendar check for.

12 new tests: `tests/test_scheduler.py` (7 -- `_run_pre_market_health_if_trading_day` skips on a
weekend/holiday and runs on an ordinary day; `ai-origination-check`'s registered trigger is a
`CronTrigger` with the right day_of_week/hour/minute fields, not present at all when no job is
given; `pre-market-health` wires through the scheduler correctly; `trade-monitor` stays an
`IntervalTrigger`) and `tests/test_report_scheduler_holiday_gate.py` (5 -- each `run_*_job`
skips on a weekend/holiday without ever opening a DB session, runs normally on an ordinary
trading day, and the monthly job's NSE-holiday-on-a-last-weekday edge case). 3 existing
assertions in `tests/test_market_hours_gate.py` updated for the new log wording.

Full suite: 333 passed (was 321). `python -c "import app.main"` imports cleanly -- the new
`app.signal_validation`/`app.time_utils` imports in `scheduler.py` and `reports.py` don't
introduce a circular import.

**Verified live**: started the app against a scratch SQLite DB, confirmed the startup log shows
exactly 8 scheduled jobs registering cleanly with no errors (including the new
`create_scheduler.<locals>.<lambda>` entry for the pre-market-health wrapper), and confirmed the
app still serves requests normally afterward.

**Not verified against real overnight/holiday production traffic** -- that needs the actual
deploy running through a real night and a real NSE holiday to confirm the job-firing-frequency
drop shows up in practice. After deploying, the check is: `journalctl`'s
`[AI][ORIGIN] Cycle skipped` lines should stop appearing between roughly 16:00 and 09:00 IST and
across weekends entirely (a few residual firings in the 09:00-09:15/15:30-15:55 edges are
expected and fine), and `sudo journalctl -u tradingview-bot | grep "pre-market-health\|ai-daily-summary"`
around the next NSE holiday should show no broker-health or report-generation activity that day.

### Dashboard showed "Unavailable" instead of the last known price -- a direct side effect of the live-feed market-hours gate above (17 Aug 2026)

**Reported**: a mobile screenshot of `/` showing both indices as "Unavailable" under a "Market
closed" badge, with the request "It should show last value."

**Root cause, confirmed by re-reading the change made earlier this same day**: before the live-
feed market-hours gate (this file's entry directly above), `IndexFeed._run()` kept retrying every
10s even outside trading hours, so a freshly-restarted process would often pick up at least one
tick fairly quickly even off-hours. After that gate shipped, the feed correctly stops attempting
to connect at all while the market's closed -- which also means `LiveFeedStore` stays completely
empty for the entire closed period after any restart, since nothing ever populates it. In
`get_index_live_figures()` (`app/platform.py`), a feed store with no entry for an index fell
straight to `entry["error"] = "Live feed has not produced a price for this index yet"` with
`price` left `None` -- rendered client-side as "Unavailable". Fixing the pinging problem correctly
exposed this pre-existing gap in the fallback logic, rather than causing it outright.

**Fixed**: that branch now queries the most recent `IndexPriceTick` ever recorded for the index
(no date filter -- whatever the last real value is, regardless of which day) before giving up.
Zero SmartAPI cost, same fail-closed philosophy `LiveFeedStore.get()` itself already documents.
`is_live=False` on this path, which the dashboard's *existing* "stale" badge already handles
correctly (`title="Live feed disconnected -- showing last known price"`) with no template change
needed for that part. Only truly falls through to "Unavailable" when there is no persisted tick at
all for that index (e.g. a brand-new index, or a database with zero history) -- there is genuinely
nothing to show in that case.

**A second bug this exposed, fixed in the same pass**: `live_dashboard.html`'s JS built the
change-percent line unconditionally, so once the fallback price started rendering with no matching
`change_abs`/`change_percent` (no tick recorded *today* yet), it would have shown the literal
string `null (null%)` instead of nothing -- the same "don't render a fabricated/absent value"
principle this codebase applies everywhere else (see `app/ai/originator.py`'s prompt-building
docstrings). That line is now omitted entirely when either figure is `null`, not just left to
concatenate as text.

**Correctness guard, easy to miss**: the fallback price must never be written back as a new
`IndexPriceTick` via `record_index_tick_if_stale`, even in the rare case this path fires *during*
trading hours (feed hasn't produced its first tick of the session yet) -- doing so would inject a
possibly-days-old value into today's tick history and corrupt the change/day-range math computed
from "today's first tick" for the rest of the session. A `price_is_fresh` flag distinguishes a
real feed/SmartAPI read from this fallback and gates the tick-write accordingly.

4 new tests in `tests/test_index_live_figures_feed.py`: falls back to the last known tick with no
SmartAPI call, picks the most recent of several historical ticks (not just any), the fallback
price is never re-recorded as a fresh tick even during trading hours, and genuinely no history at
all still correctly shows "Unavailable" rather than fabricating something. Full suite: 321 passed
(was 317).

**Verified live**: started the app against a scratch SQLite DB after market hours, confirmed the
live feed log correctly showed `[LIVEFEED] Market closed (...); pausing connection attempts`
(confirming the prior entry's fix is working), seeded one `IndexPriceTick` directly, and confirmed
`/api/live-dashboard` returned the real price with `is_live: false` and `change_abs`/
`change_percent` both `null` (not a fabricated 0) instead of the previous `price: null`.

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
