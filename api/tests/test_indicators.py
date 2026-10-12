"""Indicator values checked against numbers worked out by hand."""
import math

import pandas as pd
from django.test import SimpleTestCase

from api.indicators.adx import calculate_adx
from api.indicators.bbp import calculate_bbp
from api.indicators.ema import calculate_ema
from api.indicators.macd import calculate_macd
from api.indicators.rsi import calculate_rsi
from api.indicators.signal import Signal
from api.indicators.sma import calculate_sma
from api.indicators.so import calculate_so


def make_signal(name, **kwargs):
    return Signal(name=name, above_target=True, buy_signal=True, target_value=0, **kwargs)


def prices(close, low=None, high=None):
    close = list(close)
    return pd.DataFrame({
        "close": close,
        "low": low if low is not None else [c - 1 for c in close],
        "high": high if high is not None else [c + 1 for c in close],
    })


CLOSE = [10, 11, 12, 11, 13]


class IndicatorTests(SimpleTestCase):
    def assertSeriesAlmostEqual(self, series, expected):
        self.assertEqual(len(series), len(expected))
        for got, want in zip(series, expected):
            if want is None:
                self.assertTrue(math.isnan(got), f"expected NaN, got {got}")
            else:
                self.assertAlmostEqual(got, want, places=4)

    def test_sma_is_ratio_of_moving_average_to_close(self):
        out = calculate_sma(prices(CLOSE), make_signal("SMA", window=3))
        self.assertSeriesAlmostEqual(
            out["SMA_3"], [None, None, 11 / 12, (34 / 3) / 11, 12 / 13])

    def test_ema_is_ratio_of_exponential_average_to_close(self):
        # span=3 -> alpha=0.5: 10, 10.5, 11.25, 11.125, 12.0625
        out = calculate_ema(prices(CLOSE), make_signal("EMA", window=3))
        self.assertSeriesAlmostEqual(
            out["EMA_3"], [1.0, 10.5 / 11, 11.25 / 12, 11.125 / 11, 12.0625 / 13])

    def test_rsi_uses_average_gain_over_average_loss(self):
        out = calculate_rsi(prices(CLOSE), make_signal("RSI", window=3))
        # idx3: avg gain 2/3 / avg loss 1/3 -> RS 2 -> 66.67; idx4: gain 1 / loss 1/3 -> RS 3 -> 75
        # idx2 is already 100: the NaN first diff counts as a 0 gain/loss, so the first window
        # holds only two real changes (both gains). Pinned as current behaviour.
        self.assertSeriesAlmostEqual(out["RSI_3"], [None, None, 100, 100 - 100 / 3, 75])

    def test_rsi_is_100_when_price_never_falls(self):
        out = calculate_rsi(prices([1, 2, 3, 4, 5]), make_signal("RSI", window=3))
        self.assertEqual(out["RSI_3"].iloc[-1], 100)

    def test_bollinger_percent_b_position_inside_bands(self):
        # window [10, 11, 12]: mean 11, sample std 1 -> bands 9..13 -> (12 - 9) / 4
        out = calculate_bbp(prices(CLOSE), make_signal("BBP", window=3))
        self.assertAlmostEqual(out["BBP_3"].iloc[2], 0.75)
        self.assertTrue(math.isnan(out["BBP_3"].iloc[1]))

    def test_stochastic_oscillator_position_within_recent_range(self):
        out = calculate_so(
            prices(CLOSE, low=[9, 10, 11, 10, 12], high=[11, 12, 13, 12, 14]),
            make_signal("SO", window=3))
        self.assertSeriesAlmostEqual(out["SO_3"], [None, None, 75, 100 / 3, 75])

    def test_macd_column_name_and_values(self):
        signal = make_signal("MACD", fast_window=2, slow_window=3, signal_window=2)
        out = calculate_macd(prices([10, 11, 12]), signal)
        # MACD line 0, 1/6, 0.30556; signal line 0, 1/9, 0.24074 -> ratio
        column = out["MACD_2_3_2"]
        self.assertTrue(math.isnan(column.iloc[0]))  # 0 / 0
        self.assertAlmostEqual(column.iloc[1], 1.5, places=4)
        self.assertAlmostEqual(column.iloc[2], 1.26923, places=4)

    def test_adx_is_100_in_a_pure_uptrend(self):
        close = [10 + i for i in range(8)]
        out = calculate_adx(prices(close, low=[c - 0.5 for c in close],
                                   high=[c + 0.5 for c in close]), make_signal("ADX", window=2))
        self.assertAlmostEqual(out["ADX_2"].iloc[-1], 100)

    def test_adx_stays_between_0_and_100_for_choppy_prices(self):
        close = [10, 12, 9, 13, 8, 14, 9, 12, 10, 11, 9, 13]
        out = calculate_adx(prices(close), make_signal("ADX", window=3)).dropna(subset=["ADX_3"])
        self.assertFalse(out.empty)
        self.assertTrue(out["ADX_3"].between(0, 100).all())

    def test_indicators_do_not_mutate_the_input_frame(self):
        data = prices(CLOSE)
        before = data.copy()
        calculate_sma(data, make_signal("SMA", window=3))
        calculate_rsi(data, make_signal("RSI", window=3))
        pd.testing.assert_frame_equal(data, before)

    def test_window_longer_than_data_yields_only_nan(self):
        out = calculate_sma(prices(CLOSE), make_signal("SMA", window=50))
        self.assertTrue(out["SMA_50"].isna().all())
