# StrikeVault

FastAPI options auto-trading bot for Indian index options (Bank Nifty, Nifty 50; Sensex planned), with two independent entry sources — TradingView webhook strategies and an AI-driven origination engine (Claude / OpenAI) — Angel One SmartAPI execution, SQLite-backed trade state, scheduled monitoring, and an admin dashboard.

**Everything is PAPER trading unless two independent switches are both on** — see [PAPER and LIVE Modes](#paper-and-live-modes). Real money is at stake even in paper mode, since paper results drive live decisions.

## Architecture

```text
TradingView --> POST /webhook --> Multi-Strategy Engine --\
                                                             >--> Angel One SmartAPI --> SQLite Trade History
AI Origination (Claude/OpenAI, own 5-min cycle) -----------/          |
                                                                 Trade Monitor (30s)
```

TradingView strategies and AI Origination are independent entry paths that share the same execution, risk, and monitoring layer. `StrategyTrade.origin` (`SIGNAL`, `AI_ORIGIN_OPENAI`, `AI_ORIGIN_CLAUDE`, `AI_ALT_*`) keeps their trade history, stats, and risk locks from mixing.

## Features

- `POST /webhook` accepts `BUY_CE`, `SELL_CE`, `BUY_PE`, and `SELL_PE`, routed by `strategy` name.
- Dynamic multi-strategy engine driven by database strategy configuration — no code changes to add a strategy.
- Multi-index support (Bank Nifty, Nifty 50 today; Sensex planned) configured per-index in Settings > Instruments.
- **AI Origination**: an independent engine that opens its own trades from price/technical structure alone, with no TradingView signal involved. Runs on its own 5-minute cycle during a configurable trading window, supports Claude and OpenAI as independent providers (each gets its own trade slot per index), and carries its own risk gates (confidence floor, DTE floor, same-direction consecutive-loss gate, admin-configurable max stop-loss).
- Configurable trading start/close time (Settings > General), enforced for both rule-based strategies and AI Origination — not just displayed.
- Strategy-specific PAPER/LIVE mode, TP/SL (FIXED or TRAILING), max active trades, max trades/day, consecutive-loss limit, daily loss limit, and lot sizing.
- Two-key live-trading safety: a per-entity opt-in (strategy or index) **and** a server-side environment variable must both be on before any real order is placed.
- Single-admin login with signed session cookies.
- Bootstrap 5 dark dashboard: live index prices, market-conditions read, trade history, bot control, settings, reports, and structured logs.
- SQLite-backed platform state for bot status, settings, strategy/index configs, strategy trades, tick history, daily stats, AI decision logs, and structured event logs.
- REST API under `/api/*` for status, trades, settings, strategy stats, bot controls, kill switch, and daily-lock reset.
- Telegram notifications for trade events, exits, risk locks, and system errors.
- Automatically resolves the nearest-expiry ATM option contract per index from a daily-cached Angel One Scrip Master file, with retry and stale-cache fallback on a fetch failure.
- Daily risk lock and consecutive-loss circuit checks before trade execution.
- Server-side trailing stop support, per strategy or per trade.
- Monitors active trades every 30 seconds; squares off open positions at the configured close time (default 15:15 IST).
- Scheduled jobs for AI Origination, pre-market health checks, option-chain archival (collection only, off by default), closing-auction capture, and daily/weekly/monthly AI-generated reports.
- Safe-by-default paper mode via `SMARTAPI_LIVE_TRADING=false`.

## Local Setup

Python 3.11+ is recommended.

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Create a bcrypt admin password hash:

```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'your-password', bcrypt.gensalt()).decode())"
```

Put the generated value in `ADMIN_PASSWORD`.

Check health:

```bash
curl http://localhost:8000/health
```

Send a test signal:

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"strategy":"V7","signal":"BUY_CE"}'
```

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `SMARTAPI_API_KEY` | Angel One SmartAPI app API key |
| `SMARTAPI_CLIENT_ID` | Angel One client ID |
| `SMARTAPI_PIN` | Angel One PIN |
| `SMARTAPI_TOTP_SECRET` | TOTP secret from SmartAPI setup |
| `SMARTAPI_LIVE_TRADING` | Set `true` only when ready for real orders — the server-side half of the two-key live-trading safety pattern |
| `SMARTAPI_PRODUCT_TYPE` | Order product type, default `INTRADAY` |
| `SMARTAPI_ORDER_VARIETY` | Order variety, default `NORMAL` |
| `ADMIN_USERNAME` | Dashboard admin username |
| `ADMIN_PASSWORD` | Dashboard admin password as a bcrypt hash |
| `SESSION_SECRET_KEY` | Long random secret for signed session cookies |
| `SECURE_COOKIES` | Set `true` when serving over HTTPS |
| `DATABASE_URL` | SQLAlchemy DB URL, defaults to SQLite in `data/` |
| `QUANTITY_LOTS` | Default lots to trade (legacy single-index path; per-strategy `lots_per_trade` is the real sizing lever — see Multi-Strategy Operation) |
| `BANKNIFTY_LOT_SIZE` | Legacy fallback Bank Nifty lot size; per-index lot size is configured in Settings > Instruments |
| `BANKNIFTY_SPOT_EXCHANGE` / `BANKNIFTY_SPOT_SYMBOL` / `BANKNIFTY_SPOT_TOKEN` | Legacy fallback Bank Nifty spot identifiers; per-index values live in Settings > Instruments |
| `DEFAULT_STRATEGY_NAME` | Strategy used for webhook payloads without `strategy` |
| `INSTRUMENT_CACHE_PATH` | Cached Angel One Scrip Master JSON path, refreshed once per IST calendar day |
| `INSTRUMENT_MASTER_URL` | Angel One Scrip Master source URL |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `OPTION_CHAIN_COLLECTION_ENABLED` | Archival-only option-chain snapshotting, default `false` — do not enable without `SMARTAPI_ANALYTICS_*` set to a genuinely separate API key; it has caused live rate-limit incidents when sharing the live client's budget |
| `OPTION_CHAIN_STRIKE_BAND` / `OPTION_CHAIN_EXPIRY_COUNT` / `OPTION_CHAIN_INTERVAL_MINUTES` | Option-chain collector tuning, only relevant if collection is enabled |
| `SMARTAPI_ANALYTICS_API_KEY` / `_CLIENT_ID` / `_PIN` / `_TOTP_SECRET` | A second, independent SmartAPI credential set for the option-chain collector, so it never competes with live trading for rate-limit budget |

Trading start/close time, per-strategy risk parameters, per-index instrument config, and AI provider settings are **not** environment variables — they live in the database and are edited from the dashboard (Settings tabs), so they can change without a redeploy.

## SmartAPI Setup

1. Create an Angel One SmartAPI app and collect the API key.
2. Enable TOTP for the trading account and store the TOTP secret in `.env`.
3. Confirm each enabled index's lot size and spot token in Settings > Instruments against the current exchange contract specification.
4. Start with `SMARTAPI_LIVE_TRADING=false` (default).
5. After paper testing, enable the specific strategy's/index's live-trading opt-in **and** set `SMARTAPI_LIVE_TRADING=true`, then restart the app — both are required.

The code uses Angel One's SmartAPI Python SDK through `app/smartapi_client.py`. It performs TOTP login, stores JWT/refresh/feed tokens, retries API calls after token-expiry or rate-limit responses, refreshes the session when possible, and falls back to full TOTP login if refresh fails. Every quote-family call (`get_ltp`, `get_index_ohlc`, `get_candles`) shares a single process-wide 1 req/sec throttle.

## TradingView Webhook Setup

Create a TradingView alert with webhook URL:

```text
http://YOUR_SERVER_IP:8000/webhook
```

Alert message for CE:

```json
{"strategy":"BNV5.1","signal":"BUY_CE"}
```

Alert message for PE:

```json
{"strategy":"BNV6","signal":"BUY_PE"}
```

Short-side examples (observation-only for every strategy except V7 — see below):

```json
{"strategy":"V7","signal":"SELL_CE"}
```

```json
{"strategy":"V7","signal":"SELL_PE"}
```

The bot enforces platform state, strategy enabled state, per-strategy active trade limits, risk settings, and the configured trading start/close window (Settings > General) — a signal outside that window is rejected, not just logged.

Legacy payloads without `strategy` route to `DEFAULT_STRATEGY_NAME` from `.env`.

## Multi-Strategy Operation

Strategies are stored in the `strategy_configs` table and managed from Settings > Strategies. The backend does not require code changes for new strategy names.

Each strategy has:

- `name`, `enabled`, `mode` (`PAPER`/`LIVE`)
- `index_symbol` — which configured index this strategy trades
- `expiry_itm_strikes` — shifts the selected strike ITM by N strikes, but only on expiry day itself (0 = always ATM)
- `tp_percent`, `sl_percent`, `sl_mode` (`FIXED` or `TRAILING`)
- `trailing_activation_percent`, `trailing_offset_percent`
- `max_active_trades`, `max_trades_per_day`, `max_consecutive_losses`, `daily_max_loss_percent`
- `lots_per_trade`, `paper_trade`, `live_trade`

When a `BUY_CE`/`BUY_PE` webhook arrives, the engine:

1. Loads the strategy by name; rejects if it doesn't exist, is disabled, or the current time is outside the configured trading window.
2. Checks bot status, daily lock, consecutive-loss limit, and daily loss limit.
3. Checks open trades only for that strategy; rejects if `max_active_trades` is reached.
4. Resolves the index's nearest-expiry ATM (or expiry-day ITM-shifted) option contract.
5. Sizes the position from `lots_per_trade × lot_size`.
6. Opens PAPER or LIVE according to strategy config and the global `SMARTAPI_LIVE_TRADING` switch.
7. Monitors TP, SL, trailing stop, and square-off for every open trade every 30 seconds, independently per strategy.

`SELL_CE`/`SELL_PE` webhooks are **observation-only** for every strategy except V7 — they never close a position; exits are always decided by the monitor's own SL/target/trailing logic. V7 is the one exception (see below).

Multiple strategies (and AI Origination) can hold independent open positions simultaneously; they are never merged.

## AI Origination

An independent entry engine (`app/ai/originator.py`) that opens trades from index price structure and technical indicators alone — no TradingView signal, and each provider (Claude, OpenAI) makes its own independent decision every 5-minute cycle during the configured trading window (default 09:45–15:15 IST). Each provider gets its own trade slot per index, so both can hold concurrent separate positions on the same index.

What it sees, per cycle: regime measures (ADX, ATR, CPR classification), key price levels (opening range, previous session high/low/close, today's range), trend indicators, extension from short-term mean, and price drift. It does **not** see the option chain, OI/IV/PCR, India VIX, news, or its own trade history.

Entry gates, checked before any contract is resolved:

- Confidence floor — the model's own self-reported confidence must clear an admin-configurable threshold.
- DTE floor — never trades a contract expiring same-day; rolls forward to the next listed expiry instead.
- Same-direction consecutive-loss gate — blocks a new entry once the most recent same-index-same-direction trades lost N times in a row (a single intervening win resets the streak).
- Admin-configurable max stop-loss — caps how wide the model's proposed stop can be; an out-of-range stop/target falls back to trailing-stop management instead of being substituted with a fixed number.

Exit reasons: `STOPLOSS`, `TARGET`, `TRAIL_EXIT` (after an activation threshold, a rescued winner giving back a bounded amount), `STALL_EXIT` (flat for too long with the trail never armed), `TIME_EXIT` (square-off at the configured close time).

Configure providers, the confidence threshold, and the risk knobs above from Settings > AI.

## PAPER and LIVE Modes

`PAPER` records simulated trades using live premium data for entry and monitoring — no broker order is placed.

`LIVE` sends a real broker order only when **both** are true:

- The per-entity opt-in is on (`StrategyConfig.live_trade` for a rule-based strategy, `IndexConfig.ai_origination_live_trade` for AI Origination on that index) **and**
- The server-side `SMARTAPI_LIVE_TRADING` environment variable is `true`

`place_market_order` independently no-ops to a `PAPER_ORDER` id if the server-side switch is off, so a dashboard checkbox alone can never move real money.

## Trading Window

Settings > General has two fields, enforced for both rule-based strategies and AI Origination:

- **Trading Start Time** (default `09:45`) — no new entries before this time.
- **Square Off Time** (default `15:15`, capped at `15:15`) — every open trade closes at or after this time.

The `15:15` ceiling on Square Off Time is deliberate: a daily square-off safety-net job still fires at a fixed 15:15 as a backstop, so a later configured close would let that job force-close positions before the admin's intended time.

## V7 TradingView-Managed Execution

V7 uses a separate execution path. `BUY_CE`/`BUY_PE` open ATM option trades; `SELL_CE`/`SELL_PE` close the matching open V7 option trade (unlike every other strategy, where `SELL_*` is observation-only).

For V7 only, exits are managed by TradingView. The server-side stop-loss, target, and trailing-stop engines do not manage V7 trades. SmartAPI execution, paper trading, trade history, Telegram alerts, and ATM strike resolution remain active.

## Risk Locks and Trailing Stops

Consecutive-loss counts and locks are stored independently per strategy; a strategy lock does not block other strategies. Global daily account-loss protection is separate from strategy-level locks. At the first webhook of each new `Asia/Kolkata` trading day, strategy consecutive-loss counts, locks, and daily risk status reset automatically.

Open trades track `highest_price`, `lowest_price`, `trailing_active`, and `trailing_stop`. For long entries (`BUY_CE`, `BUY_PE`), trailing activates after price advances past the strategy's activation threshold; the stop trails below `highest_price` by the configured offset. For short entries, the mirror image applies.

## Database Migrations

No Alembic, no manual migration step. `init_db()` calls `_ensure_columns()` (`app/database.py`) on every startup, which adds any missing column via a guarded, additive `ALTER TABLE` — safe to run against an existing database with real trade history. New columns default to whatever value reproduces the pre-migration behavior, so deploying a schema change never silently alters existing rows or requires a manual step.

## Settings (Dashboard)

- **General** — trading start/close time, Telegram bot token and chat ID.
- **Strategies** — add/edit/enable/disable/delete rule-based strategies (see Multi-Strategy Operation).
- **Instruments** — per-index config: enabled, exchange segment, spot symbol/token, lot size, strike interval, and the AI Origination live-trading opt-in.
- **AI** — provider (primary + optional secondary), model, API key, base URL, temperature, timeout, confidence threshold, system prompt, and AI Origination's own risk knobs (max stop-loss %, max consecutive same-direction losses).

The AI tab's **Test Connection** button runs the configured provider independently of trading and displays only provider, model, latency, status, and a sanitized error — never the raw API key or full prompt/response.

## Production Deployment

The live deployment runs directly on a host (currently an Ubuntu EC2 instance) under a Python virtualenv and systemd — no container in production.

1. Provision an Ubuntu host, open the port the app listens on (or place it behind Nginx with HTTPS), and clone the repo.
2. Create the venv and install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real credentials
```

