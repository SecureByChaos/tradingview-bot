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
```

## Current state / open items

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

### Days-to-expiry materially affects stop survivability

Same 12% stop, breached by noise within 60 min: Bank Nifty calls 36.5% at 2–5 DTE versus
23.4% at 6–10 DTE. Longer-dated contracts carry higher premium, so the same percentage
is a wider index distance.

AI Origination calls `find_atm_contract(signal, index, 0)` — nearest expiry, no offset,
always. An expiry offset is the cheapest structural improvement identified so far and is
independent of whether any entry signal works. Untested; see the roadmap.

### Backtest tooling

`scripts/backtest/` (numpy-only, isolated from `app.main`'s import graph — a stray pandas
import there costs 80 MB on a 414 MB box), plus `band_significance.py`,
`calibrate_premium.py`, `backtest_baseline.py`, `backfill_candles.py`.

Two traps already fallen into once each, both documented in the roadmap: overlapping
forward windows inflate significance by ~√(window/stride), and premium *elasticity* is
not *delta* (they differ by ~200× for Nifty).

### Still unconfirmed

- Whether Nifty's spot token was corrected to `99926000` in Settings > Instruments.
- Whether the BNV6 Pine Script JSON comma bug (missing comma after `htf_confirmation`,
  trailing comma before the trend object's closing brace) was fixed on TradingView.

### Standing caution

Live-trade sample sizes remain small (tens of trades, days not weeks). Be honest about
that in any analysis; do not present a four-day result as established. The four-day
result that started this work read as "weak but real edge" and two years of data said
otherwise.
