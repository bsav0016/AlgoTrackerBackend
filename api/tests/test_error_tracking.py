"""Opt-in Sentry setup: nothing starts without a DSN, and what is sent never carries credentials or personal data."""
import json
from unittest import mock

import sentry_sdk
from rest_framework.test import APITestCase
from sentry_sdk.transport import Transport

from algo_tracker_backend.error_tracking import (
    FILTERED, init_error_tracking, scrub_breadcrumb, scrub_event, scrub_string, sentry_options)
from api.models import CustomUser

DSN = "https://publickey@o0.ingest.sentry.io/1"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxfQ.signaturepartxyz"


class InitTests(APITestCase):
    def test_nothing_starts_without_a_dsn(self):
        for dsn in ("", "   ", None):
            with mock.patch("sentry_sdk.init") as init:
                self.assertFalse(init_error_tracking(dsn))
                init.assert_not_called()

    def test_with_a_dsn_it_starts_with_the_safe_options(self):
        with mock.patch("sentry_sdk.init") as init:
            self.assertTrue(init_error_tracking(DSN, environment="staging", release="abc123"))
        options = init.call_args.kwargs
        self.assertEqual((options["dsn"], options["environment"], options["release"]), (DSN, "staging", "abc123"))
        self.assertIs(options["send_default_pii"], False)
        self.assertIs(options["include_local_variables"], False)
        self.assertEqual(options["max_request_body_size"], "never")
        self.assertEqual(options["traces_sample_rate"], 0)
        self.assertIs(options["before_send"], scrub_event)
        self.assertIs(options["before_breadcrumb"], scrub_breadcrumb)
        self.assertEqual([type(i).__name__ for i in options["disabled_integrations"]], ["RedisIntegration"])
        self.assertEqual(sorted(type(i).__name__ for i in options["integrations"]),
                         ["DjangoIntegration", "LoggingIntegration", "RqIntegration"])

    def test_the_test_suite_never_has_a_dsn(self):
        from django.conf import settings
        self.assertFalse(sentry_sdk.get_client().is_active())
        self.assertTrue(hasattr(settings, "SECRET_KEY"))


