from django.core.cache import caches
from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import CustomUser
from api.tests.test_strategy import make_user

URL = "/api/leaderboard/profile/"


class ProfileTestCase(APITestCase):
    def setUp(self):
        caches["throttle"].clear()
        self.addCleanup(caches["throttle"].clear)
        self.user = make_user(username="alice", email="alice@example.com", first_name="Alice", last_name="Smith")
        self.client.force_authenticate(self.user)

    def join(self, name):
        return self.client.put(URL, {"display_name": name}, format="json")

    def stored(self):
        return CustomUser.objects.get(pk=self.user.pk).leaderboard_name


class OptInTests(ProfileTestCase):
    def test_nobody_is_on_the_board_by_default(self):
        self.assertIsNone(self.user.leaderboard_name)
        self.assertEqual(self.client.get(URL).data, {"display_name": None})

    def test_joining_sets_the_name_and_shows_it_in_the_account_details(self):
        response = self.join("Quiet Trader")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"display_name": "Quiet Trader"})
        self.assertEqual(self.stored(), "Quiet Trader")
        self.assertEqual(self.client.get("/api/user-details/").data["leaderboard_name"], "Quiet Trader")

    def test_account_details_show_null_when_off(self):
        self.assertIsNone(self.client.get("/api/user-details/").data["leaderboard_name"])

    def test_the_name_can_be_changed(self):
        self.join("First Name")
        self.join("Second Name")
        self.assertEqual(self.stored(), "Second Name")

    def test_keeping_your_own_name_is_not_a_clash(self):
        self.join("Quiet Trader")
        self.assertEqual(self.join("quiet trader").status_code, status.HTTP_200_OK)
        self.assertEqual(self.stored(), "quiet trader")

    def test_extra_spaces_are_tidied(self):
        self.assertEqual(self.join("  Quiet    Trader ").data["display_name"], "Quiet Trader")

    def test_signing_in_is_required(self):
        self.client.force_authenticate(None)
        for call in (self.client.get, self.client.delete):
            self.assertEqual(call(URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.join("Quiet Trader").status_code, status.HTTP_401_UNAUTHORIZED)


class LeavingTests(ProfileTestCase):
    def test_leaving_clears_the_name(self):
        self.join("Quiet Trader")
        self.assertEqual(self.client.delete(URL).status_code, status.HTTP_204_NO_CONTENT)
        self.assertIsNone(self.stored())
        self.assertEqual(self.client.get(URL).data, {"display_name": None})

    def test_leaving_when_not_on_the_board_is_harmless(self):
        self.assertEqual(self.client.delete(URL).status_code, status.HTTP_204_NO_CONTENT)

    def test_a_left_name_can_be_taken_by_someone_else(self):
        self.join("Quiet Trader")
        self.client.delete(URL)
        other = make_user(username="bob", email="bob@example.com")
        self.client.force_authenticate(other)
        self.assertEqual(self.join("Quiet Trader").status_code, status.HTTP_200_OK)


class NameRuleTests(ProfileTestCase):
    def rejected(self, name):
        response = self.join(name)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, name)
        self.assertIn("display_name", response.data)
        self.assertIsNone(self.stored(), name)

    def test_length_limits(self):
        for name in ("ab", "x" * 21, "", "   "):
            self.rejected(name)
        self.assertEqual(self.join("abc").status_code, status.HTTP_200_OK)
        self.assertEqual(self.join("x" * 20).status_code, status.HTTP_200_OK)

    def test_only_plain_characters(self):
        for name in ("alice@example.com", "see example.com", "a/b/c", "<b>bold</b>", "emoji \U0001F600", "café owner",
                     "аdmin team", "zero​width", "-leading dash", "x@y.z"):
            self.rejected(name)

    def test_names_that_pass_for_the_app_or_staff_are_refused(self):
        for name in ("Admin", "admin", "A d m i n", "Algo_Tracker", "Support", "STAFF", "Official"):
            self.rejected(name)

    def test_another_users_username_cannot_be_borrowed(self):
        make_user(username="TopTrader", email="t@example.com")
        self.rejected("toptrader")

    def test_your_own_username_is_allowed(self):
        self.assertEqual(self.join("Alice").status_code, status.HTTP_200_OK)

    def test_a_name_in_use_is_refused_ignoring_case(self):
        other = make_user(username="bob", email="bob@example.com")
        self.client.force_authenticate(other)
        self.join("Quiet Trader")
        self.client.force_authenticate(self.user)
        response = self.join("QUIET trader")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["display_name"], ["That name is taken"])
        self.assertIsNone(self.stored())

    def test_missing_or_wrongly_typed_names_are_refused(self):
        for body in ({}, {"display_name": None}, {"display_name": 123}, {"display_name": ["a", "b"]}):
            response = self.client.put(URL, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)

    def test_the_database_itself_refuses_two_names_that_differ_only_in_case(self):
        other = make_user(username="bob", email="bob@example.com")
        CustomUser.objects.filter(pk=self.user.pk).update(leaderboard_name="Same Name")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomUser.objects.filter(pk=other.pk).update(leaderboard_name="SAME name")

    def test_many_users_can_be_off_the_board_at_once(self):
        make_user(username="bob", email="bob@example.com")
        make_user(username="carol", email="carol@example.com")
        self.assertEqual(CustomUser.objects.filter(leaderboard_name__isnull=True).count(), 3)


class NameThrottleTests(ProfileTestCase):
    def test_choosing_names_is_limited_per_address(self):
        with override_settings(AUTH_THROTTLE_RATES={"leaderboard_name_address": "3/hour"}):
            statuses = [self.join(f"Name {i}").status_code for i in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])

    def test_reading_and_leaving_are_not_limited(self):
        with override_settings(AUTH_THROTTLE_RATES={"leaderboard_name_address": "1/hour"}):
            codes = [self.client.get(URL).status_code for _ in range(3)]
            codes += [self.client.delete(URL).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 200, 204, 204, 204])
