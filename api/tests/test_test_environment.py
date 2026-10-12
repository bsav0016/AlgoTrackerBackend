"""Guards that keep the suite from touching live services, plus settings parsing."""
import socket

from django.conf import settings
from django.test import SimpleTestCase

from algo_tracker_backend.settings import parse_pairs


class TestIsolationTests(SimpleTestCase):
    def test_external_connections_are_blocked(self):
        with socket.socket() as sock, self.assertRaisesRegex(RuntimeError, "may not open network"):
            sock.connect(("8.8.8.8", 443))  # a literal IP: no DNS, nothing leaves the machine

    def test_market_data_websocket_and_api_hosts_are_unreachable(self):
        for host in ("api.twelvedata.com", "ws.twelvedata.com", "api.stripe.com", "exp.host"):
            with self.subTest(host=host), socket.socket() as sock:
                with self.assertRaises(RuntimeError):
                    sock.connect((host, 443))

    def test_credentials_and_services_are_test_values(self):
        self.assertEqual(settings.TWELVE_DATA_API_KEY, "test-twelve-data-key")
        self.assertTrue(settings.STRIPE_SECRET_KEY.startswith("sk_test_"))
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(settings.EMAIL_BACKEND, "django.core.mail.backends.locmem.EmailBackend")
        self.assertIn(":1/", settings.REDIS_URL)  # unreachable port, never a real Redis


class ParsePairsTests(SimpleTestCase):
    def test_parses_comma_separated_colon_pairs(self):
        self.assertEqual(parse_pairs("AAPL:1, MSFT:2"), [["AAPL", "1"], ["MSFT", "2"]])
        self.assertEqual(parse_pairs("1min:60:100"), [["1min", "60", "100"]])

    def test_blank_values_give_no_entries_instead_of_crashing(self):
        self.assertEqual(parse_pairs(""), [])
        self.assertEqual(parse_pairs(" , "), [])
        self.assertEqual(parse_pairs("AAPL:1,"), [["AAPL", "1"]])

    def test_test_configuration_was_parsed(self):
        self.assertEqual(settings.SYMBOLS, {"AAPL": 1.0, "BTC/USD": 24.0})
        self.assertEqual(settings.INTERVAL_MAP["1day"], {"seconds": 86400, "monthly_charge": 10})
        self.assertEqual(settings.PROMO_CODES, {"WELCOME": "50"})
