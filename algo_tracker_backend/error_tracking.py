"""Opt-in error tracking with Sentry.

Nothing is sent unless SENTRY_DSN is set. When it is, the setup is deliberately conservative, because this service
handles sign-in tokens, password-reset codes, payment webhooks and a market-data API key:

* no personal data by default (`send_default_pii=False`), no request bodies, no local variables in stack traces;
* `before_send` removes the Authorization and Cookie headers, request data, cookies, query strings and the client
  address, keeps only the user's id, and masks anything in any string that looks like a token, key or password;
* Redis breadcrumbs (they hold reset codes) and outgoing-HTTP breadcrumbs (their URLs hold the TwelveData key) are
  dropped, and the Redis integration is switched off;
* no performance tracing.

`scrub_event` is pure so it can be tested without Sentry. settings.py calls `init_error_tracking` once; the web
server, the RQ workers, the sweeper, the leaderboard job and the websocket worker all import settings, so one call
covers all of them.
"""
import re

FILTERED = "[filtered]"

# Headers that carry credentials or identify a client.
SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-csrftoken", "x-forwarded-for", "x-real-ip",
    "x-revenuecat-signature", "stripe-signature",
}

# key=value, key: value and "key": "value" pairs whose key names a secret.
_KEYED_SECRET = re.compile(
    r"""(?ix)
    (?P<key>api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|authorization|signature)
    (?P<sep>["']?\s*[=:]\s*["']?)
    (?P<value>(?!\[filtered\])(?:(?:bearer|token)\s+)?[^\s&"',;)}\]]+)
    """
)
# "Bearer <token>" (or "Token <token>") wherever it appears.
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+[\w.~+/=-]{8,}")
# A JWT, a Stripe key or webhook secret, whatever it is called.
_BARE_SECRET = re.compile(r"eyJ[\w-]{8,}\.[\w-]+\.[\w-]+|\b(?:sk|rk|whsec)_[A-Za-z0-9_]{8,}")

# Breadcrumb categories that record commands or URLs that can contain secrets.
_DROPPED_BREADCRUMBS = {"redis", "httplib", "subprocess"}


def scrub_string(text):
    """Masks secrets in a string. Running it again on its own output changes nothing."""
    text = _KEYED_SECRET.sub(lambda m: f"{m.group('key')}{m.group('sep')}{FILTERED}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {FILTERED}", text)
    return _BARE_SECRET.sub(FILTERED, text)


def _scrub(value):
    if isinstance(value, str):
        return scrub_string(value)
    if isinstance(value, dict):
        return {k: (FILTERED if isinstance(k, str) and _is_secret_key(k) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _is_secret_key(key):
    key = key.lower()
    return any(word in key for word in ("password", "secret", "token", "apikey", "api_key", "authorization", "cookie"))


def scrub_event(event, hint=None):
    """The `before_send` hook: returns the event with credentials and personal data removed."""
    request = event.get("request")
    if request:
        for field in ("data", "cookies", "query_string", "env"):
            request.pop(field, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}
    user = event.get("user")
    if user:
        event["user"] = {"id": user["id"]} if "id" in user else {}
    # Local variables are not collected (include_local_variables=False); drop them if one slips in anyway.
    for exception in (event.get("exception") or {}).get("values", []):
        for frame in (exception.get("stacktrace") or {}).get("frames", []):
            frame.pop("vars", None)
    event.pop("server_name", None)
    return _scrub(event)


def scrub_breadcrumb(crumb, hint=None):
    """The `before_breadcrumb` hook: drops breadcrumbs that record commands or URLs, masks the rest."""
    if crumb.get("category") in _DROPPED_BREADCRUMBS:
        return None
    return _scrub(crumb)


def sentry_options(dsn, environment="production", release=None):
    """The keyword arguments for sentry_sdk.init (separate so tests can inspect them)."""
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.redis import RedisIntegration
    from sentry_sdk.integrations.rq import RqIntegration

    return {
        "dsn": dsn,
        "environment": environment,
        "release": release or None,
        "integrations": [DjangoIntegration(), RqIntegration(), LoggingIntegration(level=None, event_level=40)],
        # Redis commands (reset codes, tokens) must never reach Sentry.
        "disabled_integrations": [RedisIntegration()],
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "traces_sample_rate": 0,
        "before_send": scrub_event,
        "before_breadcrumb": scrub_breadcrumb,
    }


def init_error_tracking(dsn, environment="production", release=None):
    """Starts Sentry if there is a DSN. Returns whether it did."""
    if not (dsn or "").strip():
        return False
    import sentry_sdk
    sentry_sdk.init(**sentry_options(dsn.strip(), environment, release))
    return True
