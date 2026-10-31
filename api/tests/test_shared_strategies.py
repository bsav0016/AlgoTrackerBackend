from django.core.cache import caches
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import Strategy, StrategyShare
from api.tests.test_strategy import make_strategy, make_user

PUBLIC_FIELDS = {"title", "symbol", "interval", "buy_signals", "sell_signals", "strategy_return", "asset_return"}


class ShareTestCase(APITestCase):
    def setUp(self):
        caches["throttle"].clear()
        self.addCleanup(caches["throttle"].clear)
        self.owner = make_user(username="owner", email="owner@example.com", first_name="Olive", last_name="Owner")
        self.strategy = make_strategy(self.owner)

    def share_url(self, pk=None):
        return f"/api/strategy/{pk or self.strategy.pk}/share/"

    def sign_in(self, user):
        self.client.force_authenticate(user)

    def share(self):
        self.sign_in(self.owner)
        response = self.client.post(self.share_url())
        self.client.force_authenticate(None)
        return response.data["share_id"]


class CreateAndRevokeTests(ShareTestCase):
    def test_the_owner_gets_a_link_and_repeating_it_returns_the_same_one(self):
        self.sign_in(self.owner)
        first = self.client.post(self.share_url())
        again = self.client.post(self.share_url())
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["share_id"], again.data["share_id"])
        self.assertEqual(StrategyShare.objects.count(), 1)

    def test_ids_are_long_and_different_for_each_strategy(self):
        other = make_strategy(self.owner)
        self.sign_in(self.owner)
        a = self.client.post(self.share_url()).data["share_id"]
        b = self.client.post(self.share_url(other.pk)).data["share_id"]
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 20)

    def test_signing_in_is_required(self):
        self.assertEqual(self.client.post(self.share_url()).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.delete(self.share_url()).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_someone_else_cannot_share_a_strategy_and_cannot_tell_it_exists(self):
        stranger = make_user(username="stranger", email="s@example.com")
        self.sign_in(stranger)
        real = self.client.post(self.share_url())
        missing = self.client.post(self.share_url(self.strategy.pk + 999))
        self.assertEqual(real.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(real.data, missing.data)
        self.assertEqual(StrategyShare.objects.count(), 0)

    def test_someone_else_cannot_revoke_a_share(self):
        self.share()
        self.sign_in(make_user(username="stranger", email="s@example.com"))
        self.assertEqual(self.client.delete(self.share_url()).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(StrategyShare.objects.count(), 1)

    def test_revoking_kills_the_link_and_sharing_again_makes_a_new_one(self):
        old = self.share()
        self.sign_in(self.owner)
        self.assertEqual(self.client.delete(self.share_url()).status_code, status.HTTP_204_NO_CONTENT)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/shared/{old}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotEqual(self.share(), old)

    def test_revoking_when_not_shared_is_harmless(self):
        self.sign_in(self.owner)
        self.assertEqual(self.client.delete(self.share_url()).status_code, status.HTTP_204_NO_CONTENT)

    def test_deleting_the_strategy_ends_the_share(self):
        link = self.share()
        self.strategy.delete()
        self.assertEqual(self.client.get(f"/api/shared/{link}/").status_code, status.HTTP_404_NOT_FOUND)


class PublicViewTests(ShareTestCase):
    def test_anyone_can_read_a_shared_strategy_without_signing_in(self):
        link = self.share()
        response = self.client.get(f"/api/shared/{link}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Momentum")
        self.assertEqual(response.data["symbol"], "AAPL")
        self.assertEqual(response.data["interval"], "1day")
        self.assertEqual(len(response.data["buy_signals"]), 1)

    def test_nothing_beyond_the_listed_fields_is_returned(self):
        link = self.share()
        data = self.client.get(f"/api/shared/{link}/").data
        self.assertEqual(set(data), PUBLIC_FIELDS)

    def test_the_response_text_holds_nothing_about_the_owner(self):
        self.owner.account_credits = 4321
        self.owner.save()
        link = self.share()
        body = self.client.get(f"/api/shared/{link}/").content.decode().lower()
        for secret in ("owner@example.com", "olive", "owner", str(self.owner.pk) + ",", "4321", "charge_user_date"):
            self.assertNotIn(secret, body)

    def test_an_unknown_link_is_a_404(self):
        self.assertEqual(self.client.get("/api/shared/nope/").status_code, status.HTTP_404_NOT_FOUND)

    def test_the_view_shows_the_strategy_as_it_is_now(self):
        link = self.share()
        Strategy.objects.filter(pk=self.strategy.pk).update(title="Renamed")
        self.assertEqual(self.client.get(f"/api/shared/{link}/").data["title"], "Renamed")

    def test_repeated_requests_from_one_address_are_limited(self):
        link = self.share()
        with override_settings(AUTH_THROTTLE_RATES={"shared_view_address": "3/min"}):
            statuses = [self.client.get(f"/api/shared/{link}/").status_code for _ in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])

    def test_guessing_links_is_limited_too(self):
        with override_settings(AUTH_THROTTLE_RATES={"shared_view_address": "2/min"}):
            statuses = [self.client.get(f"/api/shared/guess{i}/").status_code for i in range(4)]
        self.assertEqual(statuses, [404, 404, 429, 429])
