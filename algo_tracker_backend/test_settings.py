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
    "DATABASE_URL": "sqlite://:memory:",
    # Nothing listens on port 1, so any unmocked Redis call fails loudly.
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "TWELVE_DATA_API_KEY": "test-twelve-data-key",
    "STRIPE_SECRET_KEY": "sk_test_not_real",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_not_real",
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

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
TEST_RUNNER = "algo_tracker_backend.test_runner.NoExternalNetworkRunner"
