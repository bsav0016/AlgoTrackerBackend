"""Rate limits for the endpoints that take credentials or send email.

Each class counts requests in the Django cache (Redis in production) under its own key. The rates live in
`settings.AUTH_THROTTLE_RATES`, read at request time so a test can override them.

A throttle must never take login down with it: if the cache is unreachable the request is allowed and the
failure is logged.
"""
import logging

from django.conf import settings
from django.core.cache import caches
from rest_framework.throttling import SimpleRateThrottle

logger = logging.getLogger(__name__)


class _FailOpenThrottle(SimpleRateThrottle):
    cache = caches["throttle"]

    def get_rate(self):
        return settings.AUTH_THROTTLE_RATES[self.scope]

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception("Rate limit check failed for %s; allowing the request", self.scope)
            return True


class _ByAddress(_FailOpenThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _ByField(_FailOpenThrottle):
    """Counts per value of a request field (a username or email), so spreading attempts over many addresses
    does not help against one account. No field, no key, no limit."""
    field = ""

    def get_cache_key(self, request, view):
        data = request.data if hasattr(request.data, "get") else {}
        value = data.get(self.field)
        if not isinstance(value, str) or not value.strip():
            return None
        return self.cache_format % {"scope": self.scope, "ident": value.strip().lower()}


class LoginAddressThrottle(_ByAddress):
    scope = "login_address"


class LoginUsernameThrottle(_ByField):
    scope = "login_username"
    field = "username"


class RegisterAddressThrottle(_ByAddress):
    scope = "register_address"


class SharedStrategyAddressThrottle(_ByAddress):
    """The share page is public, so it is limited per address to stop link-guessing and scraping."""
    scope = "shared_view_address"


class PasswordResetAddressThrottle(_ByAddress):
    scope = "reset_address"


class PasswordResetEmailThrottle(_ByField):
    """Stops one inbox being flooded with reset emails."""
    scope = "reset_email"
    field = "email"


class PasswordResetConfirmAddressThrottle(_ByAddress):
    scope = "reset_confirm_address"


class PasswordResetConfirmUsernameThrottle(_ByField):
    scope = "reset_confirm_username"
    field = "username"
