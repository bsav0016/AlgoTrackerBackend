import io
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework import status

from api.models import Trade
from api.services.card_service import describe_signal, equity_points, render_card
from api.tests.test_shared_strategies import ShareTestCase

NOW = timezone.now()


def trade(days_ago, price, is_buy):
    return Trade(date=NOW - timedelta(days=days_ago), price=price, is_buy=is_buy)


class EquityPointsTests(ShareTestCase):
    def test_no_trades_means_no_curve(self):
        self.assertEqual(equity_points([], 1, NOW), [])

    def test_a_single_open_buy_runs_to_the_current_return(self):
        points = equity_points([trade(5, 100, True)], 1.1, NOW)
        self.assertEqual([round(v, 4) for _, v in points], [1.0, 1.1])
        self.assertEqual(points[-1][0], NOW)

    def test_a_closed_round_trip_multiplies_by_sell_over_buy(self):
        points = equity_points([trade(9, 100, True), trade(5, 110, False)], 1.1, NOW)
        self.assertEqual([round(v, 4) for _, v in points], [1.0, 1.1])

    def test_round_trips_compound(self):
        trades = [trade(9, 100, True), trade(8, 110, False), trade(7, 50, True), trade(6, 55, False)]
        self.assertAlmostEqual(equity_points(trades, 1.21, NOW)[-1][1], 1.21)

    def test_a_loss_goes_below_one(self):
        points = equity_points([trade(9, 100, True), trade(8, 80, False)], 0.8, NOW)
        self.assertAlmostEqual(points[-1][1], 0.8)

    def test_a_sell_with_nothing_held_is_ignored(self):
        self.assertEqual(equity_points([trade(9, 100, False)], 1, NOW), [])

    def test_a_zero_buy_price_does_not_crash(self):
        points = equity_points([trade(9, 0, True), trade(8, 5, False)], 1, NOW)
        self.assertEqual([v for _, v in points], [1.0, 1.0])


class DescribeSignalTests(ShareTestCase):
    def test_windows_and_direction_are_shown(self):
        self.assertEqual(describe_signal({"name": "RSI", "above_target": False, "target_value": 30, "window": 14}),
                         "RSI(14) < 30")
        self.assertEqual(describe_signal({"name": "MACD", "above_target": True, "target_value": 0,
                                          "fast_window": 12, "slow_window": 26, "signal_window": 9}),
                         "MACD(12,26,9) > 0")


class CardImageTests(ShareTestCase):
    def png(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "image/png")
        return Image.open(io.BytesIO(response.content))

    def test_a_shared_strategy_has_a_link_preview_sized_card(self):
        link = self.share()
        image = self.png(f"/api/shared/{link}/card.png")
        self.assertEqual(image.size, (1200, 630))

    def test_it_can_be_cached_briefly(self):
        link = self.share()
        self.assertIn("max-age=300", self.client.get(f"/api/shared/{link}/card.png")["Cache-Control"])

    def test_a_card_with_a_curve_differs_from_one_without(self):
        without = render_card(self.strategy, [], NOW)
        with_curve = render_card(self.strategy, [trade(9, 100, True), trade(5, 120, False)], NOW)
        self.assertNotEqual(without, with_curve)

    def test_dollar_signs_and_odd_text_in_the_title_do_not_break_it(self):
        self.strategy.title = "$$ Big $ gains $"
        self.assertTrue(render_card(self.strategy, [], NOW).startswith(b"\x89PNG"))
        self.strategy.title = "x" * 50
        self.assertTrue(render_card(self.strategy, [], NOW).startswith(b"\x89PNG"))

    def test_a_losing_open_position_renders(self):
        self.strategy.strategy_return = 0.8
        self.assertTrue(render_card(self.strategy, [trade(4, 100, True)], NOW).startswith(b"\x89PNG"))

    def test_the_view_draws_from_the_stored_trades(self):
        link = self.share()
        Trade.objects.create(strategy=self.strategy, price=100, is_buy=True)
        Trade.objects.create(strategy=self.strategy, price=120, is_buy=False)
        self.assertEqual(self.png(f"/api/shared/{link}/card.png").size, (1200, 630))

    def test_unknown_and_revoked_links_are_404(self):
        link = self.share()
        self.assertEqual(self.client.get("/api/shared/nope/card.png").status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(self.owner)
        self.client.delete(self.share_url())
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/shared/{link}/card.png").status_code, status.HTTP_404_NOT_FOUND)

    def test_it_is_limited_per_address(self):
        link = self.share()
        with override_settings(AUTH_THROTTLE_RATES={"shared_card_address": "2/min"}):
            statuses = [self.client.get(f"/api/shared/{link}/card.png").status_code for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 429, 429])
