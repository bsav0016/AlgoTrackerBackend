"""Indicators and annualisation checked against the standard definitions.

Tests marked expectedFailure describe the standard result and currently fail: the app's RSI, ADX and MACD value
are not the standard ones (see issue #67). Whoever fixes an indicator removes its marker; a marked test that starts
passing fails the suite, so the markers cannot go stale.
"""
import math
import unittest

import pandas as pd
from django.test import SimpleTestCase

from api.indicators.adx import calculate_adx
from api.indicators.macd import calculate_macd
from api.indicators.rsi import calculate_rsi
from api.services.backtest_service import BacktestService
from api.tests.reference_indicators import macd_lines, wilder_adx, wilder_rsi
from api.tests.test_indicators import make_signal, prices

SECONDS = {"1min": 60, "1day": 86400}
BTC_HOURS = 24 * 365          # trades round the clock
STOCK_HOURS = 252 * 6.5       # 252 sessions of 6.5 hours


def synthetic_ohlc(count=120):
    """A deterministic wandering price with a trend change, so RSI and ADX move through their ranges."""
    close = [100 + 8 * math.sin(i / 6) + 0.15 * i + 2 * math.sin(i * 1.7) for i in range(count)]
    return close, [c + 1 + 0.5 * abs(math.sin(i)) for i, c in enumerate(close)], [c - 1 - 0.5 * abs(math.cos(i)) for i, c in enumerate(close)]


class ReferenceDefinitionTests(SimpleTestCase):
    """The reference loops themselves, against numbers worked out by hand."""

    def test_wilder_rsi_by_hand(self):
        # closes 10 11 10 12 13 12, n=3: seed gain 3/3=1, loss 1/3 -> RS 3 -> 75.
        # next: gain (1*2+1)/3=1, loss (1/3*2+0)/3=2/9 -> RS 4.5 -> 81.818; then gain 2/3, loss 13/27 -> 58.065
        got = wilder_rsi([10, 11, 10, 12, 13, 12], 3)
        self.assertEqual(got[:3], [None, None, None])
        self.assertAlmostEqual(got[3], 75.0, places=6)
        self.assertAlmostEqual(got[4], 100 - 100 / 5.5, places=6)
        self.assertAlmostEqual(got[5], 100 - 100 / (1 + (2 / 3) / (13 / 27)), places=6)

    def test_wilder_adx_is_100_in_a_clean_uptrend_and_starts_at_index_2n_minus_1(self):
        close = [10 + i for i in range(10)]
        got = wilder_adx([c + 0.5 for c in close], [c - 0.5 for c in close], close, 3)
        self.assertEqual(got[:5], [None] * 5)
        self.assertAlmostEqual(got[5], 100.0)
        self.assertAlmostEqual(got[-1], 100.0)


class RsiReferenceTests(SimpleTestCase):
    @unittest.expectedFailure
    def test_rsi_matches_wilder_on_the_hand_worked_series(self):
        out = calculate_rsi(prices([10, 11, 10, 12, 13, 12]), make_signal("RSI", window=3))
        for got, want in zip(out["RSI_3"].iloc[3:], [75.0, 100 - 100 / 5.5, 58.0645]):
            self.assertAlmostEqual(got, want, places=3)

    @unittest.expectedFailure
    def test_rsi_matches_wilder_over_a_long_series(self):
        close, high, low = synthetic_ohlc()
        out = calculate_rsi(prices(close, low=low, high=high), make_signal("RSI", window=14))["RSI_14"]
        for i, want in enumerate(wilder_rsi(close, 14)):
            if want is not None:
                self.assertAlmostEqual(out.iloc[i], want, places=3, msg=f"candle {i}")

    @unittest.expectedFailure
    def test_rsi_has_no_value_before_n_price_changes_exist(self):
        out = calculate_rsi(prices([10, 11, 10, 12, 13, 12]), make_signal("RSI", window=3))
        self.assertTrue(out["RSI_3"].iloc[:3].isna().all())


class AdxReferenceTests(SimpleTestCase):
    def test_adx_is_100_in_a_clean_uptrend(self):
        close = [10 + i for i in range(10)]
        out = calculate_adx(prices(close, low=[c - 0.5 for c in close], high=[c + 0.5 for c in close]),
                            make_signal("ADX", window=3))
        self.assertAlmostEqual(out["ADX_3"].iloc[-1], 100.0)

    @unittest.expectedFailure
    def test_adx_matches_wilder_over_a_long_series(self):
        close, high, low = synthetic_ohlc()
        out = calculate_adx(prices(close, low=low, high=high), make_signal("ADX", window=14))["ADX_14"]
        for i, want in enumerate(wilder_adx(high, low, close, 14)):
            if want is not None:
                self.assertAlmostEqual(out.iloc[i], want, places=2, msg=f"candle {i}")


class MacdReferenceTests(SimpleTestCase):
    @unittest.expectedFailure
    def test_macd_value_is_positive_exactly_when_the_macd_line_is_above_its_signal_line(self):
        # The app divides MACD by its signal line, so the sign flips whenever the signal line is negative.
        close, high, low = synthetic_ohlc()
        line, signal_line = macd_lines(close, 12, 26, 9)
        out = calculate_macd(prices(close, low=low, high=high),
                             make_signal("MACD", fast_window=12, slow_window=26, signal_window=9))["MACD_12_26_9"]
        for i in range(26, len(close)):
            self.assertEqual(out.iloc[i] > 0, line[i] > signal_line[i], f"candle {i}")


class AnnualisationTests(SimpleTestCase):
    """Sharpe and Sortino are scaled by sqrt(candles per year). The number after the colon in SYMBOLS is the
    hours the market trades in a year; these pin what that gives for a 24/7 symbol and a stock."""

    def test_round_the_clock_market(self):
        self.assertAlmostEqual(BacktestService.periods_per_year(BTC_HOURS, SECONDS["1min"]), 525_600)
        self.assertAlmostEqual(BacktestService.periods_per_year(BTC_HOURS, SECONDS["1day"]), 365)

    def test_stock_with_intraday_candles(self):
        self.assertAlmostEqual(BacktestService.periods_per_year(STOCK_HOURS, SECONDS["1min"]), 98_280)

    @unittest.expectedFailure
    def test_stock_with_daily_candles_has_one_per_session(self):
        # A daily candle is one session, so a year has 252, not 1638 hours / 24.
        self.assertAlmostEqual(BacktestService.periods_per_year(STOCK_HOURS, SECONDS["1day"]), 252)

    def test_the_documented_example_values_are_not_hours_per_year(self):
        # README and test settings say SYMBOLS=AAPL:1,BTC/USD:24, which this formula reads as 1 and 24 trading
        # hours a year: one daily candle a year for BTC. Pinned so a change to either side is noticed.
        self.assertAlmostEqual(BacktestService.periods_per_year(24, SECONDS["1day"]), 1)
