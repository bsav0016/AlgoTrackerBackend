"""Replays one price series through the live path (candle store -> job -> check_signals) and through
BacktestService, and checks they hold the same position after every candle and earn the same return."""
import math

import pandas as pd
from asgiref.sync import async_to_sync

from api.indicators.signal import Signal
from api.models import Strategy, Trade
from api.redis_manager import RedisManager
from api.services.backtest_service import BacktestService
from api.services.signal_service import SignalService
from api.tasks import process_strategy_task
from api.tests.test_live_pipeline import DAY, INTERVAL, SYMBOL, LivePipelineCase

CANDLES = 90
HISTORY = 35  # candles already stored when the strategy is subscribed


def series():
    """A wavy, slowly rising series with cents, plus a high and low around each close."""
    rows = []
    for i in range(CANDLES):
        close = round(100 + 6 * math.sin(i / 3) + 2 * math.sin(i * 1.7) + i * 0.05, 2)
        rows.append({
            "timestamp": i * DAY,
            "close": close,
            "high": round(close + 0.5 + 0.3 * abs(math.sin(i)), 2),
            "low": round(close - 0.5 - 0.3 * abs(math.cos(i)), 2),
        })
    return pd.DataFrame(rows)


def signal(name, buy, target, **windows):
    return Signal(name=name, above_target=not buy, buy_signal=buy, target_value=target, **windows)


INDICATORS = {
    "RSI": {"window": 5},
    "SMA": {"window": 5},
    "EMA": {"window": 5},
    "MACD": {"fast_window": 3, "slow_window": 6, "signal_window": 3},
    "SO": {"window": 5},
    "BBP": {"window": 5},
    "ADX": {"window": 5},
}


class LiveBacktestParityTests(LivePipelineCase):
    def thresholds(self, name, windows, prices):
        """Buy below the 35th percentile of the indicator and sell above the 65th, so a wavy series trades often."""
        column = BacktestService.indicator_column(signal(name, True, 0, **windows))
        values = SignalService.calculate_indicators(prices, [signal(name, True, 0, **windows)])[column].dropna()
        return values.quantile(0.35), values.quantile(0.65)

    def backtest(self, prices, signals):
        data = SignalService.calculate_indicators(prices, signals)
        data = BacktestService.find_sells(BacktestService.find_buys(data, signals), signals)
        # Live evaluates the candle after the last stored one, comparing it with that one.
        return BacktestService.calculate_returns(data.iloc[HISTORY - 1:])

    def run_parity(self, name):
        windows = INDICATORS[name]
        prices = series()
        buy_below, sell_above = self.thresholds(name, windows, prices)
        signals = [signal(name, True, buy_below, **windows), signal(name, False, sell_above, **windows)]
        expected = self.backtest(prices, signals)

        for _, row in prices.iloc[:HISTORY].iterrows():
            async_to_sync(RedisManager.store_close_price)(
                SYMBOL, INTERVAL, row["close"], int(row["timestamp"]), high=row["high"], low=row["low"])
        strategy = Strategy.objects.create(
            user=self.user, title=name, symbol=SYMBOL, interval=INTERVAL, position=1,
            buy_signals=[{**vars(signals[0])}], sell_signals=[{**vars(signals[1])}])

        for offset, (_, row) in enumerate(prices.iloc[HISTORY:].iterrows(), start=1):
            async_to_sync(RedisManager.store_close_price)(
                SYMBOL, INTERVAL, row["close"], int(row["timestamp"]), high=row["high"], low=row["low"])
            while self.queued:
                process_strategy_task(*self.queued.pop(0))
            strategy.refresh_from_db()
            with self.subTest(indicator=name, candle=HISTORY + offset - 1):
                self.assertEqual(strategy.position >= 2, bool(expected.at[offset, "owned"]))

        owned = expected["owned"]
        expected_trades = int((owned.diff().fillna(0) != 0).sum())
        self.assertGreaterEqual(expected_trades, 2, f"{name}: the series should make the strategy trade")
        self.assertEqual(Trade.objects.count(), expected_trades)
        self.assertAlmostEqual(
            float(strategy.strategy_return), expected["return"].iloc[-1] / expected["return"].iloc[0], places=3)
        self.assertAlmostEqual(
            float(strategy.asset_return), expected["close"].iloc[-1] / expected["close"].iloc[0], places=3)

    def test_rsi(self):
        self.run_parity("RSI")

    def test_sma(self):
        self.run_parity("SMA")

    def test_ema(self):
        self.run_parity("EMA")

    def test_macd(self):
        self.run_parity("MACD")

    def test_stochastic_oscillator(self):
        self.run_parity("SO")

    def test_bollinger_percent_b(self):
        self.run_parity("BBP")

    def test_adx(self):
        self.run_parity("ADX")
