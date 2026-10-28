"""Settings for the automated test suite (used by `python manage.py test`).

Everything that could touch a real service is forced to a harmless value, and
overrides any developer .env, so tests can never use live market data, real
payments or push notifications.
"""
import os

# Assigned (not setdefault) so a local .env or shell variable cannot leak in.
os.environ.update({
    "DEBUG": "True",
    "SECRET_KEY": "test-secret-key-not-for-production",
    # In-memory SQLite unless TEST_DATABASE_URL names a Postgres (CI does, for the row-lock tests).
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "sqlite://:memory:"),
    # Nothing listens on port 1, so any unmocked Redis call fails loudly.
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "TWELVE_DATA_API_KEY": "test-twelve-data-key",
    "STRIPE_SECRET_KEY": "sk_test_not_real",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_not_real",
    "REVENUECAT_WEBHOOK_AUTH": "test-revenuecat-webhook-secret",
    "SYMBOLS": "AAPL:1,BTC/USD:24",
    "INTERVALS": "1min:60:100,1day:86400:10",
    "PROMO_CODES": "WELCOME:50",
    "PROMO_CODE_AVAILABILITY": "True",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST": "noreply@example.com",
    "MONTHLY_CREDITS": "500",
    "BACKTEST_BASE_COST": "3",
    "BACKTEST_ITERATIONS": "5",
})
os.environ.pop("FIREBASE_CREDENTIALS_PATH", None)

from .settings import *  # noqa: E402,F401,F403

# The rate limits count in the "throttle" cache; keep it in process, and make the limits too high to get in
# the way of the other tests (the throttle tests set their own).
CACHES["throttle"] = {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
AUTH_THROTTLE_RATES = {scope: "100000/min" for scope in AUTH_THROTTLE_RATES}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
TEST_RUNNER = "algo_tracker_backend.test_runner.NoExternalNetworkRunner"

# Expected errors (the tests exercise failure paths) would otherwise fill the test output.
LOGGING["handlers"]["console"]["level"] = "CRITICAL"
