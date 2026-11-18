"""The strategy's curve in the strategy payload: the same list the share card draws, from one function."""
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import Trade
from api.serializers import StrategySerializer
from api.services.card_service import equity_points
from api.tests.test_shared_strategies import ShareTestCase
from api.tests.test_strategy import make_strategy, make_user

NOW = timezone.now()


def trade(strategy, days_ago, price, is_buy, exit_reason=""):
    return Trade.objects.create(strategy=strategy, date=NOW - timedelta(days=days_ago), price=price, is_buy=is_buy,
                                exit_reason=exit_reason)


class CurveTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.strategy = make_strategy(self.user)

    def curve(self):
        return StrategySerializer(type(self.strategy).objects.get(pk=self.strategy.pk)).data["equity_curve"]

    def test_there_is_no_curve_until_there_is_something_to_draw(self):
        self.assertEqual(self.curve(), [])
        trade(self.strategy, 9, 100, False)  # a sell with nothing bought
        self.assertEqual(self.curve(), [])

    def test_a_round_trip_starts_at_one_and_multiplies_by_sell_over_buy(self):
        trade(self.strategy, 9, 100, True)
        trade(self.strategy, 5, 110, False)
        curve = self.curve()
        self.assertEqual([round(p["multiple"], 6) for p in curve], [1.0, 1.1])
        self.assertEqual([p["date"] for p in curve], sorted(p["date"] for p in curve))

    def test_two_round_trips_compound(self):
        for days, price, is_buy in ((9, 100, True), (7, 110, False), (5, 100, True), (3, 90, False)):
            trade(self.strategy, days, price, is_buy)
        self.assertEqual([round(p["multiple"], 6) for p in self.curve()], [1.0, 1.1, round(1.1 * 0.9, 6)])

    def test_a_position_still_open_ends_at_the_strategys_current_return(self):
        trade(self.strategy, 9, 100, True)
        self.strategy.strategy_return = 1.25
        self.strategy.save()
        curve = self.curve()
        self.assertEqual(len(curve), 2)
        self.assertEqual(curve[0]["multiple"], 1.0)
        self.assertAlmostEqual(curve[1]["multiple"], 1.25)

    def test_a_sale_by_an_exit_rule_counts_like_any_other_sale(self):
        trade(self.strategy, 9, 100, True)
        trade(self.strategy, 5, 95, False, exit_reason="stop_loss")
        self.assertEqual([round(p["multiple"], 6) for p in self.curve()], [1.0, 0.95])

    def test_the_order_trades_were_saved_in_does_not_matter(self):
        later_sell = trade(self.strategy, 5, 110, False)
        trade(self.strategy, 9, 100, True)
        self.assertEqual([round(p["multiple"], 6) for p in self.curve()], [1.0, 1.1])
        later_sell.delete()
        self.assertEqual(len(self.curve()), 2)  # now just an open position

    def test_it_is_the_same_list_the_share_card_is_drawn_from(self):
        for days, price, is_buy in ((20, 100, True), (15, 120, False), (10, 110, True), (6, 99, False), (2, 100, True)):
            trade(self.strategy, days, price, is_buy)
        shown = self.curve()
        card = equity_points(list(self.strategy.trades.order_by("date", "pk")), self.strategy.strategy_return)
        self.assertEqual(len(shown), len(card))
        for point, (moment, multiple) in zip(shown, card):
            self.assertEqual(point["multiple"], multiple)
            # An open position ends at "now", which each call reads for itself; every other date is a trade's own.
            self.assertAlmostEqual(point["date"].timestamp(), moment.timestamp(), delta=5)
        self.assertEqual([p["date"] for p in shown[:-1]], [m for m, _ in card[:-1]])

    def test_the_response_carries_numbers_and_date_text(self):
        trade(self.strategy, 9, 100, True)
        trade(self.strategy, 5, 110, False)
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/user-details/")
        point = response.json()["strategies"][0]["equity_curve"][0]
        self.assertIsInstance(point["multiple"], float)
        self.assertIsInstance(point["date"], str)

    def test_it_comes_back_with_the_users_strategies(self):
        trade(self.strategy, 9, 100, True)
        trade(self.strategy, 5, 110, False)
        self.client.force_authenticate(self.user)
        strategies = self.client.get("/api/user-details/").data["strategies"]
        self.assertEqual(len(strategies[0]["equity_curve"]), 2)

    def test_the_query_count_does_not_grow_with_the_number_of_trades(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.force_authenticate(self.user)
        trade(self.strategy, 9, 100, True)
        with CaptureQueriesContext(connection) as few:
            self.client.get("/api/user-details/")
        for i in range(20):
            trade(self.strategy, 8 - (i % 8), 100 + i, i % 2 == 0)
        with CaptureQueriesContext(connection) as many:
            self.client.get("/api/user-details/")
        self.assertEqual(len(few), len(many))


class QueryCostTests(APITestCase):
    def test_a_strategy_costs_no_more_queries_than_before_the_curve_existed(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        user = make_user()
        self.client.force_authenticate(user)
        first = make_strategy(user)
        trade(first, 9, 100, True)
        with CaptureQueriesContext(connection) as one:
            self.client.get("/api/user-details/")
        for _ in range(3):
            other = make_strategy(user)
            trade(other, 9, 100, True)
            trade(other, 5, 110, False)
        with CaptureQueriesContext(connection) as four:
            self.client.get("/api/user-details/")
        # Each extra strategy adds its own trades query and nothing else: the curve reuses the trades the list loaded.
        self.assertEqual(len(four) - len(one), 3)


class SharedViewStaysPrivateTests(ShareTestCase):
    def test_the_public_share_view_does_not_carry_the_curve(self):
        link = self.share()
        data = self.client.get(f"/api/shared/{link}/").data
        self.assertNotIn("equity_curve", data)
        self.assertEqual(self.client.get(f"/api/shared/{link}/").status_code, status.HTTP_200_OK)
