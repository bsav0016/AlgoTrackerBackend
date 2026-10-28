from rest_framework import status
from rest_framework.test import APITestCase

from api.models import Notification, PushDevice, Trade
from api.tests.test_strategy import RISING, make_strategy, make_user


class NotificationHistoryTests(APITestCase):
    url = "/api/notifications/"

    def setUp(self):
        self.user = make_user()
        self.strategy = make_strategy(self.user, position=1)
        self.client.force_authenticate(self.user)

    def add(self, user=None, n=1):
        for i in range(n):
            trade = Trade.objects.create(strategy=self.strategy, price=10 + i, is_buy=True)
            Notification.objects.create(user=user or self.user, strategy=self.strategy, trade=trade,
                                        kind="buy", message=f"signal {i}")

    def test_requires_a_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lists_only_my_notifications_newest_first_with_ids_for_tap_to_open(self):
        self.add(n=2)
        other = make_user(username="other", email="other@example.com")
        self.add(user=other)
        body = self.client.get(self.url).data
        self.assertEqual([r["message"] for r in body["results"]], ["signal 1", "signal 0"])
        first = body["results"][0]
        trade = Trade.objects.get(price=11)
        self.assertEqual((first["strategy_id"], first["trade_id"], first["kind"], first["price"]),
                         (self.strategy.pk, trade.pk, "buy", "11.00"))
        self.assertIsNone(body["next_before"])

    def test_pages_with_next_before(self):
        self.add(n=3)
        first = self.client.get(self.url, {"limit": 2}).data
        self.assertEqual([r["message"] for r in first["results"]], ["signal 2", "signal 1"])
        second = self.client.get(self.url, {"limit": 2, "before": first["next_before"]}).data
        self.assertEqual([r["message"] for r in second["results"]], ["signal 0"])
        self.assertIsNone(second["next_before"])

    def test_bad_paging_values_are_a_400(self):
        self.assertEqual(self.client.get(self.url, {"limit": "x"}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_a_strategy_removes_its_notifications(self):
        self.add()
        self.strategy.delete()  # its trades cascade, and each notification goes with its trade
        self.assertEqual(self.client.get(self.url).data["results"], [])

    def test_a_real_signal_shows_up(self):
        PushDevice.register(self.user, "ExponentPushToken[a]")
        self.strategy.check_signals(RISING)
        results = self.client.get(self.url).data["results"]
        self.assertEqual([r["message"] for r in results], ["Momentum triggered a buy signal!"])