3. Create a systemd unit, e.g. `/etc/systemd/system/tradingview-bot.service`:

```ini
[Unit]
Description=TradingView Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tradingview-bot
ExecStart=/home/ubuntu/tradingview-bot/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

4. Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tradingview-bot.service
```

5. Deploying a change:

```bash
cd ~/tradingview-bot
git pull origin main
sudo systemctl daemon-reload   # only needed if the unit file itself changed
sudo systemctl restart tradingview-bot.service
sudo systemctl status tradingview-bot.service --no-pager
```

Deployment is manual and separate from development — a merged change to `main` is not live until this restart happens.

## API

Dashboard pages require login at `/login`.

### `GET /health`

Returns service status and live-trading mode.

### `GET /active-trade`, `GET /trades`

Legacy single-index-era read endpoints; the dashboard and `/api/*` routes below are the actively maintained surface.

### `POST /webhook`

Payload:

```json
{"strategy":"V7","signal":"BUY_CE"}
```

or:

```json
{"strategy":"V7","signal":"SELL_PE"}
```

Optional fields (`market_data`, `indicators`, `trend`, `strategy_filters`, `trade_state`) may be included for logging/validation context; unknown extra fields are accepted, not rejected.

### Admin REST API (`/api/*`)

Authenticated session required:

- `GET /api/status`
- `GET /api/trades`, `GET /api/trades/export`
- `GET /api/strategies`, `GET /api/strategy-stats`
- `GET /api/instruments`
- `GET /api/settings`, `POST /api/settings`
- `GET /api/daily-stats/export`
- `GET /api/logs/export`
- `GET /api/reports/export`
- `GET /api/ai/latest-context`
- `POST /api/start`, `POST /api/stop`, `POST /api/restart`
- `POST /api/kill-switch`
- `POST /api/reset-daily-lock`

