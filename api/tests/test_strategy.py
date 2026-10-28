from datetime import timedelta
from unittest import mock

import pandas as pd
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from api.tests.helpers import ledger
from api.models import CreditTransaction, CustomUser, PushDevice, Strategy, Trade
from api.services.notification_service import NotificationService

RISING = pd.DataFrame({"close": [10.0, 11.0, 12.0]})   # close above its 2-bar average
FALLING = pd.DataFrame({"close": [12.0, 11.0, 10.0]})  # close below its 2-bar average

# Buy while price is above its 2-bar average, sell once it drops below.
BUY = {"name": "SMA", "above_target": False, "target_value": 1.0, "window": 2}
SELL = {"name": "SMA", "above_target": True, "target_value": 1.0, "window": 2}


def make_user(**kwargs):
    defaults = dict(username="u", email="u@example.com", password="x", first_name="U", last_name="U")
    return CustomUser.objects.create_user(**{**defaults, **kwargs})


def make_strategy(user, position=1, **kwargs):
    strategy = Strategy.objects.create(
        user=user, title="Momentum", symbol="AAPL", interval="1day", position=position,
        buy_signals=[BUY], sell_signals=[SELL], **kwargs)
    # Reload like process_strategy_task does, so numeric fields are Decimals as in production.
    return Strategy.objects.get(pk=strategy.pk)


class CheckSignalsTests(TestCase):
    """Live signal handling. Push delivery is mocked; no notification ever leaves the process."""

    def setUp(self):
        self.user = make_user()
        for token in ("ExponentPushToken[a]", "ExponentPushToken[b]"):
            PushDevice.register(self.user, token)
        patcher = mock.patch.object(NotificationService, "send_push_message")
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, strategy, prices, ts=None):
        """check_signals as a job runs it: pushes are sent when the transaction commits."""
        with self.captureOnCommitCallbacks(execute=True):
            strategy.check_signals(prices, ts=ts)

    def test_buy_signal_records_a_trade_and_notifies_every_device(self):
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, RISING)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)
        trade = Trade.objects.get()
        self.assertTrue(trade.is_buy)
        self.assertEqual(float(trade.price), 12.0)
        self.assertEqual(self.push.call_count, 2)
        self.push.assert_any_call("ExponentPushToken[a]", "Momentum triggered a buy signal!")
        self.push.assert_any_call("ExponentPushToken[b]", "Momentum triggered a buy signal!")

    def test_sell_signal_while_holding_records_a_sell_and_notifies(self):
        strategy = make_strategy(self.user, position=2)
        self.check(strategy, FALLING)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 0)
        self.assertFalse(Trade.objects.get().is_buy)
        self.push.assert_any_call("ExponentPushToken[a]", "Momentum triggered a sell signal!")

    def test_sell_signal_without_a_position_does_nothing(self):
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, FALLING)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 1)
        self.assertFalse(Trade.objects.exists())
        self.push.assert_not_called()

    def test_buy_signal_while_already_holding_does_not_buy_again(self):
        strategy = make_strategy(self.user, position=2)
        self.check(strategy, RISING)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 2)
        self.assertFalse(Trade.objects.exists())
        self.push.assert_not_called()

    def test_a_fresh_buy_settles_to_held_on_the_next_check(self):
        strategy = make_strategy(self.user, position=3)
        self.check(strategy, RISING)
        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 2)
        self.push.assert_not_called()

    def test_user_without_devices_is_not_notified_but_trade_is_still_recorded(self):
        self.user.push_devices.all().delete()
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, RISING)
        self.push.assert_not_called()
        self.assertEqual(Trade.objects.count(), 1)

    def test_held_position_tracks_the_asset_in_both_returns(self):
        strategy = make_strategy(self.user, position=2)
        self.check(strategy, RISING)  # 11 -> 12

        strategy.refresh_from_db()
        self.assertAlmostEqual(float(strategy.strategy_return), 12 / 11, places=4)
        self.assertAlmostEqual(float(strategy.asset_return), 12 / 11, places=4)

    def test_strategy_return_is_unchanged_while_not_holding(self):
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, FALLING)  # 11 -> 10

        strategy.refresh_from_db()
        self.assertEqual(float(strategy.strategy_return), 1.0)
        self.assertAlmostEqual(float(strategy.asset_return), 10 / 11, places=4)

    def test_subscription_is_charged_once_the_billing_date_has_passed(self):
        strategy = make_strategy(self.user, position=1,
                                 charge_user_date=timezone.now() - timedelta(minutes=1))
        self.check(strategy, FALLING)

        strategy.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.user.monthly_credits, 500 - 10)  # the 1day interval costs 10 a month
        self.assertGreater(strategy.charge_user_date, timezone.now() + timedelta(days=29))

    def test_pushes_wait_for_the_transaction_to_commit(self):
        strategy = make_strategy(self.user, position=1)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            strategy.check_signals(RISING)
            self.push.assert_not_called()  # the trade is not committed yet
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(self.push.call_count, 2)

    def test_no_push_goes_out_when_the_transaction_rolls_back(self):
        strategy = make_strategy(self.user, position=1)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError), transaction.atomic():
                with mock.patch.object(Trade, "save", side_effect=RuntimeError("db down")):
                    strategy.check_signals(RISING)
        self.assertEqual(callbacks, [])
        self.push.assert_not_called()

    def test_a_failing_device_does_not_stop_the_others(self):
        strategy = make_strategy(self.user, position=1)
        self.push.side_effect = [RuntimeError("bad token"), None]
        self.check(strategy, RISING)  # must not raise
        self.assertEqual(self.push.call_count, 2)
        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)

    def test_a_zero_previous_price_does_not_crash(self):
        strategy = make_strategy(self.user, position=2)
        self.check(strategy, pd.DataFrame({"close": [0.0, 5.0]}))

        strategy.refresh_from_db()
        self.assertEqual(float(strategy.asset_return), 1.0)

    def test_a_single_candle_is_skipped_instead_of_crashing(self):
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, pd.DataFrame({"close": [10.5]}), ts=0)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 1)
        self.assertIsNone(strategy.last_processed_ts)
        self.assertEqual(Trade.objects.count(), 0)

    def test_the_candle_timestamp_is_recorded(self):
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, RISING, ts=1234)

        strategy.refresh_from_db()
        self.assertEqual(strategy.last_processed_ts, 1234)

    def test_a_strategy_deleted_for_non_payment_is_not_saved_back(self):
        CustomUser.objects.filter(pk=self.user.pk).update(monthly_credits=0, account_credits=0)
        strategy = make_strategy(self.user, position=1, charge_user_date=timezone.now() - timedelta(minutes=1))
        self.check(strategy, FALLING, ts=1)

        self.assertEqual(Strategy.objects.count(), 0)


