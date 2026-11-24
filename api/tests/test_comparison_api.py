"""Rules that compare two indicators (stage 2): validation, storage, editing, the live job and the share text."""
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from api.indicators.signal import Signal
from api.models import Strategy
from api.serializers import SignalSerializer, StrategySerializer
from api.services.card_service import describe_signal
from api.tests.test_api_trading import BUY_SMA, SELL_SMA, backtest_payload, make_user, twelve_data
from api.tests.test_strategy import make_user as make_strategy_user

# Buy while the 2-candle average is above the 4-candle average, sell once it is below.
BUY_CROSS = {"name": "SMA_PRICE", "above_target": True, "buy_signal": True, "window": 2,
             "compare_to": {"name": "SMA_PRICE", "window": 4}}
SELL_CROSS = {"name": "SMA_PRICE", "above_target": False, "buy_signal": False, "window": 2,
              "compare_to": {"name": "SMA_PRICE", "window": 4}}


def check(data):
    serializer = SignalSerializer(data=data)
    return serializer.is_valid(), serializer


class SignalSerializerTests(SimpleTestCase):
    def test_a_comparison_rule_is_valid_and_target_value_is_not_needed(self):
        ok, serializer = check(BUY_CROSS)
        self.assertTrue(ok, serializer.errors)
        self.assertEqual(serializer.validated_data["compare_to"], {"name": "SMA_PRICE", "window": 4})
        self.assertEqual(serializer.validated_data["target_value"], 0.0)

    def test_a_target_value_sent_with_compare_to_is_ignored(self):
        ok, serializer = check({**BUY_CROSS, "target_value": 123.0})
        self.assertTrue(ok)
        self.assertEqual(serializer.validated_data["target_value"], 0.0)

    def test_ema_and_sma_averages_can_be_mixed(self):
        for first, second in (("EMA_PRICE", "SMA_PRICE"), ("SMA_PRICE", "EMA_PRICE"), ("EMA_PRICE", "EMA_PRICE")):
            ok, serializer = check({**BUY_CROSS, "name": first, "compare_to": {"name": second, "window": 9}})
            self.assertTrue(ok, (first, second, serializer.errors))

    def test_the_comparison_indicator_needs_a_sane_window(self):
        for window in (0, -3, 501):
            ok, serializer = check({**BUY_CROSS, "compare_to": {"name": "SMA_PRICE", "window": window}})
            self.assertFalse(ok, window)
            self.assertIn("compare_to", serializer.errors)

    def test_an_average_cannot_be_compared_with_itself(self):
        ok, serializer = check({**BUY_CROSS, "compare_to": {"name": "SMA_PRICE", "window": 2}})
        self.assertFalse(ok)
        self.assertIn("itself", str(serializer.errors))

    def test_only_same_scale_indicators_can_be_compared(self):
        for name, extra in (("RSI", {"window": 14}), ("SMA", {"window": 5}), ("EMA", {"window": 5}),
                            ("MACD", {"fast_window": 3, "slow_window": 6, "signal_window": 3})):
            ok, serializer = check({"name": name, "above_target": True, "buy_signal": True,
                                    "compare_to": {"name": "SMA_PRICE", "window": 4}, **extra})
            self.assertFalse(ok, name)
            self.assertIn("cannot be compared", str(serializer.errors))

    def test_a_price_average_cannot_be_compared_with_an_oscillator_or_an_unknown_indicator(self):
        for other in ("RSI", "SMA", "FOO"):
            ok, serializer = check({**BUY_CROSS, "compare_to": {"name": other, "window": 4}})
            self.assertFalse(ok, other)
            self.assertIn("can only be compared with", str(serializer.errors))

    def test_a_price_average_must_be_compared_not_set_against_a_typed_price(self):
        ok, serializer = check({"name": "SMA_PRICE", "above_target": True, "buy_signal": True, "window": 20,
                                "target_value": 150.0})
        self.assertFalse(ok)
        self.assertIn("send compare_to", str(serializer.errors))

    def test_compare_to_null_is_the_same_as_leaving_it_out(self):
        ok, serializer = check({"name": "RSI", "above_target": True, "buy_signal": True, "window": 14,
                                "target_value": 70, "compare_to": None})
        self.assertTrue(ok, serializer.errors)
        self.assertNotIn("compare_to", serializer.validated_data)

    def test_ordinary_rules_are_validated_exactly_as_before(self):
        ok, serializer = check({"name": "RSI", "above_target": True, "buy_signal": True, "window": 14})
        self.assertFalse(ok)
        self.assertEqual(str(serializer.errors["target_value"][0]), "This field is required.")
        ok, serializer = check({"name": "RSI", "above_target": True, "buy_signal": True, "window": 14,
                                "target_value": float("inf")})
        self.assertFalse(ok)
        ok, _ = check({"name": "RSI", "above_target": True, "buy_signal": True, "window": 0, "target_value": 70})
        self.assertFalse(ok)
        ok, serializer = check({**BUY_SMA})
        self.assertTrue(ok, serializer.errors)
        self.assertNotIn("compare_to", serializer.validated_data)


class BacktestComparisonTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)
        self.closes = [10, 11, 12, 13, 14, 13, 12, 11, 10, 11, 12, 13, 14]
        patcher = mock.patch("api.services.backtest_service.requests.get", return_value=twelve_data(self.closes))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_crossover_backtest_runs_and_returns_both_averages_for_the_chart(self):
        response = self.client.post("/api/backtest/", backtest_payload(signals=[BUY_CROSS, SELL_CROSS]), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = response.data["result"]
        self.assertEqual(len(rows), len(self.closes))
        self.assertTrue({"SMA_PRICE_2", "SMA_PRICE_4"} <= set(rows[5]["indicators"]))
        close = pd.Series(self.closes, dtype=float)
        fast, slow = close.rolling(2).mean(), close.rolling(4).mean()
        for index, row in enumerate(rows):
            if index >= 3:
                self.assertAlmostEqual(row["indicators"]["SMA_PRICE_2"], fast[index])
                self.assertAlmostEqual(row["indicators"]["SMA_PRICE_4"], slow[index])
                self.assertEqual(row["buy"], bool(fast[index] > slow[index]), index)
                self.assertEqual(row["sell"], bool(fast[index] < slow[index]), index)
        self.assertIn(1, [row["owned"] for row in rows])

    def test_a_bad_comparison_is_a_400_before_any_credits_are_spent(self):
        bad = {**BUY_CROSS, "compare_to": {"name": "RSI", "window": 4}}
        response = self.client.post("/api/backtest/", backtest_payload(signals=[bad, SELL_CROSS]), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_saved_backtest_keeps_the_comparison_and_ordinary_rules_are_stored_as_before(self):
        self.client.post("/api/backtest/", backtest_payload(signals=[BUY_CROSS, SELL_CROSS]), format="json")
        self.client.post("/api/backtest/", backtest_payload(), format="json")
        saved = self.client.get("/api/backtests/")
        self.assertEqual(saved.status_code, 200)
        from api.models import SavedBacktest
        cross, plain = SavedBacktest.objects.order_by("id")
        self.assertEqual(cross.buy_signals[0]["compare_to"], {"name": "SMA_PRICE", "window": 4})
        self.assertNotIn("compare_to", plain.buy_signals[0])
        self.assertEqual(set(plain.buy_signals[0]), {"name", "above_target", "buy_signal", "target_value", "window",
                                                     "fast_window", "slow_window", "signal_window", "version"})


class StrategyComparisonTests(APITestCase):
    def setUp(self):
        self.user = make_strategy_user()
        self.client.force_authenticate(self.user)

    def create(self, signals):
        response = self.client.post("/api/strategy/", {"title": "Crossover", "symbol": "AAPL", "interval": "1day",
                                                       "signals": signals}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return Strategy.objects.latest("id")

    def test_a_crossover_strategy_is_stored_with_compare_to_and_comes_back_in_the_payload(self):
        strategy = self.create([BUY_CROSS, SELL_CROSS])
        self.assertEqual(strategy.buy_signals[0]["compare_to"], {"name": "SMA_PRICE", "window": 4})
        self.assertEqual(strategy.sell_signals[0]["target_value"], 0.0)
        payload = StrategySerializer(strategy).data
        self.assertEqual(payload["buy_signals"][0]["compare_to"], {"name": "SMA_PRICE", "window": 4})

    def test_a_strategy_with_only_typed_numbers_is_stored_exactly_as_before(self):
        strategy = self.create([BUY_SMA, SELL_SMA])
        self.assertEqual(set(strategy.buy_signals[0]), {"name", "above_target", "buy_signal", "target_value", "window",
                                                        "fast_window", "slow_window", "signal_window", "version"})

    def test_combine_signals_rebuilds_the_comparison_for_the_live_job(self):
        strategy = self.create([BUY_CROSS, SELL_CROSS])
        buy, sell = strategy.combine_signals()
        self.assertEqual(buy.compare_to, {"name": "SMA_PRICE", "window": 4})
        self.assertEqual(sell.compare_to, {"name": "SMA_PRICE", "window": 4})
        self.assertTrue(buy.buy_signal and not sell.buy_signal)

    def test_a_strategy_stored_before_compare_to_existed_still_rebuilds(self):
        strategy = self.create([BUY_SMA, SELL_SMA])
        self.assertTrue(all(signal.compare_to is None for signal in strategy.combine_signals()))


class EditComparisonTests(APITestCase):
    def setUp(self):
        self.user = make_strategy_user()
        self.client.force_authenticate(self.user)
        self.client.post("/api/strategy/", {"title": "Plain", "symbol": "AAPL", "interval": "1day",
                                            "signals": [BUY_SMA, SELL_SMA]}, format="json")
        self.strategy = Strategy.objects.get()
        self.url = f"/api/strategy/{self.strategy.pk}/"

    def patch(self, signals):
        return self.client.patch(self.url, {"signals": signals}, format="json")

    def test_editing_to_a_comparison_rule_is_a_recorded_change_and_editing_it_again_unchanged_is_not(self):
        self.assertEqual(self.patch([BUY_CROSS, SELL_CROSS]).status_code, 200)
        self.strategy.refresh_from_db()
        self.assertEqual(self.strategy.buy_signals[0]["compare_to"], {"name": "SMA_PRICE", "window": 4})
        self.assertIn("buy_signals", self.strategy.edit_log[-1]["changes"])
        logged = len(self.strategy.edit_log)
        self.assertEqual(self.patch([BUY_CROSS, SELL_CROSS]).status_code, 200)
        self.strategy.refresh_from_db()
        self.assertEqual(len(self.strategy.edit_log), logged)

    def test_changing_only_the_slow_window_is_a_change(self):
        self.patch([BUY_CROSS, SELL_CROSS])
        slower = {**BUY_CROSS, "compare_to": {"name": "SMA_PRICE", "window": 30}}
        self.assertEqual(self.patch([slower, SELL_CROSS]).status_code, 200)
        self.strategy.refresh_from_db()
        self.assertEqual(self.strategy.buy_signals[0]["compare_to"]["window"], 30)

    def test_an_invalid_comparison_in_an_edit_is_refused_and_the_rules_are_kept(self):
        response = self.patch([{**BUY_CROSS, "compare_to": {"name": "RSI", "window": 4}}, SELL_CROSS])
        self.assertEqual(response.status_code, 400)
        self.strategy.refresh_from_db()
        self.assertNotIn("compare_to", self.strategy.buy_signals[0])

    def test_a_rule_stored_with_compare_to_null_equals_one_stored_without_it(self):
        with_null = [{**BUY_SMA, "compare_to": None}]
        self.assertTrue(Strategy._same_signals([BUY_SMA], with_null))
        self.assertFalse(Strategy._same_signals([BUY_SMA], [BUY_CROSS]))

    def test_unchanged_ordinary_rules_are_not_a_change_after_this_feature(self):
        """An old strategy's stored rules (no compare_to key) must still equal the same rules sent again."""
        before = self.strategy.edit_log
        self.assertEqual(self.patch([BUY_SMA, SELL_SMA]).status_code, 200)
        self.strategy.refresh_from_db()
        self.assertEqual(self.strategy.edit_log, before)


class LiveComparisonTests(TestCase):
    def test_the_live_job_buys_when_the_fast_average_is_above_the_slow_one(self):
        user = make_strategy_user()
        strategy = Strategy.objects.create(
            user=user, title="Cross", symbol="AAPL", interval="1day", position=1,
            buy_signals=[{**BUY_CROSS, "target_value": 0.0}], sell_signals=[{**SELL_CROSS, "target_value": 0.0}])
        strategy = Strategy.objects.get(pk=strategy.pk)
        rising = pd.DataFrame({"close": [10.0, 10, 10, 10, 11, 13]})
        with mock.patch.object(Strategy, "send_notifications"):
            strategy.check_signals(rising)
        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)           # bought: 2-candle average 12 is above 4-candle average 11
        falling = pd.DataFrame({"close": [14.0, 14, 14, 14, 12, 8]})
        with mock.patch.object(Strategy, "send_notifications"):
            strategy.check_signals(falling)
        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 0)           # sold: 2-candle average 10 is below 4-candle average 12


class DescribeSignalTests(SimpleTestCase):
    def test_a_comparison_reads_as_one_indicator_against_the_other(self):
        self.assertEqual(describe_signal({**BUY_CROSS, "target_value": 0.0}), "SMA_PRICE(2) > SMA_PRICE(4)")
        self.assertEqual(describe_signal({**SELL_CROSS, "target_value": 0.0}), "SMA_PRICE(2) < SMA_PRICE(4)")

    def test_ordinary_rules_read_as_before(self):
        self.assertEqual(describe_signal({"name": "RSI", "window": 14, "above_target": True, "target_value": 70}),
                         "RSI(14) > 70")
        self.assertEqual(describe_signal({"name": "MACD", "fast_window": 3, "slow_window": 6, "signal_window": 3,
                                          "above_target": False, "target_value": 0}), "MACD(3,6,3) < 0")

    def test_a_signal_dataclass_with_compare_to_round_trips_through_the_stored_form(self):
        from api.indicators.signal import signal_dict
        stored = signal_dict(Signal(**{**BUY_CROSS, "target_value": 0.0}))
        self.assertEqual(stored["compare_to"], {"name": "SMA_PRICE", "window": 4})
        self.assertNotIn("compare_to", signal_dict(Signal("RSI", True, True, 70, window=14)))
