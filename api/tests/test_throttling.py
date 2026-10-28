"""Rate limits on login, registration and password reset, and sessions ending on a password reset."""
from unittest import mock

from django.core import mail
from django.core.cache import caches
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from api.tests.helpers import FakeRedis
from api.tests.test_api_accounts import REGISTER, make_user

RATES = {
    "login_address": "3/min",
    "login_username": "100/hour",
    "register_address": "2/hour",
    "reset_address": "100/hour",
    "reset_email": "2/hour",
    "reset_confirm_address": "100/hour",
    "reset_confirm_username": "3/hour",
}


@override_settings(AUTH_THROTTLE_RATES=RATES)
class ThrottleTestCase(APITestCase):
    def setUp(self):
        caches["throttle"].clear()
        self.addCleanup(caches["throttle"].clear)


class LoginThrottleTests(ThrottleTestCase):
    def login(self, username="alice", password="wrong-pass", **extra):
        return self.client.post("/api/login/", {"username": username, "password": password}, format="json", **extra)

    def test_repeated_attempts_from_one_address_are_refused(self):
        make_user()
        statuses = [self.login().status_code for _ in range(5)]
        self.assertEqual(statuses[:3], [status.HTTP_403_FORBIDDEN] * 3)
        self.assertEqual(statuses[3:], [status.HTTP_429_TOO_MANY_REQUESTS] * 2)

    def test_the_right_password_is_refused_too_once_the_limit_is_hit(self):
        make_user()
        for _ in range(3):
            self.login()
        self.assertEqual(self.login(password="S3cure-pass!").status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_another_address_is_not_affected(self):
        make_user()
        for _ in range(4):
            self.login(REMOTE_ADDR="10.0.0.1")
        self.assertEqual(self.login(password="S3cure-pass!", REMOTE_ADDR="10.0.0.2").status_code,
                         status.HTTP_200_OK)

    def test_the_client_address_comes_from_the_proxy_header(self):
        """Behind Caddy REMOTE_ADDR is always the proxy; the client is the last X-Forwarded-For entry."""
        make_user()
        for _ in range(4):
            self.login(REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7")
        other = self.login(password="S3cure-pass!", REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR="203.0.113.8")
        self.assertEqual(other.status_code, status.HTTP_200_OK)
        same = self.login(password="S3cure-pass!", REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.assertEqual(same.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_a_forged_header_value_does_not_buy_a_fresh_allowance(self):
        """The client can prepend entries; only the one the proxy appended counts."""
        make_user()
        for i in range(4):
            self.login(REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR=f"198.51.100.{i}, 203.0.113.7")
        response = self.login(REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR="198.51.100.99, 203.0.113.7")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_settings(AUTH_THROTTLE_RATES={**RATES, "login_address": "100/min", "login_username": "3/hour"})
    def test_guesses_at_one_account_are_limited_across_addresses(self):
        make_user()
        for i in range(3):
            self.login(REMOTE_ADDR=f"10.0.0.{i}")
        blocked = self.login(password="S3cure-pass!", REMOTE_ADDR="10.0.0.99")
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_settings(AUTH_THROTTLE_RATES={**RATES, "login_address": "100/min", "login_username": "3/hour"})
    def test_the_username_limit_ignores_letter_case_and_spaces(self):
        make_user()
        for name in ("alice", "ALICE", " Alice "):
            self.login(username=name)
        self.assertEqual(self.login(username="aLiCe").status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_settings(AUTH_THROTTLE_RATES={**RATES, "login_address": "100/min", "login_username": "3/hour"})
    def test_other_accounts_are_not_locked_by_one_accounts_guesses(self):
        make_user()
        make_user(username="bob", email="bob@example.com")
        for _ in range(4):
            self.login()
        self.assertEqual(self.login(username="bob", password="S3cure-pass!").status_code, status.HTTP_200_OK)

    def test_a_dead_cache_does_not_take_login_down(self):
        make_user()
        with mock.patch("django.core.cache.backends.locmem.LocMemCache.get", side_effect=ConnectionError("redis is down")):
            response = self.login(password="S3cure-pass!")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class RegistrationThrottleTests(ThrottleTestCase):
    def test_repeated_sign_ups_from_one_address_are_refused(self):
        statuses = []
        for i in range(3):
            body = {**REGISTER, "username": f"user{i}", "email": f"user{i}@example.com"}
            statuses.append(self.client.post("/api/register/", body, format="json").status_code)
        self.assertEqual(statuses, [status.HTTP_201_CREATED, status.HTTP_201_CREATED,
                                    status.HTTP_429_TOO_MANY_REQUESTS])


class PasswordResetThrottleTests(ThrottleTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.redis = FakeRedis()
        patcher = mock.patch("api.redis_manager.redis_client", self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request_code(self, email="alice@example.com"):
        return self.client.post("/api/password-reset/", {"email": email}, format="json")

    def test_one_inbox_cannot_be_flooded_with_codes(self):
        statuses = [self.request_code().status_code for _ in range(3)]
        self.assertEqual(statuses, [status.HTTP_200_OK, status.HTTP_200_OK, status.HTTP_429_TOO_MANY_REQUESTS])
        self.assertEqual(len(mail.outbox), 2)

    def test_the_email_limit_ignores_letter_case(self):
        self.request_code("alice@example.com")
        self.request_code("ALICE@example.com")
        self.assertEqual(self.request_code("Alice@Example.com").status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_other_inboxes_are_unaffected(self):
        make_user(username="bob", email="bob@example.com")
        for _ in range(3):
            self.request_code()
        self.assertEqual(self.request_code("bob@example.com").status_code, status.HTTP_200_OK)

    def test_trying_codes_against_one_account_is_limited_even_after_new_codes(self):
        """The per-code guess cap resets when a new code is requested; the per-account limit does not."""
        for _ in range(3):
            self.client.post("/api/password-reset-confirm/",
                             {"username": "alice", "otp": "000000", "password": "N3w-password!"}, format="json")
        response = self.client.post("/api/password-reset-confirm/",
                                    {"username": "alice", "otp": "000000", "password": "N3w-password!"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class SessionsEndOnPasswordResetTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        patcher = mock.patch("api.redis_manager.redis_client", FakeRedis())
        patcher.start()
        self.addCleanup(patcher.stop)

    def sign_in(self):
        return self.client.post("/api/login/", {"username": "alice", "password": "S3cure-pass!"}, format="json").data

    def refresh(self, token):
        return self.client.post("/api/token/refresh/", {"refresh": token}, format="json").status_code

    def reset_password(self):
        self.client.post("/api/password-reset/", {"email": "alice@example.com"}, format="json")
        code = mail.outbox[-1].body.split("code is: ")[1].split("\n")[0]
        return self.client.post("/api/password-reset-confirm/",
                                {"username": "alice", "otp": code, "password": "N3w-password!"}, format="json")

    def test_every_signed_in_phone_is_signed_out(self):
        phone_one, phone_two = self.sign_in(), self.sign_in()
        self.assertEqual(self.refresh(phone_one["refresh_token"]), status.HTTP_200_OK)
        self.assertEqual(self.reset_password().status_code, status.HTTP_200_OK)
        self.assertEqual(self.refresh(phone_one["refresh_token"]), status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.refresh(phone_two["refresh_token"]), status.HTTP_401_UNAUTHORIZED)

    def test_signing_in_again_with_the_new_password_works(self):
        self.sign_in()
        self.reset_password()
        login = self.client.post("/api/login/", {"username": "alice", "password": "N3w-password!"}, format="json")
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.assertEqual(self.refresh(login.data["refresh_token"]), status.HTTP_200_OK)

    def test_a_wrong_code_leaves_sessions_alone(self):
        phone = self.sign_in()
        self.client.post("/api/password-reset/", {"email": "alice@example.com"}, format="json")
        self.client.post("/api/password-reset-confirm/",
                         {"username": "alice", "otp": "not-it", "password": "N3w-password!"}, format="json")
        self.assertEqual(self.refresh(phone["refresh_token"]), status.HTTP_200_OK)
