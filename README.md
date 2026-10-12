# AlgoTracker – Backend

Django REST API for [AlgoTracker](https://github.com/bsav0016/AlgoTrackerFrontend), a mobile app for building indicator-based trading strategies, back-testing them, and subscribing to live signals. The API handles accounts and payments, runs backtests, and watches live prices so it can push a notification when a subscribed strategy signals a trade.

<!-- TODO: add an architecture diagram (app -> API -> RQ worker / websocket worker -> Postgres + Redis) -->

## Why it exists

Back-testing a strategy and watching live prices around the clock are too heavy and too continuous for a phone app. This API replays strategies over historical prices on demand and keeps a live price feed running, so subscribed strategies can notify users when they signal even when the app is closed.

## What it does

- **Accounts** – register, login/logout (JWT via `djangorestframework-simplejwt`), password reset by email, account deletion, promo codes
- **Strategies** – a strategy is a symbol, an interval, and lists of buy and sell signals built from indicators (RSI, SMA, EMA, MACD, Stochastic Oscillator, Bollinger %B, ADX)
- **Backtesting** (`api/services/backtest_service.py`) – fetches historical prices, finds buy and sell points from the signals, and computes returns, Sharpe and Sortino ratios, and annual percent return against the asset itself; produces charts with matplotlib
- **Live signals** – a separate worker keeps a websocket open to the TwelveData price feed; new prices are handed to an RQ job that checks each subscribed strategy and updates its position
- **Push notifications** – sent to a user's device through Expo's push service when a strategy changes position, plus Firebase for Android
- **Credits and payments** – backtests and monthly subscriptions are charged in credits; credits are bought with Stripe payment intents or through RevenueCat (store purchases)

## Stack

- Python 3.12, Django, Django REST Framework, simplejwt
- PostgreSQL, Redis, django-rq (background jobs)
- pandas, numpy, matplotlib
- Stripe, RevenueCat, TwelveData, Firebase / Expo push, SMTP email
- Docker Compose, Caddy (reverse proxy and HTTPS), gunicorn

## Architecture

`docker-compose.yml` runs six services:

| Service | Role |
| --- | --- |
| `web` | Django API (gunicorn) |
| `worker-tasks` | `python manage.py rqworker default` – runs strategy checks |
| `worker-websocket` | `python websocket_worker.py` – TwelveData listener with automatic reconnect |
| `db` | PostgreSQL 16 |
| `redis` | Redis 7 – queue and cache |
| `caddy` | Reverse proxy and TLS for `DOMAIN` |

## Local development

Runs the API directly on your machine (no Docker). Needs Python 3.12. The commands below are for a POSIX shell; on Windows PowerShell activate the venv with `.venv\Scripts\Activate.ps1` and use `Copy-Item` instead of `cp`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` (it is git-ignored). For local work you only need:

```
DEBUG=True
SECRET_KEY=any-local-string
ALLOWED_HOSTS=localhost,127.0.0.1
SYMBOLS=AAPL:1,BTC/USD:24
INTERVALS=1min:60:100,1day:86400:10
```

Add `TWELVE_DATA_API_KEY` to run backtests against real prices. With `DEBUG=True` the database defaults to a local SQLite file (`db.sqlite3`, set `DATABASE_URL` to use Postgres instead) and Firebase credentials are optional. With `DEBUG=False` Firebase credentials are required, as in production.

Redis is used for password-reset codes, cached prices and background jobs. Start one if you need those (`docker compose up -d redis` works, or any Redis on `localhost:6379`; override with `REDIS_URL`).

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver            # API on http://localhost:8000
python manage.py rqworker default     # optional: strategy checks (needs Redis)
python websocket_worker.py            # optional: live TwelveData prices (needs Redis and an API key)
```

Point the mobile app at `http://<your machine>:8000/` (see the frontend README).

## Tests

```bash
python manage.py test
```

`manage.py test` automatically uses `algo_tracker_backend.test_settings`: fake API keys, in-memory SQLite, an unreachable Redis, and a guard that fails any test that tries to open a non-local network connection. It ignores your `.env`, needs no services or secrets, and never contacts TwelveData, Stripe or Expo; those are mocked. Run one module with `python manage.py test api.tests.test_backtest_service`.

The suite covers the indicators, backtest math, live signal handling and billing, and the account, backtest, strategy and payment APIs.

Static checks use Ruff (`pip install -r requirements-dev.txt`, then `ruff check --select E9,F63,F7,F82 .`, which flags syntax errors and undefined names).

### CI

`.github/workflows/ci.yml` runs on pull requests and on pushes to `master`, without any secrets. The `backend` job runs `pip check`, a check for hard-coded private URLs or secret keys, Ruff, `manage.py check`, a check that model changes have migrations, and the tests. The `docker` job validates `docker-compose.yml` with placeholder values, builds the image, and runs the tests inside it. Deployment stays manual (see below). A `.dockerignore` keeps `.env`, credentials and `.git` out of the image.

## Running it with Docker

1. Copy `.env.example` to `.env` and fill in values (TwelveData API key, Stripe keys, email settings, `SECRET_KEY`, etc.).
2. Put your Firebase service-account JSON at the path in `FIREBASE_CREDENTIALS_PATH`. It is git-ignored and bind-mounted into the containers.
3. Start the stack:
   ```bash
   docker compose up --build
   ```
4. First run only:
   ```bash
   docker compose exec web python manage.py migrate
   docker compose exec web python manage.py createsuperuser
   ```
5. Point the app's `API_URL` at the server (for example `http://localhost:8000`, or your `DOMAIN`).

Production runs the same compose file on a single DigitalOcean droplet; deploys are manual (`git pull`, `docker compose up --build -d`).

## Design decisions

- **Environment-driven settings.** One settings file reads `DEBUG`, `ALLOWED_HOSTS`, `DATABASE_URL` and the rest from the environment, instead of per-host branches. This replaced earlier Heroku and AWS branches.
- **RQ instead of Celery** for background jobs, to keep the moving parts down (it only needs Redis, which is already used for caching).
- **Separate websocket worker.** The price feed is a long-lived connection, so it runs outside the request/response API and reconnects on its own after errors.
- **Pricing is configuration.** Backtest cost, iterations, monthly credits, symbols and intervals come from environment variables and are served to the app.
- **Two payment paths** (Stripe and RevenueCat) both end in the same credit balance.

## Limitations

- Tests cover the core logic and API but not the websocket worker, the Redis price loader, or the Expo push client.
- Deploys are manual; CI only runs checks and tests.
- `requirements.txt` pins direct dependencies only, so transitive versions can still drift between builds.
- RevenueCat webhook deliveries are not authenticated or de-duplicated (see the expected-failure test in `api/tests/test_api_payments.py`).
- Prices come from a third-party feed (TwelveData); symbols are limited to the configured `SYMBOLS` list.
- Backtests are simulations on historical data and do not model fees or slippage. <!-- TODO: confirm; I did not see fee or slippage handling in backtest_service.py -->
- <!-- TODO: is the production deployment still running? Last upstream commit before this work was May 2025. -->
