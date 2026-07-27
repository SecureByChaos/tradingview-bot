# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

StrikeVault: a FastAPI webhook bot that turns TradingView alerts into BankNifty/Nifty/Sensex option trades on Angel One (SmartAPI), with a database-driven multi-strategy engine, SQLite trade state, a Bootstrap dashboard, and an optional AI review/origination layer. See `README.md` for the full feature list, webhook payload formats, environment variable reference, and deployment steps — it is kept accurate and should be the first stop for "how do I configure X."

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # fill in SMARTAPI_*, ADMIN_PASSWORD (bcrypt hash), SESSION_SECRET_KEY

# Run the dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Tests (pytest, no config file — just plain pytest)
pytest
pytest tests/test_trade_manager.py
pytest tests/test_trade_manager.py::test_closes_on_target_and_logs_trade

# Docker
docker compose up -d --build
docker compose logs -f
```

There is no lint/format tooling configured in this repo (no ruff/black/flake8 config, no pre-commit). There is no Alembic — schema changes are applied by `init_db()` at startup (see Database below), except the `ai_*` tables which ship as hand-written SQL under `migrations/` and must be applied manually for pre-existing databases (`sqlite3 data/platform.sqlite3 ".read migrations/00X_....sql"`, in order).

Generate a bcrypt admin password hash: `python -c "import bcrypt; print(bcrypt.hashpw(b'your-password', bcrypt.gensalt()).decode())"`.

## Architecture

### Process wiring (`app/main.py`)

Everything is instantiated once at import time as module-level singletons (`smartapi`, `option_finder`, `multi_strategy_manager`, `v7_manager`, `risk_service`, `monitor`, `health_manager`, `scheduler`) and attached onto the FastAPI router objects (`api_routes.router.X = ...`) rather than using FastAPI dependency injection for these. `lifespan()` calls `init_db()`, authenticates SmartAPI, and starts the APScheduler background scheduler. `POST /webhook` is the single entry point for TradingView; it runs signal-integrity checks (log-only, never blocking — `signal_validation.py`), routes to `v7_manager` or `multi_strategy_manager` depending on strategy name, and — for accepted BUY signals — queues a background AI shadow review.

### Three trade managers, one `StrategyTrade` table

- **`multi_strategy.py` (`MultiStrategyTradeManager`)** — the real engine for every non-V7 strategy. Strategies are DB rows (`StrategyConfig`), not code; no code changes are needed to add a new TradingView strategy name. Each strategy independently tracks PAPER/LIVE mode, TP/SL (fixed or trailing), max active trades, and its own consecutive-loss lock. `SELL_CE`/`SELL_PE` from TradingView are **observation-only** for these strategies (`record_exit_suggestion`) — real exits are decided exclusively by `monitor_open_trades`' own SL/target/trailing/stall/time checks on the 30s monitor tick, never by the incoming signal.
- **`v7_manager.py` (`V7Manager`)** — V7 is the exception: TradingView itself decides exits (`SELL_CE`/`SELL_PE` closes the position directly), so V7 has its own trailing-stop implementation and bypasses the shared SL/TP engine entirely.
- **`trade_manager.py` (`TradeManager`)** — older single-strategy/single-active-trade implementation, largely superseded by the multi-strategy engine but still wired into a few legacy endpoints in `api_routes.py` (`/active-trade`-style single-trade API, `square_off_open_trade`). `tests/test_trade_manager.py` tests this legacy path, not the multi-strategy engine.

All three write/read `StrategyTrade` rows (`app/db_models.py`). Position sizing everywhere is `lots_per_trade * lot_size` (not capital-based — `capital_per_trade` on `StrategyConfig` is a deprecated, unused column kept only so inserts satisfy an old NOT NULL constraint on production DBs).

### The `origin` isolation pattern — read this before touching AI trade code

Every `StrategyTrade` has an `origin` field: `"SIGNAL"` (a real TradingView-driven trade) vs `"AI_ALT_<provider>"` or `"AI_ORIGIN_<provider>"` (AI-driven paper evaluation trades). This is the single most important cross-cutting invariant in the codebase: **every** query that determines real trading state — `current_state`, `active_trade_count`, cooldown-after-loss checks, `latest_open_trade_for_option`, TradingView exit-suggestion lookups — explicitly filters `origin == "SIGNAL"`. AI-originated trades must never be countable against `max_active_trades`, must never trigger a strategy's risk lock or Telegram alerts, and must never be closeable by a TradingView exit signal meant for the real trade. When adding new code that reads or counts open/closed trades, always check whether it needs the same `origin == "SIGNAL"` filter (or the inverse, for AI-only reporting).

### AI layer (`app/ai/`) — three independent experiments, don't conflate them

1. **Entry shadow review** (`shadow.py`, `run_shadow_review`) — fires once in the background after a real `SIGNAL` trade opens; builds a rich context from TradingView payload + SmartAPI + DB fallback (tracking which source each field came from, for a completeness score), gets an ACCEPT/REJECT verdict from the configured provider(s), and saves it to `AITradeReview`. A **REJECT** on an entry triggers `alternative_trader.py`'s `maybe_open_alternative_trade`, which paper-opens a side-by-side alternative call — never live, never touching the original trade.
2. **Exit-call shadow checks** (`exit_shadow.py`, `run_exit_shadow_checks`, scheduled every 3 min) — asks EXIT/HOLD on every open `SIGNAL` trade using only that trade's own numbers; writes to the standalone, read-only `AIExitCall` table. Cannot close a real trade. A trade stops being re-checked once any row has `decision == "EXIT"`.
3. **AI Origination** (`originator.py`, `run_origination_checks`, scheduled every 5 min) — the AI trades with **no TradingView signal at all**, deciding BUY_CE/BUY_PE/NONE purely from recent index tick history (`IndexPriceTick`) plus best-effort real session OHLC. Trading window is 09:30–15:15 IST. Each configured provider gets its own independent trade slot per index (so Claude and OpenAI can each hold a Bank Nifty position simultaneously) with which-provider-goes-first alternating every 5-minute cycle to avoid structural bias. Falls back to trailing-stop risk management whenever the AI's proposed `sl_percent`/`target_percent` are missing or outside the 5–50% sane band (see `_AI_ORIGIN_TRAIL_*` constants in `multi_strategy.py` for the trailing mechanics scoped specifically to `AI_ORIGIN_*` trades).

Providers are pluggable (`app/ai/factory.py`, `dummy`/`openai`/`claude`); a second, independently-configured provider can run in parallel with the primary for direct comparison (`AISettings.secondary_*`).

### Live-trading safety: the two-key pattern

Nothing places a real order unless **two independent switches** both say yes: (1) a scoped opt-in — `StrategyConfig.mode == LIVE` + `live_trade` for normal strategies, or `IndexConfig.ai_origination_live_trade` for AI Origination (which has no `StrategyConfig` row of its own) — **and** (2) the server-side `SMARTAPI_LIVE_TRADING` env var (`Settings.live_trading`). `resolve_mode()` in each manager and `SmartAPIClient.place_market_order` both independently enforce this, so a bug in one layer can't alone cause a live order. `AI_ALT_*` alternative trades are hard-coded to always stay PAPER regardless of any setting.

### Risk/lock model

Two independent layers: per-strategy (`StrategyStats.risk_locked`, consecutive losses + `daily_max_loss_percent` via `RiskProtectionService.enforce_daily_loss_limits`, checked every monitor tick) and a 30-minute post-loss cooldown checked in `main.py`'s webhook handler (also `origin == "SIGNAL"`-scoped). Both reset automatically at the first webhook of each new IST trading day (`reset_daily_risk_if_needed`). AI Origination has its own separate cooldown constant (`_REOPEN_COOLDOWN_MINUTES` in `originator.py`) which is currently **disabled on purpose** to observe raw trade volume — see the comment there before re-enabling it.

### Scheduler (`app/scheduler.py`)

APScheduler jobs, all IST-timezoned: 30s trade monitor (`MultiStrategyMonitor.tick`, covers both multi-strategy and V7 open trades), 3-min AI exit-shadow checks, 5-min AI Origination checks, 15:15 daily square-off (cron), 09:00 weekday pre-market health check, and daily/weekly/monthly AI report jobs (`app/reports.py`).

### Database

SQLAlchemy ORM models in `app/db_models.py`; no Alembic. `database.py`'s `init_db()` runs `Base.metadata.create_all` then a manual `_ensure_columns()` that adds any missing columns via raw `ALTER TABLE` (idempotent, checked against `inspect(engine).get_columns(...)`) — **this is how schema migrations work in this repo for everything except the `ai_settings`/`ai_trade_reviews`/`system_health_logs`/`ai_context_logs` tables**, which instead have hand-written files under `migrations/` intended for manual application. When adding a column to an existing table, add it to the model *and* to the `_ensure_columns()` statement dict, or existing databases will never pick it up. Cost fields (`estimated_cost`/`net_pnl`) are backfilled for historical closed trades on every startup (`_backfill_trade_costs`) since they're derivable from data already stored; diagnostic-only fields like `spot_at_entry`/`tick_sample_count` are intentionally left NULL on old rows since they cannot be reconstructed.

### Costs and P&L

`StrategyTrade.profit_loss`/`pnl_percent` are always **gross** and never modified retroactively (so historical analysis stays comparable). `app/trade_costs.py` computes Angel One's real round-trip costs (brokerage, STT, exchange txn, SEBI, stamp duty, GST — all published rates) into a separate `estimated_cost` field, with `net_pnl = profit_loss - estimated_cost`. Slippage is a separate, explicit, zero-by-default parameter (not folded into the statutory formula) since it's empirical and only meaningful once real LIVE fills exist to calibrate it.

### Timezone handling

All scheduling and business-day logic uses IST (`Asia/Kolkata`) via `app/time_utils.py` (`utc_now()`, `to_ist()`, `format_ist()`, `IST`). SQLite does not reliably round-trip `tzinfo` even on `DateTime(timezone=True)` columns — always go through `to_ist()` rather than subtracting raw datetime columns; see the comment in `multi_strategy.py`'s stall-exit check for the failure mode this avoids.

### Shared query/aggregation layer

`app/platform.py` is the central helper module used by webhook handling, dashboard routes, and the API — bot/strategy state getters, daily stats rebuilding, trade serialization, and most of the dashboard's summary/reporting queries (`get_dashboard_summary`, `get_performance_summary`, `get_exit_shadow_summary`, `get_origination_summary`, `origin_comparison_metrics`, etc.) live here rather than in the route files.

### Diagnostic scripts (`scripts/`)

Not part of the running app; run manually for offline analysis. `reconcile_origination.py` splits closed AI Origination trades into PHANTOM (zero-movement artifacts)/PRE_VALIDATION (predate the 5–50% sane SL/target band)/VALID buckets with gross/cost/net breakdowns, because raw trade counts include known bad data. `pull_option_candles.py` pulls historical 1-minute option candles for traded strikes before they expire off Angel One's API (time-sensitive — expired contracts disappear after expiry day).
