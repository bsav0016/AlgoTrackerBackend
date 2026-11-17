import io
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from PIL import Image, ImageDraw
from rest_framework import status

from api.models import Trade
from api.services.card_service import (BACKGROUND, PLOT, SCALE, _fit, _font, describe_signal, equity_points,
                                       nice_ticks, render_card)
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


class DrawingTests(ShareTestCase):
    def is_png(self, data):
        return data[:4] == bytes.fromhex("89504e47")

    def test_tick_values_are_round_numbers_inside_the_range(self):
        self.assertEqual(nice_ticks(0, 100, 5), [0, 20, 40, 60, 80, 100])
        self.assertEqual(nice_ticks(-34, 3, 5), [-30, -20, -10, 0])
        self.assertEqual(nice_ticks(5, 5), [5])
        self.assertEqual(nice_ticks(float("nan"), 5), [])

    def test_text_is_shortened_to_fit(self):
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font = _font(21)
        self.assertEqual(_fit(draw, "short", font, 500), "short")
        fitted = _fit(draw, "a very long line of rules " * 8, font, 300)
        self.assertTrue(fitted.endswith("..."))
        self.assertLessEqual(draw.textlength(fitted, font=font), 300 * SCALE)

    def test_long_rule_lines_never_run_into_the_chart_area(self):
        signal = {"name": "MACD", "above_target": True, "target_value": 0.123456,
                  "fast_window": 12, "slow_window": 26, "signal_window": 9}
        self.strategy.buy_signals = [signal] * 3
        self.strategy.sell_signals = [signal] * 3
        self.strategy.exit_rules = {"stop_loss": 5.0, "trailing_stop": 4.0, "take_profit": 12.5}
        card = Image.open(io.BytesIO(render_card(self.strategy, [], NOW))).convert("RGB")
        background = tuple(int(BACKGROUND[i:i + 2], 16) for i in (1, 3, 5))
        self.assertEqual(card.getpixel((5, 5)), background)
        # The gap between the text column and the chart panel holds nothing when there is no curve to label.
        for y in range(150, 420):
            for x in range(586, PLOT[0] - 2):
                self.assertEqual(card.getpixel((x, y)), background, (x, y))

    def test_the_title_is_cut_rather_than_overflowing_the_card(self):
        self.strategy.title = "W" * 50
        card = Image.open(io.BytesIO(render_card(self.strategy, [], NOW))).convert("RGB")
        background = card.getpixel((5, 5))
        for y in range(20, 90):  # the title row, right of the longest allowed title
            for x in range(1150, 1200):
                self.assertEqual(card.getpixel((x, y)), background, (x, y))

    def test_dollar_signs_are_drawn_as_they_are(self):
        self.strategy.title = "$$ gains"
        self.assertTrue(self.is_png(render_card(self.strategy, [], NOW)))

    def test_a_flat_curve_renders(self):
        flat = [trade(9, 100, True), trade(5, 100, False)]
        self.assertTrue(self.is_png(render_card(self.strategy, flat, NOW)))
