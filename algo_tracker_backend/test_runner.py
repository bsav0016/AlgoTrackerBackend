import ipaddress
import socket
from unittest import mock

from django.test.runner import DiscoverRunner

_real_connect = socket.socket.connect


def _is_local(address):
    if isinstance(address, str):  # unix socket path
        return True
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname that needs DNS is never local


def _guarded_connect(self, address):
    if not _is_local(address):
        raise RuntimeError(
            f"Tests may not open network connections (tried {address!r}). "
            "Mock the external service instead."
        )
    return _real_connect(self, address)


class NoExternalNetworkRunner(DiscoverRunner):
    """Fails any test that tries to reach a non-loopback host (TwelveData, Stripe, Expo...)."""

    def run_tests(self, *args, **kwargs):
        with mock.patch.object(socket.socket, "connect", _guarded_connect):
            return super().run_tests(*args, **kwargs)
