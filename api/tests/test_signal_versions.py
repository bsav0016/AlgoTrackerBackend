"""Indicator definitions are versioned: strategies saved before versions existed keep the original RSI, ADX and
MACD value, and everything new gets the standard ones."""
import pandas as pd
from django.test import SimpleTestCase, TestCase

from api.indicators.signal import CURRENT_VERSION, LEGACY_VERSION, Signal
from api.models import CustomUser, Strategy
from api.serializers import BacktestRequestSerializer, SignalSerializer, StrategySerializer, StrategyRequestSerializer
from api.services.signal_service import SignalService
from api.tests.test_indicator_reference import synthetic_ohlc
from api.tests.test_indicators import prices

RSI = {"name": "RSI", "above_target": False, "target_value": 30.0, "window": 14}
BUY = {"above_target": True, "buy_signal": True, "target_value": 0.0, "name": "RSI", "window": 14}


def rsi_values(version):
    close, high, low = synthetic_ohlc()
    signal = Signal(name="RSI", above_target=True, buy_signal=True, target_value=0, window=14, version=version)
    return SignalService.calculate_indicators(prices(close, low=low, high=high), [signal])["RSI_14"]


class DispatchTests(SimpleTestCase):
    def test_new_signals_use_the_current_version(self):
        self.assertEqual(Signal(name="RSI", above_target=True, buy_signal=True, target_value=0, window=3).version, CURRENT_VERSION)
        self.assertEqual(CURRENT_VERSION, 2)

    def test_each_version_computes_its_own_definition(self):
        old, new = rsi_values(LEGACY_VERSION), rsi_values(CURRENT_VERSION)
        self.assertFalse(old.dropna().equals(new.dropna()))
        self.assertTrue(old.iloc[13:].notna().all())   # version 1 starts one candle early
        self.assertTrue(new.iloc[:14].isna().all())


class StoredStrategyTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(username="u", email="u@example.com", password="pw-for-tests-123", first_name="U", last_name="U")

    def test_signals_stored_without_a_version_are_version_1(self):
        strategy = Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day", buy_signals=[RSI], sell_signals=[RSI])
        self.assertEqual({s.version for s in strategy.combine_signals()}, {LEGACY_VERSION})

    def test_a_stored_version_is_kept(self):
        strategy = Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day",
                                           buy_signals=[{**RSI, "version": 2}], sell_signals=[])
        self.assertEqual([s.version for s in strategy.combine_signals()], [2])

    def test_saving_signals_records_their_version(self):
        strategy = Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day")
        strategy.add_signals([Signal(name="RSI", above_target=True, buy_signal=True, target_value=50, window=14)])
        strategy.refresh_from_db()
        self.assertEqual(strategy.buy_signals[0]["version"], CURRENT_VERSION)

    def test_the_api_reports_version_1_for_old_strategies(self):
        strategy = Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day",
                                           buy_signals=[RSI], sell_signals=[{**RSI, "version": 2}])
        data = StrategySerializer(strategy).data
        self.assertEqual(data["buy_signals"][0]["version"], 1)
        self.assertEqual(data["sell_signals"][0]["version"], 2)


class RequestTests(SimpleTestCase):
    def signal(self, **kwargs):
        return {**BUY, **kwargs}

    def test_version_defaults_to_the_current_one(self):
        serializer = SignalSerializer(data=self.signal())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["version"], CURRENT_VERSION)

    def test_version_1_can_be_asked_for(self):
        serializer = SignalSerializer(data=self.signal(version=1))
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["version"], 1)

    def test_unknown_versions_are_refused(self):
        self.assertFalse(SignalSerializer(data=self.signal(version=3)).is_valid())

    def test_one_strategy_cannot_mix_versions(self):
        mixed = [self.signal(version=1), self.signal(version=2, buy_signal=False)]
        body = {"symbol": "AAPL", "interval": "1day", "start_date": "2026-01-01T00:00:00Z", "end_date": "2026-02-01T00:00:00Z", "signals": mixed}
        self.assertFalse(BacktestRequestSerializer(data=body).is_valid())
        self.assertFalse(StrategyRequestSerializer(data={"title": "t", "symbol": "AAPL", "interval": "1day", "signals": mixed}).is_valid())
        same = [self.signal(version=1), self.signal(version=1, buy_signal=False)]
        self.assertTrue(StrategyRequestSerializer(data={"title": "t", "symbol": "AAPL", "interval": "1day", "signals": same}).is_valid())
