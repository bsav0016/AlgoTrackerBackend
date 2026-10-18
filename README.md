# AlgoTracker – Backend

Django REST API for [AlgoTracker](https://github.com/bsav0016/AlgoTrackerFrontend), a mobile app for building indicator-based trading strategies, back-testing them, and subscribing to live signals. The API handles accounts and payments, runs backtests, and watches live prices so it can push a notification when a subscribed strategy signals a trade.

## Project status

**This is a portfolio project and it is not currently deployed.** The production server is paused, so there is no hosted demo and no live API to call. The code is published to show how the system is designed and tested.

- **What works without any accounts or keys:** the whole automated test suite (`python manage.py test`) and CI run with fake settings, an in-memory database and no network access. Nothing in the tests contacts TwelveData, Stripe, RevenueCat, Firebase or an email server.
- **What needs your own accounts to run for real:** live prices (TwelveData), card payments (Stripe), store purchases (RevenueCat), push notifications (Firebase and Expo) and outgoing email (SMTP). You supply your own keys; none are in this repository.
- **No credentials are stored here.** Every secret and environment-specific value is read from environment variables. `.env.example` lists them with empty or placeholder values. The TwelveData API key used during development was regenerated on 2026-10-13, so a key from any earlier version of this repository no longer works.
- **The mobile app** that talks to this API is in [AlgoTrackerFrontend](https://github.com/bsav0016/AlgoTrackerFrontend).

## Why it exists

Back-testing a strategy and watching live prices around the clock are too heavy and too continuous for a phone app. This API replays strategies over historical prices on demand and keeps a live price feed running, so subscribed strategies can notify users when they signal even when the app is closed.

## What it does

- **Accounts** – register, login/logout (JWT via `djangorestframework-simplejwt`), password reset by email, account deletion, promo codes
- **Strategies** – a strategy is a symbol, an interval, and lists of buy and sell signals built from indicators (RSI, SMA, EMA, MACD, Stochastic Oscillator, Bollinger %B, ADX)
- **Backtesting** (`api/services/backtest_service.py`) – fetches historical prices, finds buy and sell points from the signals, and computes returns, Sharpe and Sortino ratios, and annual percent return against the asset itself; produces charts with matplotlib
- **Live signals** – a separate worker keeps a websocket open to the TwelveData price feed; price ticks are folded into open/high/low/close candles per interval, and each finished candle is handed to an RQ job that checks each subscribed strategy and updates its position. The job locks the strategy and records the last candle it handled, so a repeated or out-of-order job does not trade or notify twice
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

The suite covers the indicators, backtest math, live signal handling (including candle building from ticks, running a job twice or out of order, and the path from a stored candle through the Redis candle list and the queued job to a buy or sell, run against an in-memory Redis) and billing, and the account (including the password-reset flow, that reset codes are never logged, and the wrong-guess limit), backtest, strategy and payment APIs.

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

## Payments

Backtests and subscriptions cost credits, and credits can be bought two ways. Both end in the same balance (`add_funds` on the user).

| Path | How it works | How the server trusts it |
| --- | --- | --- |
| **Stripe** | The app asks the API for a payment (`Customer`, ephemeral key and `PaymentIntent`), pays in Stripe's payment sheet, and Stripe calls the API's webhook when the payment succeeds. | The webhook signature is verified with `STRIPE_WEBHOOK_SECRET`. A bad signature is rejected and nothing is credited. |
| **RevenueCat** (store purchases) | RevenueCat calls the API's confirmation webhook after a store purchase, and the app registers its RevenueCat customer id. | The webhook must carry a shared secret in its `Authorization` header (`REVENUECAT_WEBHOOK_AUTH`). With no secret configured, every request is rejected. |

Stripe notes for anyone running this themselves:

- Use a **Stripe key that belongs to this app only**, not one shared with other projects. A restricted key is enough: the code creates customers, ephemeral keys and payment intents, so give it only the permissions for those (customers and payment intents write) and check the exact list in Stripe's test mode.
- Give the webhook endpoint **its own signing secret** (`STRIPE_WEBHOOK_SECRET`) so it can be rotated independently.
- Start in test mode. The tests use fake keys and mock Stripe, so they never call it.

## Before going live again

The production server is currently paused. When it is brought back up:

- [ ] Set `REVENUECAT_WEBHOOK_AUTH` in the server's `.env` (a long random value). Until it is set, the RevenueCat webhook rejects every request, so purchases will not credit.
- [ ] In RevenueCat, open Integrations > Webhooks and set the webhook URL to the server's `/api/revenuecat-confirmation/` and the **Authorization header value** to the same string. The webhook was last configured with an old Heroku address that no longer responds.
- [ ] Make a sandbox purchase and confirm the credits arrive.
- [ ] Build a **fresh server** rather than resuming an old one. Use a **new** Django `SECRET_KEY`, and a **new** SSH key pair (never reuse earlier ones), and set the other variables from `.env.example`. The TwelveData API key was already regenerated on 2026-10-13; put the current one in the server's environment.
- [ ] Create a Stripe key for this app only, and a webhook endpoint with its own signing secret (see Payments).
- [ ] Check that the app's production API address points at the live server.

## Design decisions

- **Environment-driven settings.** One settings file reads `DEBUG`, `ALLOWED_HOSTS`, `DATABASE_URL` and the rest from the environment, instead of per-host branches. This replaced earlier Heroku and AWS branches.
- **CORS is closed by default.** The only client is the native Expo app, which does not use CORS, so production allows no cross-origin browser requests. List origins in `CORS_ALLOWED_ORIGINS` if a web client is ever added; `DEBUG=True` allows every origin for local experiments.
- **RQ instead of Celery** for background jobs, to keep the moving parts down (it only needs Redis, which is already used for caching).
- **Separate websocket worker.** The price feed is a long-lived connection, so it runs outside the request/response API and reconnects on its own after errors.
- **Pricing is configuration.** Backtest cost, iterations, monthly credits, symbols and intervals come from environment variables and are served to the app.
- **Password-reset codes are secrets.** A code is six digits from Python's `secrets` module, kept in Redis for 10 minutes, single use, never printed or logged (a test checks this), and thrown away after 5 wrong guesses (HTTP 429), so the code cannot be brute-forced; the user then requests a new one.
- **Two payment paths** (Stripe and RevenueCat) both end in the same credit balance.
- **Webhooks are authenticated.** Stripe events are verified with the Stripe signature. The RevenueCat webhook has no signature, so it requires a shared secret in the `Authorization` header (`REVENUECAT_WEBHOOK_AUTH`), compared in constant time. With no secret configured it rejects every request, so a missing setting can never allow free credits.

## Limitations

- Tests cover the core logic and API but not the websocket worker, the TwelveData history loader, or the Expo push client. The live signal path is tested from `RedisManager.record_tick` onward. Gaps in the feed leave missing candles (empty intervals are skipped, not filled). A candle that was open while the worker was down is stored with only the ticks it saw. History loaded from TwelveData has closes only, so Stochastic Oscillator and ADX strategies only signal once enough live candles have built up.
- Deploys are manual; CI only runs checks and tests.
- Views, the Redis price loader and settings log through Python's `logging`; the websocket client and the Expo push client still use `print`, so their output is not leveled or routed by the logging config.
- `requirements.txt` pins direct dependencies only, so transitive versions can still drift between builds.
- RevenueCat webhook deliveries are not de-duplicated: a processed transaction is deleted, so a retried delivery could credit twice (see the expected-failure test in `api/tests/test_api_payments.py`).
- Prices come from a third-party feed (TwelveData); symbols are limited to the configured `SYMBOLS` list.
- Backtests are simulations on historical data and do not model fees or slippage.

## License

MIT. See [LICENSE](LICENSE).