class ScrubStringTests(APITestCase):
    def test_masks_keys_tokens_and_passwords(self):
        cases = {
            "GET https://api.twelvedata.com/time_series?apikey=abc123&symbol=AAPL":
                "GET https://api.twelvedata.com/time_series?apikey=[filtered]&symbol=AAPL",
            "password=\"hunter2\" failed": "password=\"[filtered]\" failed",
            "Invalid token: abcdefgh": "Invalid token: [filtered]",
            "Authorization: Bearer " + JWT: "Authorization: [filtered]",
            "header Bearer abcdefghijkl here": "header Bearer [filtered] here",
            # sk_test_ here: the repository's secret scan (rightly) rejects anything shaped like a live key.
            "stripe sk_test_abcdefgh1234 key": "stripe [filtered] key",
            "whsec_abcdefghij": "[filtered]",
            JWT: "[filtered]",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(scrub_string(text), expected)

    def test_leaves_ordinary_text_alone(self):
        for text in ("status code 500 for user 12", "Backtest market data unavailable for AAPL 1day",
                     "Error building response"):
            self.assertEqual(scrub_string(text), text)


class ScrubEventTests(APITestCase):
    def event(self):
        return {
            "message": "failed with apikey=SECRETKEY123",
            "server_name": "droplet-1",
            "request": {
                "url": "https://api.example.com/api/backtest/",
                "method": "POST",
                "query_string": "token=abc",
                "data": {"password": "hunter2"},
                "cookies": {"sessionid": "s3ss10n"},
                "env": {"REMOTE_ADDR": "203.0.113.9"},
                "headers": {"Authorization": "Bearer " + JWT, "Cookie": "sessionid=1", "X-Forwarded-For": "203.0.113.9",
                            "Content-Type": "application/json", "User-Agent": "AlgoTracker/1.0"},
            },
            "user": {"id": "7", "email": "a@example.com", "username": "alice", "ip_address": "203.0.113.9"},
            "exception": {"values": [{
                "type": "Exception", "value": "boom password=hunter2",
                "stacktrace": {"frames": [{"function": "f", "vars": {"password": "hunter2", "token": JWT}}]}}]},
            "breadcrumbs": {"values": [{"category": "query", "message": "token=abcdefgh"}]},
            "extra": {"api_key": "KEY123", "note": "fine"},
        }

    def test_credentials_and_personal_data_are_removed(self):
        scrubbed = scrub_event(self.event())
        request = scrubbed["request"]
        self.assertEqual(request["headers"], {"Content-Type": "application/json", "User-Agent": "AlgoTracker/1.0"})
        for gone in ("data", "cookies", "query_string", "env"):
            self.assertNotIn(gone, request)
        self.assertEqual(scrubbed["user"], {"id": "7"})
        self.assertNotIn("server_name", scrubbed)
        self.assertNotIn("vars", scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0])
        self.assertEqual(request["url"], "https://api.example.com/api/backtest/")
        self.assertEqual(request["method"], "POST")

    def test_secrets_in_any_string_are_masked_and_nothing_else_is_lost(self):
        scrubbed = scrub_event(self.event())
        self.assertEqual(scrubbed["message"], "failed with apikey=[filtered]")
        self.assertEqual(scrubbed["exception"]["values"][0]["value"], "boom password=[filtered]")
        self.assertEqual(scrubbed["breadcrumbs"]["values"][0]["message"], "token=[filtered]")
        self.assertEqual(scrubbed["extra"], {"api_key": FILTERED, "note": "fine"})
        dumped = json.dumps(scrubbed)
        for secret in ("SECRETKEY123", "hunter2", "s3ss10n", "203.0.113.9", "a@example.com", "alice", "KEY123", JWT):
            self.assertNotIn(secret, dumped)

    def test_an_event_without_a_request_or_user_is_fine(self):
        self.assertEqual(scrub_event({"message": "hello"}), {"message": "hello"})
        self.assertEqual(scrub_event({"user": {"email": "a@example.com"}}), {"user": {}})

    def test_breadcrumbs_that_record_commands_or_urls_are_dropped(self):
        for category in ("redis", "httplib", "subprocess"):
            self.assertIsNone(scrub_breadcrumb({"category": category, "message": "x"}))
        self.assertEqual(scrub_breadcrumb({"category": "query", "message": "ok"}), {"category": "query", "message": "ok"})


class CaptureTransport(Transport):
    """Collects what would have been sent, in place of the network."""

    def __init__(self, options=None):
        super().__init__(options)
        self.events = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") == "event":
                self.events.append(item.payload.json)


class EndToEndTests(APITestCase):
    """A real failing request, through the real Sentry client, ends up at the (fake) transport without secrets."""

    def setUp(self):
        self.transport = CaptureTransport()
        options = sentry_options(DSN, environment="test")
        options["transport"] = self.transport
        sentry_sdk.init(**options)
        self.addCleanup(self.stop_sentry)
        self.user = CustomUser.objects.create_user(
            username="alice", email="alice@example.com", password="x", first_name="A", last_name="B")
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + JWT)

    @staticmethod
    def stop_sentry():
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)
        sentry_sdk.get_isolation_scope().set_client(None)
        sentry_sdk.get_current_scope().set_client(None)

    def test_a_failing_backtest_is_reported_without_the_token_the_body_or_the_key(self):
        payload = {
            "symbol": "AAPL", "interval": "1day", "start_date": "2024-01-01T00:00:00Z",
            "end_date": "2024-02-01T00:00:00Z", "signals": [], "password": "hunter2-body-secret",
        }
        boom = Exception("market data failed apikey=SUPERSECRETKEY123")
        with mock.patch("api.views.BacktestService.conduct_backtest", side_effect=boom):
            self.client.post("/api/backtest/", payload, format="json")
        sentry_sdk.flush()
        self.assertTrue(self.transport.events, "the error was not reported at all")
        dumped = json.dumps(self.transport.events)
        for secret in ("SUPERSECRETKEY123", "hunter2-body-secret", JWT, "alice@example.com"):
            self.assertNotIn(secret, dumped)
        event = self.transport.events[0]
        self.assertEqual(event["environment"], "test")
        self.assertNotIn("data", event.get("request", {}))
        self.assertNotIn("Authorization", event.get("request", {}).get("headers", {}))

