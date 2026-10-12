"""Account, auth and password-reset workflows through the real URL routing, serializers and database."""
from unittest import mock

from django.core import mail
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import CustomUser, Strategy
from api.tests.helpers import FakeRedis

REGISTER = {
    "username": "alice", "email": "alice@example.com", "password": "S3cure-pass!",
    "first_name": "Alice", "last_name": "Smith",
}


def make_user(username="alice", email="alice@example.com", password="S3cure-pass!", **kwargs):
    return CustomUser.objects.create_user(
        username=username, email=email, password=password, first_name="A", last_name="B", **kwargs)


class RegistrationTests(APITestCase):
    url = "/api/register/"

    def test_registers_a_user_and_returns_tokens(self):
        response = self.client.post(self.url, REGISTER, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["access_token"] and response.data["refresh_token"])
        self.assertNotIn("password", response.data)
        user = CustomUser.objects.get(username="alice")
        self.assertTrue(user.check_password(REGISTER["password"]))
        self.assertEqual((user.monthly_credits, user.account_credits), (500, 0))

    def test_valid_promo_code_adds_account_credits(self):
        response = self.client.post(self.url, {**REGISTER, "promo_code": "WELCOME"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CustomUser.objects.get(username="alice").account_credits, 50)

    def test_unknown_promo_code_is_rejected_and_no_user_is_created(self):
        response = self.client.post(self.url, {**REGISTER, "promo_code": "NOPE"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(CustomUser.objects.exists())

    def test_blank_promo_markers_are_ignored(self):
        for marker in ("", " ", "N/A"):
            with self.subTest(marker=marker):
                CustomUser.objects.all().delete()
                response = self.client.post(self.url, {**REGISTER, "promo_code": marker}, format="json")
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_duplicate_username_and_email_are_rejected(self):
        make_user()
        for field, value in (("username", "alice"), ("email", "alice@example.com")):
            with self.subTest(field=field):
                payload = {**REGISTER, "username": "other", "email": "other@example.com", field: value}
                response = self.client.post(self.url, payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.assertIn(field, response.data["detail"])
        self.assertEqual(CustomUser.objects.count(), 1)

    def test_missing_fields_are_rejected(self):
        response = self.client.post(self.url, {"username": "alice"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(CustomUser.objects.exists())


class LoginTests(APITestCase):
    def setUp(self):
        self.user = make_user()

    def test_login_returns_tokens_that_authenticate_requests(self):
        response = self.client.post("/api/login/", {"username": "alice", "password": "S3cure-pass!"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access_token']}")
        details = self.client.get("/api/user-details/")
        self.assertEqual(details.status_code, status.HTTP_200_OK)
        self.assertEqual(details.data["username"], "alice")

    def test_wrong_password_is_rejected(self):
        response = self.client.post("/api/login/", {"username": "alice", "password": "wrong"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("access_token", response.data)

    def test_missing_credentials_are_a_bad_request(self):
        response = self.client.post("/api/login/", {"username": "alice"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_inactive_user_cannot_log_in(self):
        CustomUser.objects.filter(pk=self.user.pk).update(is_active=False)
        response = self.client.post("/api/login/", {"username": "alice", "password": "S3cure-pass!"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_protected_endpoints_require_a_token(self):
        for method, url in (("get", "/api/user-details/"), ("post", "/api/backtest/"),
                            ("post", "/api/strategy/"), ("post", "/api/device-id/"),
                            ("post", "/api/payment-intent/"), ("delete", "/api/delete-account/")):
            with self.subTest(url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_garbage_token_is_rejected(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")
        self.assertEqual(self.client.get("/api/user-details/").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_token_can_be_logged_out(self):
        login = self.client.post("/api/login/", {"username": "alice", "password": "S3cure-pass!"},
                                 format="json").data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access_token']}")
        response = self.client.post("/api/logout/", {"refresh": login["refresh_token"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refreshed = self.client.post("/api/token/refresh/", {"refresh": login["refresh_token"]}, format="json")
        self.assertEqual(refreshed.status_code, status.HTTP_401_UNAUTHORIZED)


class MonthlyCreditsTests(APITestCase):
    def test_reset_date_in_the_past_tops_up_monthly_credits_on_user_details(self):
        from datetime import timedelta
        from django.utils import timezone
        user = make_user(monthly_credits=40, reset_monthly_credits=timezone.now() - timedelta(days=1))
        self.client.force_authenticate(user)
        response = self.client.get("/api/user-details/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.monthly_credits, 540)
        self.assertGreater(user.reset_monthly_credits, timezone.now() + timedelta(days=29))

    def test_credits_are_not_topped_up_before_the_reset_date(self):
        user = make_user(monthly_credits=40)
        self.client.force_authenticate(user)
        self.client.get("/api/user-details/")
        user.refresh_from_db()
        self.assertEqual(user.monthly_credits, 40)


class DeleteAccountTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def test_wrong_password_keeps_the_account(self):
        response = self.client.delete("/api/delete-account/", {"password": "wrong"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(CustomUser.objects.filter(pk=self.user.pk).exists())

    def test_correct_password_deletes_the_account_and_its_strategies(self):
        Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day")
        response = self.client.delete("/api/delete-account/", {"password": "S3cure-pass!"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(CustomUser.objects.exists())
        self.assertFalse(Strategy.objects.exists())


class DeviceIdTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def test_add_is_idempotent_and_remove_deletes(self):
        for _ in range(2):
            self.assertEqual(self.client.post("/api/device-id/", {"device_id": "tok"}, format="json").status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.device_ids, ["tok"])

        self.assertEqual(self.client.delete("/api/device-id/", {"device_id": "tok"}, format="json").status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.device_ids, [])

    def test_missing_or_unknown_device_ids_are_errors(self):
        self.assertEqual(self.client.post("/api/device-id/", {}, format="json").status_code, 400)
        self.assertEqual(self.client.delete("/api/device-id/", {}, format="json").status_code, 400)
        self.assertEqual(self.client.delete("/api/device-id/", {"device_id": "x"}, format="json").status_code, 404)


class PasswordResetTests(APITestCase):
    """Uses the real RedisManager against an in-memory Redis stand-in that returns bytes like redis-py."""

    def setUp(self):
        self.user = make_user()
        self.redis = FakeRedis()
        patcher = mock.patch("api.redis_manager.redis_client", self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request_code(self):
        response = self.client.post("/api/password-reset/", {"email": "alice@example.com"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = mail.outbox[-1].body
        return body.split("code is: ")[1].split("\n")[0]

    def confirm(self, otp, password="N3w-password!"):
        return self.client.post("/api/password-reset-confirm/",
                                {"username": "alice", "otp": otp, "password": password}, format="json")

    def test_full_reset_flow_changes_the_password(self):
        otp = self.request_code()
        self.assertEqual(mail.outbox[-1].to, ["alice@example.com"])
        self.assertEqual(self.confirm(otp).status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-password!"))

    def test_code_is_single_use(self):
        otp = self.request_code()
        self.assertEqual(self.confirm(otp).status_code, status.HTTP_200_OK)
        self.assertEqual(self.confirm(otp, "An0ther-pass!").status_code, status.HTTP_417_EXPECTATION_FAILED)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-password!"))

    def test_wrong_code_does_not_change_the_password(self):
        otp = self.request_code()
        wrong = "000000" if otp != "000000" else "111111"
        self.assertEqual(self.confirm(wrong).status_code, status.HTTP_417_EXPECTATION_FAILED)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("S3cure-pass!"))

    def test_code_expires_after_ten_minutes(self):
        self.request_code()
        self.assertEqual(self.redis.ttl("password_reset_otp:alice@example.com"), 600)

    def test_requesting_a_new_code_invalidates_the_previous_one(self):
        first = self.request_code()
        second = self.request_code()
        if first != second:
            self.assertEqual(self.confirm(first).status_code, status.HTTP_417_EXPECTATION_FAILED)
        self.assertEqual(self.confirm(second).status_code, status.HTTP_200_OK)

    def test_unknown_email_gets_404_and_no_mail(self):
        response = self.client.post("/api/password-reset/", {"email": "nobody@example.com"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(mail.outbox, [])

    def test_confirm_for_unknown_user_is_404(self):
        response = self.client.post("/api/password-reset-confirm/",
                                    {"username": "ghost", "otp": "123456", "password": "x"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PromoCodeAvailabilityTests(APITestCase):
    def test_reports_the_configured_flag_on_the_path_the_app_calls(self):
        # The app requests 'promo-code-availability/' (with a trailing slash).
        response = self.client.get("/api/promo-code-availability/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"availability": True})
