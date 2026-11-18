"""The plain-language numbers: worked out by hand on a small series, then through the API and the saved copy."""
import math
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.test import APITestCase

from api import trade_stats
from api.services.backtest_service import BacktestService
from api.tests.test_api_trading import backtest_payload, make_user, twelve_data

# Ten candles. Buy at 1, sell at 4: in at 100, out at 110, a +10% trade. Buy at 6, sell at 7: in at 100, out at 90,
# a -10% trade. Buy at 8 and still holding at the end (not a closed trade).
CLOSES = [100, 100, 110, 121, 110, 110, 100, 90, 90, 99]
BUYS = [False, True, False, False, False, False, True, False, True, False]
SELLS = [False, False, False, False, True, False, False, True, False, False]


def run(cost_rate=0.0, delay=False, closes=CLOSES, buys=BUYS, sells=SELLS, exit_rules=None):
    frame = pd.DataFrame({"datetime": pd.date_range("2024-01-01", periods=len(closes)), "close": closes,
                          "buy": buys, "sell": sells})
    return BacktestService.calculate_returns(frame, cost_rate, delay, exit_rules)


class ByHandTests(SimpleTestCase):
    def test_the_worked_example(self):
        stats = trade_stats.summarize(run())
        self.assertEqual((stats["closed_trades"], stats["winning_trades"]), (2, 1))
        self.assertAlmostEqual(stats["win_rate"], 50.0)
        self.assertAlmostEqual(stats["average_trade"], 0.0)
        self.assertAlmostEqual(stats["best_trade"], 10.0)
        self.assertAlmostEqual(stats["worst_trade"], -10.0)
        self.assertAlmostEqual(stats["time_invested"], 6 / 9 * 100)  # held on 6 of the 9 candles after the first
        self.assertAlmostEqual(stats["max_drawdown"], (99 / 121 - 1) * 100)  # the strategy: 121 down to 99
        self.assertAlmostEqual(stats["asset_max_drawdown"], (90 / 121 - 1) * 100)  # holding: 121 down to 90
        self.assertTrue(stats["open_trade"])

    def test_trading_costs_come_out_of_each_trade(self):
        stats = trade_stats.summarize(run(cost_rate=0.01))
        # In at 100 costs 1%, out at 110 costs 1%: 100 * 0.99 * 1.1 * 0.99 = 107.811, so +7.811%.
        self.assertAlmostEqual(stats["best_trade"], 7.811, places=3)
        # In at 100 and out at 90: 110 * 0.99 * 0.9 * 0.99 / 110 - 1 over the same base.
        self.assertLess(stats["worst_trade"], -10.0)

    def test_trades_follow_the_position_actually_taken_when_trading_is_delayed_a_candle(self):
        stats = trade_stats.summarize(run(delay=True))
        self.assertEqual(stats["closed_trades"], 2)
        self.assertAlmostEqual(stats["best_trade"], 0.0)  # in at 110, out at 110
        self.assertAlmostEqual(stats["time_invested"], 5 / 9 * 100)

    def test_an_exit_rule_sale_is_a_closed_trade_like_any_other(self):
        closes = [100, 100, 94, 90, 90]
        result = run(closes=closes, buys=[False, True, True, True, True], sells=[False] * 5, exit_rules={"stop_loss": 5})
        stats = trade_stats.summarize(result)
        self.assertEqual(stats["closed_trades"], 1)
        self.assertAlmostEqual(stats["worst_trade"], -6.0)  # in at 100, stopped out at 94
        self.assertFalse(stats["open_trade"])