class StrategyChargeTests(TestCase):
    def test_monthly_credits_are_used_before_account_credits(self):
        user = make_user(monthly_credits=500, account_credits=20)
        make_strategy(user).charge_user(100)
        user.refresh_from_db()
        self.assertEqual((user.monthly_credits, user.account_credits), (400, 20))

    def test_shortfall_is_deducted_from_account_credits(self):
        user = make_user(monthly_credits=50, account_credits=200)
        make_strategy(user).charge_user(100)
        user.refresh_from_db()
        self.assertEqual((user.monthly_credits, user.account_credits), (0, 150))

    def test_strategy_is_removed_and_nothing_is_charged_when_the_user_cannot_pay(self):
        user = make_user(monthly_credits=50, account_credits=40)
        strategy = make_strategy(user)
        strategy.charge_user(100)
        user.refresh_from_db()
        self.assertEqual((user.monthly_credits, user.account_credits), (50, 40))
        self.assertFalse(Strategy.objects.filter(pk=strategy.pk).exists())
        self.assertFalse(ledger().exists())

    def test_the_charge_is_recorded_against_the_strategy_and_its_billing_date(self):
        user = make_user(monthly_credits=500, account_credits=0)
        strategy = make_strategy(user)
        due = int(strategy.charge_user_date.timestamp())
        strategy.charge_user(100)
        row = ledger().get()
        self.assertEqual((row.kind, row.amount, row.idempotency_key),
                         ("subscription", -100, f"subscription:{strategy.pk}:{due}"))
        self.assertGreater(strategy.charge_user_date, timezone.now() + timedelta(days=29))

    def test_running_the_same_billing_date_twice_charges_once(self):
        user = make_user(monthly_credits=500, account_credits=0)
        strategy = make_strategy(user)
        due = strategy.charge_user_date
        strategy.charge_user(100)
        strategy.charge_user_date = due  # a retried job that never saved the new billing date
        strategy.charge_user(100)
        user.refresh_from_db()
        self.assertEqual(user.monthly_credits, 400)
        self.assertEqual(ledger().count(), 1)


class StrategySignalStorageTests(TestCase):
    def test_signals_round_trip_through_storage_with_buy_and_sell_sides_preserved(self):
        from api.indicators.signal import Signal
        user = make_user()
        strategy = Strategy.objects.create(user=user, symbol="AAPL", interval="1day")
        signals = [
            Signal(name="RSI", above_target=False, buy_signal=True, target_value=30, window=14),
            Signal(name="MACD", above_target=True, buy_signal=False, target_value=1,
                   fast_window=12, slow_window=26, signal_window=9),
        ]
        strategy.add_signals(signals)
        strategy.refresh_from_db()
        self.assertEqual(strategy.combine_signals(), signals)
