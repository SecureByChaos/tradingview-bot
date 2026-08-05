# Changelog — 2026-07-31

Four PRs merged today, in order. All developed on `claude/read-claude-md-vuz0tq` against
`main`. Nothing here is live until the user deploys it — see CLAUDE.md's "Deployment is
manual and separate."

## PR #3 — Week 2 Section 1: rate-limit outage fix, stale-data visibility, market-hours guards

https://github.com/SecureByChaos/tradingview-bot/pull/3

Root-caused a real production outage (Friday 11:48–14:33): a 3-attempt/3.5s retry on
SmartAPI's `/quote` calls latched `BROKER_FAILED` permanently on **rate-limit
exhaustion**, not on genuine auth failure.

- `app/smartapi_client.py` — new `SmartAPIRateLimitedError` exception and
  `_retry_rate_limited()` (capped exponential backoff, ~30.5s worst case). Pure
  rate-limiting now never calls `_mark_failed()`; `_status` stays `CONNECTED` through a
  rate-limit episode. Tracks `_last_rate_limited` / `_rate_limit_hits_today` /
  `_rate_limit_hits_total`, surfaced via `get_broker_health()`.
- `app/health/broker_health.py` — downgrades broker health to `WARNING` (not silent
  pass) if rate-limited within the last 15 minutes, even after the connection itself
  recovers.
- `app/db_models.py` / `app/database.py` — new nullable `StrategyTrade.data_stale`
  column (migration via `_ensure_columns()`).
- `app/ai/originator.py` — `_load_market_context()` now returns
  `(MarketContext | None, bool)`; logs a WARNING when the context used for an entry was
  stale.
- `app/dashboard_routes.py` — "Data Stale" column added to the history-export CSV.
- `scripts/backfill_candles.py`, `scripts/pull_option_candles.py` — added
  `--during-market-hours` guard (on by default: refuse to run during NSE hours, since
  these scripts authenticate their own SmartAPI session and compete for the same
  rate-limit budget as live origination).

## PR #4 — Week 2 Section 3: DTE floor for AI Origination

https://github.com/SecureByChaos/tradingview-bot/pull/4

- `app/ai/originator.py` — `_MIN_DTE_TO_TRADE = 5`. `_open_trade()` now resolves the
  contract and checks DTE **before** fetching entry price, so a too-close-to-expiry
  contract is skipped with zero wasted LTP calls. Built on top of `app/premium_model.py`
  (already on `main`), which buckets stop risk by DTE — this closes the gap the roadmap
  flagged between "always nearest expiry" and the known DTE-driven stop-survivability
  asymmetry.

## PR #5 — Week 2 Section 4: option-candle date fix + FUTIDX archive

https://github.com/SecureByChaos/tradingview-bot/pull/5

- `scripts/pull_option_candles.py` — `--start`/`--end` were hardcoded to an already-expired
  window (`2026-07-20`–`2026-07-24`); made `required=True` instead of silently defaulting
  to stale dates.
- `scripts/backfill_futures.py` (new) — pulls 1-minute FUTIDX candles for each enabled
  index's current-month contract (resolved from the instrument master, same
  nearest-unexpired-first logic as `option_finder.py`), stored under `<INDEX>_FUT` in the
  existing `candles` table. This is the only route to real volume for VWAP-dependent
  strategies (BNV5.1, BNV6) — spot index tokens always report volume 0.
  Market-hours-guarded like the other pull scripts. **Confirmed run in production by the
  user** after merge: resolved `BANKNIFTY25AUG26FUT` / `NIFTY25AUG26FUT`, stored
  7467/7500 rows.

## PR #6 — Add BNV6 to the backtest harness

https://github.com/SecureByChaos/tradingview-bot/pull/6

Transcribed BNV6.2 Momentum (real Pine v6 source, pasted by the repo owner) verbatim
into the statistical backtest harness, now that FUTIDX candles exist. Scoped to BNV6
only — BNV5.1 is being tested separately by the user.

- `scripts/backtest/data.py` — `IndexArrays` gains `volume`, `vwap` (session-anchored,
  built from `close` per the Pine source's `ta.vwap(close)`, not typical price),
  `htf_ema9`/`htf_ema21` (15-minute, same T+15 causal-closed-bar rule as the existing
  15-minute EMAs).
- `scripts/backtest/setups.py` — `_bnv6_signals()`: EMA9/21 trend, VWAP position, RSI(14)
  level, 15-min HTF EMA9>EMA21 confirmation, ATR floor, EMA-gap/ATR "strong trend"
  filter, 3-bar prior-high/low breakout (not session-reset, matching the Pine source).
  New `_apply_cooldown()` (24 bars, continuous across session boundaries — mirrors
  Pine's non-resetting `bar_index`, unlike `_apply_daily_cap`'s per-session reset used by
  BNV7/NV1). Registered in `default_setups()` with its own causal assertion (no signal
  before VWAP/HTF EMA9/21 warm up).
- `scripts/backfill_futures.py` — now also resamples fetched 1-minute FUTIDX bars to
  5-minute and stores those, since `setup_significance.py` defaults to `--interval
  FIVE_MINUTE`.
- `scripts/setup_significance.py` — new `--setups` flag (comma-separated) to filter
  `default_setups()` down to a subset, e.g. `--setups BNV6`.

**Open assumption, unconfirmed:** the harness assumes BNV6 runs on 5-minute bars in
production. If its real TradingView chart timeframe differs, `breakout_bars`/
`cooldown_bars` bar-counts won't mean the same thing as in the backtest.

## Still needs the user (no credentials in this sandbox)

- Deploy today's changes.
- Let `backfill_futures.py` accumulate enough 5-minute FUTIDX history.
- Run `python -m scripts.setup_significance --setups BNV6` in production for real
  results.
- Confirm BNV6's actual chart timeframe on TradingView matches the 5-minute assumption
  above.
