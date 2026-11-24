"""Rules that compare two indicators with each other (stage 1: the indicators and the shared evaluator)."""
import math

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from api.indicators.price_average import calculate_ema_price, calculate_sma_price
from api.indicators.signal import Signal, comparison_signal, indicator_signals
from api.services.backtest_service import BacktestService
from api.services.signal_service import SignalService

CLOSES = [10.0, 11, 12, 11, 13, 12, 14, 13, 11, 10, 12, 15]


def frame(closes=CLOSES):
    return pd.DataFrame({"close": closes, "low": [c - 1 for c in closes], "high": [c + 1 for c in closes]})


def rule(name, window, other_name=None, other_window=None, buy=True, above=True, target=0.0, **extra):
    compare = {"name": other_name, "window": other_window} if other_name else None
    return Signal(name=name, above_target=above, buy_signal=buy, target_value=target, window=window,
                  compare_to=compare, **extra)


class PriceAverageTests(SimpleTestCase):
    def test_sma_price_is_the_plain_average_in_price_units(self):
        out = calculate_sma_price(frame([10.0, 11, 12, 11, 13]), Signal("SMA_PRICE", True, True, 0, window=3))
        values = out["SMA_PRICE_3"].tolist()
        self.assertTrue(math.isnan(values[0]) and math.isnan(values[1]))
        self.assertEqual(values[2:], [11.0, (11 + 12 + 11) / 3, 12.0])

    def test_ema_price_follows_the_recursive_definition(self):
        out = calculate_ema_price(frame([10.0, 11, 12, 11, 13]), Signal("EMA_PRICE", True, True, 0, window=3))
        alpha, expected = 0.5, [10.0]
        for close in [11, 12, 11, 13]:
            expected.append(alpha * close + (1 - alpha) * expected[-1])
        self.assertEqual(out["EMA_PRICE_3"].tolist(), expected)

    def test_they_do_not_change_the_existing_ratio_indicators(self):
        data = frame()
        ratio = SignalService.calculate_indicators(data, [Signal("SMA", True, True, 1, window=3)])
        price = SignalService.calculate_indicators(data, [Signal("SMA_PRICE", True, True, 1, window=3)])
        self.assertTrue(np.allclose(ratio["SMA_3"].dropna(), (price["SMA_PRICE_3"] / data["close"]).dropna()))
        self.assertNotEqual(ratio["SMA_3"].dropna().tolist(), price["SMA_PRICE_3"].dropna().tolist())


class ComparisonSignalTests(SimpleTestCase):
    def test_a_rule_without_compare_to_has_no_comparison_indicator(self):
        sig = Signal("SMA", True, True, 1, window=3)
        self.assertIsNone(sig.compare_to)
        self.assertIsNone(comparison_signal(sig))

    def test_signals_stored_before_compare_to_existed_still_load(self):
        stored = {"name": "RSI", "above_target": True, "buy_signal": True, "target_value": 30, "window": 14,
                  "fast_window": None, "slow_window": None, "signal_window": None, "version": 1}
        self.assertIsNone(Signal(**stored).compare_to)

    def test_the_comparison_indicator_is_built_from_the_dict_and_inherits_the_version(self):
        sig = rule("SMA_PRICE", 20, "EMA_PRICE", 50, version=1)
        other = comparison_signal(sig)
        self.assertEqual((other.name, other.window, other.version), ("EMA_PRICE", 50, 1))
        self.assertEqual(other.compare_to, None)

    def test_indicator_signals_lists_both_sides_of_every_rule(self):
        plain = Signal("RSI", True, True, 30, window=14)
        compared = rule("SMA_PRICE", 20, "SMA_PRICE", 50)
        self.assertEqual([s.name + str(s.window) for s in indicator_signals([plain, compared])],
                         ["RSI14", "SMA_PRICE20", "SMA_PRICE50"])


class SignalServiceComparisonTests(SimpleTestCase):
    def test_both_columns_are_calculated(self):
        out = SignalService.calculate_indicators(frame(), [rule("SMA_PRICE", 2, "SMA_PRICE", 4)])
        self.assertIn("SMA_PRICE_2", out)
        self.assertIn("SMA_PRICE_4", out)

    def test_a_comparison_with_a_different_kind_of_average_works(self):
        out = SignalService.calculate_indicators(frame(), [rule("EMA_PRICE", 3, "SMA_PRICE", 5)])
        self.assertIn("EMA_PRICE_3", out)
        self.assertIn("SMA_PRICE_5", out)

    def test_a_column_that_is_already_there_is_reused(self):
        data = frame()
        data["SMA_PRICE_4"] = -1.0
        out = SignalService.calculate_indicators(data, [rule("SMA_PRICE", 2, "SMA_PRICE", 4)])
        self.assertTrue((out["SMA_PRICE_4"] == -1.0).all())

    def test_an_unknown_second_indicator_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not recognized"):
            SignalService.calculate_indicators(frame(), [rule("SMA_PRICE", 2, "FOO", 4)])


