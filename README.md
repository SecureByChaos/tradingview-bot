# banknifty-trading-bot

Production-ready FastAPI webhook bot for BankNifty option buying signals from TradingView, with Angel One SmartAPI execution, active-trade persistence, scheduled monitoring, and CSV trade logs.

## Architecture

```text
TradingView -> Webhook -> FastAPI -> Trade Manager -> Angel One SmartAPI
                                      -> Trade Monitor -> CSV Trade Logger
```

## Features

- `POST /webhook` accepts only `BUY_CE` and `BUY_PE`.
- Single-admin login with signed session cookies.
- Bootstrap 5 dark dashboard with bot status, active trade, history, controls, settings, and logs.
- SQLite-backed platform state for bot status, settings, daily stats, trade records, and structured logs.
- REST API under `/api/*` for status, trades, settings, bot controls, kill switch, and daily-lock reset.
- Telegram notifications for bot events, trade events, exits, risk locks, and system errors.
- Automatically selects the current ATM BankNifty CE/PE from live BankNifty spot.
- One open trade at a time.
- Maximum two completed trades per day.
- Stops trading after two same-day stoploss exits.
- Entry risk management: 10% stoploss and 20% target.
- Monitors active trades every 30 seconds.
- Squares off open positions at 15:15 IST.
- Persists active trade state in `data/active_trade.json`.
- Logs completed trades to `data/trades.csv`.
- Safe-by-default paper mode via `SMARTAPI_LIVE_TRADING=false`.

## Local Setup

Python 3.11+ is recommended. The Docker image uses Python 3.12.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
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
curl -X POST http://localhost:8000/webhook ^
  -H "Content-Type: application/json" ^
  -d "{\"signal\":\"BUY_CE\"}"
```

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `SMARTAPI_API_KEY` | Angel One SmartAPI app API key |
| `SMARTAPI_CLIENT_ID` | Angel One client ID |
| `SMARTAPI_PIN` | Angel One PIN |
| `SMARTAPI_TOTP_SECRET` | TOTP secret from SmartAPI setup |
| `SMARTAPI_LIVE_TRADING` | Set `true` only when ready for real orders |
| `ADMIN_USERNAME` | Dashboard admin username |
| `ADMIN_PASSWORD` | Dashboard admin password as a bcrypt hash |
| `SESSION_SECRET_KEY` | Long random secret for signed session cookies |
| `SECURE_COOKIES` | Set `true` when serving over HTTPS |
| `DATABASE_URL` | SQLAlchemy DB URL, defaults to SQLite in `data/` |
| `QUANTITY_LOTS` | Number of BankNifty lots to trade |
| `BANKNIFTY_LOT_SIZE` | Current BankNifty lot size from NSE/broker contract specs |
| `BANKNIFTY_SPOT_TOKEN` | SmartAPI token for BankNifty index |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

## SmartAPI Setup

1. Create an Angel One SmartAPI app and collect the API key.
2. Enable TOTP for the trading account and store the TOTP secret in `.env`.
3. Confirm `BANKNIFTY_LOT_SIZE` against the current exchange contract specification.
4. Start with `SMARTAPI_LIVE_TRADING=false`.
5. After paper testing, set `SMARTAPI_LIVE_TRADING=true` and restart the app.

The code uses Angel One's SmartAPI Python SDK through a local adapter in `app/smartapi_client.py`. This keeps broker-specific behavior isolated for future changes.

## TradingView Webhook Setup

Create a TradingView alert with webhook URL:

```text
http://YOUR_SERVER_IP:8000/webhook
```

Alert message for CE:

```json
{"signal":"BUY_CE"}
```

Alert message for PE:

```json
{"signal":"BUY_PE"}
```

Signals should already be time-filtered in TradingView. The Python bot only enforces risk, daily limits, one-position-at-a-time behavior, and 15:15 IST square-off.

## AWS Lightsail Deployment

1. Create an Ubuntu Lightsail instance.
2. Open port `8000` in the Lightsail firewall, or place the app behind Nginx with HTTPS.
3. Install Docker:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker ubuntu
```

4. Copy the repository to the server and create `.env`:

```bash
cp .env.example .env
nano .env
```

5. Start the service:

```bash
docker compose up -d --build
docker compose logs -f
```

6. Verify:

```bash
curl http://YOUR_SERVER_IP:8000/health
```

For production exposure, terminate TLS with Nginx or a Lightsail load balancer and restrict webhook access where possible.

## API

Dashboard pages require login at `/login`.

### `GET /health`

Returns service status and live-trading mode.

### `GET /active-trade`

Returns the persisted active trade, or `null`.

### `GET /trades`

Returns completed trade rows from `data/trades.csv`.

### `POST /webhook`

Payload:

```json
{"signal":"BUY_CE"}
```

or:

```json
{"signal":"BUY_PE"}
```

### Admin REST API

Authenticated session required:

- `GET /api/status`
- `GET /api/active-trade`
- `GET /api/trades`
- `GET /api/settings`
- `POST /api/settings`
- `POST /api/start`
- `POST /api/stop`
- `POST /api/restart`
- `POST /api/kill-switch`
- `POST /api/reset-daily-lock`

## Dashboard

- `/` shows status, daily stats, active trade, risk status, and recent logs.
- `/active-trade-page` shows live active trade details.
- `/history` shows filtered trade history.
- `/control` provides start, stop, restart, kill switch, and daily-lock reset.
- `/settings` persists trading, risk, square-off, and Telegram settings in SQLite.
- `/logs` shows structured event logs.

## Daily Risk Lock

The platform computes cumulative daily P&L from completed trades. If it is less than or equal to `Daily Max Loss %` (default `-20%`), the risk service closes the active position, disables new trades, sets bot status to `RISK_LOCKED`, sends a Telegram alert, and shows a dashboard warning. Admin reset is required from `/control`.

## Tests

```bash
pytest
```

## Important Risk Notes

This bot can place live market orders when `SMARTAPI_LIVE_TRADING=true`. Validate credentials, lot size, symbol selection, margin, order product type, and exchange holidays before enabling live mode. Keep the app running during market hours so the monitor can exit positions.
