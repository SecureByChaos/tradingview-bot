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