def reference_buys(data, signals):
    """The comparison exactly as it was written before comparison rules: one indicator against a typed number."""
    out = data.copy()
    out["buy"] = True
    for sig in signals:
        if sig.buy_signal:
            col = BacktestService.indicator_column(sig)
            cond = out[col] > sig.target_value if sig.above_target else out[col] < sig.target_value
            out["buy"] = out["buy"] & cond
    return out


def reference_sells(data, signals):
    out = data.copy()
    sells = [s for s in signals if not s.buy_signal]
    out["sell"] = bool(sells)
    for sig in signals:
        if not sig.buy_signal:
            col = BacktestService.indicator_column(sig)
            cond = out[col] > sig.target_value if sig.above_target else out[col] < sig.target_value
            out["sell"] = out["sell"] & cond
    return out


class OldRulesAreUnchangedTests(SimpleTestCase):
    """The evaluator was rewritten to serve both kinds of rule; every rule that only has a typed number must give the
    same answers, row for row, as the old code."""

    SERIES = frame([100 + 6 * math.sin(i / 3) + 2 * math.sin(i * 1.7) + i * 0.05 for i in range(120)])
    INDICATORS = {
        "RSI": {"window": 5}, "SMA": {"window": 5}, "EMA": {"window": 5}, "SO": {"window": 5}, "BBP": {"window": 5},
        "ADX": {"window": 5}, "MACD": {"fast_window": 3, "slow_window": 6, "signal_window": 3},
    }

    def test_every_indicator_in_both_directions_for_buys_and_sells(self):
        for name, windows in self.INDICATORS.items():
            for above in (True, False):
                for target in (-0.5, 0.0, 0.5, 1.0, 30.0, 50.0, 70.0):
                    signals = [
                        Signal(name, above, True, target, **windows),
                        Signal(name, not above, False, target + 1, **windows),
                    ]
                    with self.subTest(indicator=name, above=above, target=target):
                        data = SignalService.calculate_indicators(self.SERIES, signals)
                        new = BacktestService.find_sells(BacktestService.find_buys(data, signals), signals)
                        old = reference_sells(reference_buys(data, signals), signals)
                        pd.testing.assert_series_equal(new["buy"], old["buy"])
                        pd.testing.assert_series_equal(new["sell"], old["sell"])

    def test_no_sell_rules_still_means_never_sell(self):
        signals = [Signal("RSI", False, True, 40, window=5)]
        data = SignalService.calculate_indicators(self.SERIES, signals)
        self.assertFalse(BacktestService.find_sells(data, signals)["sell"].any())