## Dashboard

- `/` — live index prices, day range, market-conditions read (informational, not a second trading gate), and open positions across every strategy and AI Origination.
- `/history` — filtered trade history plus performance KPIs and charts (equity curve, daily P&L, win/loss).
- `/smartapi-health` — pre-market health checks and broker connectivity status.
- `/control` — start, stop, restart, kill switch, and daily-lock reset.
- `/settings` — General, Strategies, Instruments, and AI tabs (see Settings above).
- `/reports` — on-demand daily/weekly/monthly/pattern-discovery/AI-origination-summary report generation.
- `/logs` — structured event logs.

## Daily Risk Lock

The platform computes cumulative daily P&L from completed trades. If it is at or below `Daily Max Loss %` (default `-20%`) for a strategy, the risk service disables new trades for that strategy, sends a Telegram alert, and shows a dashboard warning. The lock resets automatically before the first webhook of the next IST trading day; an admin can also reset it from `/control`.

## Timezone

User-facing timestamps are displayed in IST (`Asia/Kolkata`, UTC+05:30). SQLite does not reliably round-trip timezone info even on timezone-aware columns, so anything doing datetime arithmetic on a stored timestamp normalizes through `to_ist()` (`app/time_utils.py`) first.

## Tests

```bash
pytest
```

## Important Risk Notes

This bot can place live market orders when both live-trading switches are on (see [PAPER and LIVE Modes](#paper-and-live-modes)). Validate credentials, lot size, symbol selection, margin, order product type, and exchange holidays before enabling live mode on any strategy or index. Keep the app running during market hours so the monitor can exit positions — and remember that AI Origination trades on its own initiative, independent of any TradingView signal, once its own live-trading opt-in is on.
