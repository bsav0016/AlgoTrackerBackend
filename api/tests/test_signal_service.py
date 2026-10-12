import pandas as pd
from django.test import SimpleTestCase

from api.indicators.signal import Signal
from api.services.signal_service import SignalService


def signal(name, **kwargs):
    return Signal(name=name, above_target=True, buy_signal=True, target_value=1, **kwargs)


FRAME = pd.DataFrame({
    "close": [10.0, 11, 12, 11, 13, 12, 14],
    "low": [9.0, 10, 11, 10, 12, 11, 13],
    "high": [11.0, 12, 13, 12, 14, 13, 15],
})


class SignalServiceTests(SimpleTestCase):
    def test_each_supported_indicator_adds_its_column(self):
        cases = [
            (signal("RSI", window=3), "RSI_3"),
            (signal("SMA", window=3), "SMA_3"),
            (signal("EMA", window=3), "EMA_3"),
            (signal("SO", window=3), "SO_3"),
            (signal("BBP", window=3), "BBP_3"),
            (signal("ADX", window=2), "ADX_2"),
            (signal("MACD", fast_window=2, slow_window=3, signal_window=2), "MACD_2_3_2"),
        ]
        for sig, column in cases:
            with self.subTest(indicator=sig.name):
                self.assertIn(column, SignalService.get_indicator_values(sig, FRAME))

    def test_unknown_indicator_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not recognized"):
            SignalService.get_indicator_values(signal("FOO", window=3), FRAME)

    def test_existing_column_is_reused_not_recomputed(self):
        data = FRAME.copy()
        data["SMA_3"] = -1.0
        out = SignalService.calculate_indicators(data, [signal("SMA", window=3)])
        self.assertTrue((out["SMA_3"] == -1.0).all())

    def test_duplicate_signals_are_computed_once_and_all_columns_kept(self):
        out = SignalService.calculate_indicators(
            FRAME, [signal("SMA", window=3), signal("SMA", window=3), signal("RSI", window=3)])
        self.assertIn("SMA_3", out)
        self.assertIn("RSI_3", out)