class WhenThereIsNothingToSayTests(SimpleTestCase):
    def test_no_trades_means_no_trade_figures_and_zero_time_invested(self):
        stats = trade_stats.summarize(run(closes=[100, 101, 99, 102], buys=[False] * 4, sells=[False] * 4))
        for key in ("win_rate", "average_trade", "best_trade", "worst_trade"):
            self.assertIsNone(stats[key], key)
        self.assertEqual((stats["closed_trades"], stats["winning_trades"]), (0, 0))
        self.assertEqual(stats["time_invested"], 0.0)
        self.assertEqual(stats["max_drawdown"], 0.0)
        self.assertFalse(stats["open_trade"])

    def test_a_position_that_never_closed_has_no_closed_trade_figures(self):
        stats = trade_stats.summarize(run(closes=[100, 100, 110, 120], buys=[False, True, False, False], sells=[False] * 4))
        self.assertEqual(stats["closed_trades"], 0)
        self.assertIsNone(stats["win_rate"])
        self.assertTrue(stats["open_trade"])
        self.assertAlmostEqual(stats["time_invested"], 100.0)

    def test_too_few_candles_or_missing_columns_give_none_throughout(self):
        for bad in (None, pd.DataFrame(), run().iloc[:1], run().drop(columns=["held"])):
            stats = trade_stats.summarize(bad)
            self.assertEqual(set(stats), set(trade_stats.KEYS))
            self.assertTrue(all(v is None for v in stats.values()))

    def test_a_losing_streak_is_counted_and_a_flat_trade_is_not_a_win(self):
        closes = [100, 100, 100, 95, 95, 90, 90]
        stats = trade_stats.summarize(run(closes=closes, buys=[False, True, False, True, False, False, False],
                                          sells=[False, False, True, False, False, True, False]))
        self.assertEqual(stats["closed_trades"], 2)
        self.assertEqual(stats["winning_trades"], 0)  # the first trade is flat, the second a loss
        self.assertAlmostEqual(stats["win_rate"], 0.0)

    def test_every_number_is_json_safe(self):
        stats = trade_stats.summarize(run())
        for value in stats.values():
            self.assertTrue(value is None or isinstance(value, (bool, int)) or math.isfinite(value))


class MaxDrawdownTests(SimpleTestCase):
    def test_the_deepest_fall_from_a_high(self):
        self.assertAlmostEqual(trade_stats.max_drawdown([100, 120, 90, 130, 117]), (90 / 120 - 1) * 100)

    def test_a_series_that_never_falls_is_zero_and_an_empty_one_too(self):
        self.assertEqual(trade_stats.max_drawdown([1, 2, 3]), 0.0)
        self.assertEqual(trade_stats.max_drawdown([]), 0.0)


class ThroughTheApiTests(APITestCase):
    def setUp(self):
        self.client.force_authenticate(make_user())
        patcher = mock.patch("api.services.backtest_service.requests.get", return_value=twelve_data())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_response_carries_the_numbers_and_they_match_the_rows(self):
        data = self.client.post("/api/backtest/", backtest_payload(), format="json").data
        for key in trade_stats.KEYS:
            self.assertIn(key, data)
        rows = data["result"]
        owned = [r["owned"] for r in rows]
        self.assertAlmostEqual(data["time_invested"], sum(owned[1:]) / (len(owned) - 1) * 100)
        self.assertLessEqual(data["max_drawdown"], 0)
        self.assertEqual(data["closed_trades"], sum(1 for a, b in zip(owned, owned[1:]) if a and not b))

    def test_the_numbers_are_kept_with_the_saved_run_and_come_back_the_same(self):
        data = self.client.post("/api/backtest/", backtest_payload(), format="json").data
        saved = self.client.get(f"/api/backtests/{data['id']}/").data
        for key in trade_stats.KEYS:
            self.assertEqual(saved[key], data[key], key)

    def test_a_run_saved_before_these_numbers_existed_still_opens(self):
        from api.models import SavedBacktest
        data = self.client.post("/api/backtest/", backtest_payload(), format="json").data
        saved = SavedBacktest.objects.get(pk=data["id"])
        saved.summary = {k: v for k, v in saved.summary.items() if k not in trade_stats.KEYS}
        saved.save()
        response = self.client.get(f"/api/backtests/{data['id']}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("win_rate", response.data)  # absent, which the app reads as "not known"