class ComparisonRuleTests(SimpleTestCase):
    # fast = 2-candle average, slow = 4-candle average, over a series that rises, falls and rises again.
    SERIES = [10.0, 11, 12, 13, 14, 13, 12, 11, 10, 11, 12, 13, 14]

    def evaluate(self, signals, closes=None):
        data = SignalService.calculate_indicators(frame(closes or self.SERIES), signals)
        return BacktestService.find_sells(BacktestService.find_buys(data, signals), signals)

    def test_fast_above_slow_matches_hand_calculation(self):
        out = self.evaluate([rule("SMA_PRICE", 2, "SMA_PRICE", 4)])
        close = pd.Series(self.SERIES)
        fast, slow = close.rolling(2).mean(), close.rolling(4).mean()
        self.assertEqual(out["buy"].tolist(), (fast > slow).tolist())
        # while the slow average has no value yet (the first three candles) the rule is false
        self.assertEqual(out["buy"].tolist()[:3], [False, False, False])
        self.assertTrue(out["buy"].any() and not out["buy"].all())

    def test_below_is_the_opposite_comparison(self):
        above = self.evaluate([rule("SMA_PRICE", 2, "SMA_PRICE", 4, above=True)])["buy"]
        below = self.evaluate([rule("SMA_PRICE", 2, "SMA_PRICE", 4, above=False)])["buy"]
        # equal averages are neither above nor below; there is a run of strictly-above and strictly-below
        self.assertFalse((above & below).any())
        self.assertTrue((above | below).sum() >= len(self.SERIES) - 3 - 1)

    def test_target_value_is_ignored_when_compare_to_is_set(self):
        a = self.evaluate([rule("SMA_PRICE", 2, "SMA_PRICE", 4, target=0.0)])["buy"]
        b = self.evaluate([rule("SMA_PRICE", 2, "SMA_PRICE", 4, target=999.0)])["buy"]
        self.assertEqual(a.tolist(), b.tolist())

    def test_a_crossover_strategy_buys_when_fast_rises_above_and_sells_when_it_falls_below(self):
        signals = [rule("SMA_PRICE", 2, "SMA_PRICE", 4, buy=True, above=True),
                   rule("SMA_PRICE", 2, "SMA_PRICE", 4, buy=False, above=False)]
        out = self.evaluate(signals)
        results = BacktestService.calculate_returns(out)
        owned = results["owned"].tolist()
        self.assertIn(1, owned)
        self.assertIn(0, owned[owned.index(1):])          # bought, later sold
        first_buy = owned.index(1)
        self.assertTrue(out["buy"].iloc[first_buy])       # it bought on a candle where fast was above slow
        sold_at = first_buy + owned[first_buy:].index(0)
        self.assertTrue(out["sell"].iloc[sold_at])        # and sold on a candle where fast was below slow

    def test_a_comparison_rule_and_an_ordinary_rule_must_both_hold(self):
        both = self.evaluate([rule("SMA_PRICE", 2, "SMA_PRICE", 4),
                              Signal("SMA", True, True, 0.0, window=2)])["buy"]
        only_cross = self.evaluate([rule("SMA_PRICE", 2, "SMA_PRICE", 4)])["buy"]
        self.assertFalse((both & ~only_cross).any())

    def test_comparing_an_average_with_itself_is_never_above_or_below(self):
        out = self.evaluate([rule("SMA_PRICE", 3, "SMA_PRICE", 3)])
        self.assertFalse(out["buy"].any())

    def test_a_missing_value_on_either_side_makes_the_rule_false(self):
        data = SignalService.calculate_indicators(frame(), [rule("SMA_PRICE", 2, "SMA_PRICE", 6)])
        holds = BacktestService.rule_holds(data, rule("SMA_PRICE", 2, "SMA_PRICE", 6))
        self.assertFalse(holds.iloc[:5].any())

    def test_ema_against_sma_works_and_matches_hand_calculation(self):
        out = self.evaluate([rule("EMA_PRICE", 3, "SMA_PRICE", 5)])
        close = pd.Series(self.SERIES)
        expected = close.ewm(span=3, adjust=False).mean() > close.rolling(5).mean()
        self.assertEqual(out["buy"].tolist(), expected.tolist())


class WarmUpTests(SimpleTestCase):
    def test_the_slower_side_of_a_comparison_sets_the_warm_up(self):
        self.assertEqual(BacktestService.warm_up_candles([rule("SMA_PRICE", 20, "SMA_PRICE", 50)]), 50)

    def test_the_comparison_indicator_counts_even_when_it_is_the_longer_one(self):
        self.assertEqual(BacktestService.warm_up_candles([rule("SMA_PRICE", 50, "SMA_PRICE", 20)]), 50)
        self.assertEqual(BacktestService.warm_up_candles([rule("SMA_PRICE", 5, "SMA_PRICE", 200)]), 200)

    def test_recursive_averages_get_several_windows_to_settle(self):
        self.assertEqual(BacktestService.warm_up_candles([rule("EMA_PRICE", 10, "SMA_PRICE", 30)]), 40)
        self.assertEqual(BacktestService.warm_up_candles([rule("SMA_PRICE", 20, "EMA_PRICE", 30)]), 120)

    def test_other_rules_in_the_strategy_still_count(self):
        signals = [rule("SMA_PRICE", 20, "SMA_PRICE", 50), Signal("ADX", True, True, 25, window=14)]
        self.assertEqual(BacktestService.warm_up_candles(signals), 84)

    def test_rules_without_a_comparison_are_unchanged(self):
        self.assertEqual(BacktestService.warm_up_candles([Signal("SMA", True, True, 1, window=30)]), 30)
        self.assertEqual(BacktestService.warm_up_candles([Signal("EMA", True, True, 1, window=30)]), 120)
        self.assertEqual(BacktestService.warm_up_candles([]), 0)
