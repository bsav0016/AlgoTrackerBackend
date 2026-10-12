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

## Running it

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

- No automated tests: `api/tests.py` is empty.
- Deploys are manual and there is no CI.
- Prices come from a third-party feed (TwelveData); symbols are limited to the configured `SYMBOLS` list.
- Backtests are simulations on historical data and do not model fees or slippage. <!-- TODO: confirm; I did not see fee or slippage handling in backtest_service.py -->
- <!-- TODO: is the production deployment still running? Last upstream commit before this work was May 2025. -->
