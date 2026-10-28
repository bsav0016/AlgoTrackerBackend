from datetime import timedelta
from unittest import mock

import pandas as pd
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from api.tests.helpers import FakeExpo, ledger
from api.models import CreditTransaction, CustomUser, Notification, PushDelivery, PushDevice, Strategy, Trade
from api.tasks import MAX_DELIVERY_ATTEMPTS, send_notification_task

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
        self.expo = FakeExpo().install(self)
        # The queue runs the job straight away; a real worker would pick it up after the commit.
        patcher = mock.patch.object(send_notification_task, "delay", side_effect=send_notification_task)
        patcher.start()
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
        self.assertEqual(len(self.expo.sent), 2)
        self.assertIn(("ExponentPushToken[a]", "Momentum triggered a buy signal!"), self.expo.sent)
        self.assertIn(("ExponentPushToken[b]", "Momentum triggered a buy signal!"), self.expo.sent)

    def test_sell_signal_while_holding_records_a_sell_and_notifies(self):
        strategy = make_strategy(self.user, position=2)
        self.check(strategy, FALLING)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 0)
        self.assertFalse(Trade.objects.get().is_buy)
        self.assertIn(("ExponentPushToken[a]", "Momentum triggered a sell signal!"), self.expo.sent)

    def test_sell_signal_without_a_position_does_nothing(self):
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, FALLING)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 1)
        self.assertFalse(Trade.objects.exists())
        self.assertEqual(self.expo.sent, [])

    def test_buy_signal_while_already_holding_does_not_buy_again(self):
        strategy = make_strategy(self.user, position=2)
        self.check(strategy, RISING)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 2)
        self.assertFalse(Trade.objects.exists())
        self.assertEqual(self.expo.sent, [])

    def test_a_fresh_buy_settles_to_held_on_the_next_check(self):
        strategy = make_strategy(self.user, position=3)
        self.check(strategy, RISING)
        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 2)
        self.assertEqual(self.expo.sent, [])

    def test_user_without_devices_is_not_notified_but_trade_is_still_recorded(self):
        self.user.push_devices.all().delete()
        strategy = make_strategy(self.user, position=1)
        self.check(strategy, RISING)
        self.assertEqual(self.expo.sent, [])
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
            self.assertEqual(self.expo.sent, [])  # the trade is not committed yet
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(self.expo.sent), 2)

    def test_no_push_goes_out_when_the_transaction_rolls_back(self):
        strategy = make_strategy(self.user, position=1)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError), transaction.atomic():
                with mock.patch.object(Trade, "save", side_effect=RuntimeError("db down")):
                    strategy.check_signals(RISING)
        self.assertEqual(callbacks, [])
        self.assertEqual(self.expo.sent, [])

    def test_a_failing_device_does_not_stop_the_others(self):
        strategy = make_strategy(self.user, position=1)
        self.expo.errors["ExponentPushToken[a]"] = "MessageRateExceeded"
        with mock.patch("django_rq.get_queue"):
            self.check(strategy, RISING)  # must not raise
        self.assertEqual(self.expo.sent, [("ExponentPushToken[b]", "Momentum triggered a buy signal!")])
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


class NotificationRecordTests(TestCase):
    """The owed push is a row written with the trade, before anything is sent."""

    def setUp(self):
        self.user = make_user()
        self.device = PushDevice.register(self.user, "ExponentPushToken[a]")[0]
        patcher = mock.patch.object(send_notification_task, "delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def test_trade_notification_and_deliveries_are_written_together(self):
        strategy = make_strategy(self.user, position=1)
        with self.captureOnCommitCallbacks(execute=True):
            strategy.check_signals(RISING)
        trade = Trade.objects.get()
        notification = Notification.objects.get()
        self.assertEqual((notification.trade, notification.user, notification.strategy), (trade, self.user, strategy))
        self.assertEqual(notification.kind, "buy")
        delivery = notification.deliveries.get()
        self.assertEqual((delivery.device, delivery.status, delivery.attempts), (self.device, "pending", 0))
        self.delay.assert_called_once_with(notification.pk)

    def test_the_rows_exist_even_if_the_job_is_never_queued(self):
        strategy = make_strategy(self.user, position=1)
        strategy.check_signals(RISING)  # commit callbacks never run, as when the worker dies after the commit
        self.assertEqual(PushDelivery.objects.filter(status="pending").count(), 1)
        self.delay.assert_not_called()

    def test_inactive_devices_get_no_delivery_and_no_devices_means_no_job(self):
        PushDevice.objects.update(active=False)
        strategy = make_strategy(self.user, position=1)
        with self.captureOnCommitCallbacks(execute=True):
            strategy.check_signals(RISING)
        self.assertEqual(Trade.objects.count(), 1)
        self.assertFalse(PushDelivery.objects.exists())
        self.delay.assert_not_called()

    def test_a_rolled_back_trade_leaves_no_notification(self):
        strategy = make_strategy(self.user, position=1)
        with self.assertRaises(RuntimeError), transaction.atomic():
            with mock.patch.object(PushDelivery.objects, "bulk_create", side_effect=RuntimeError("db down")):
                strategy.check_signals(RISING)
        self.assertFalse(Trade.objects.exists() or Notification.objects.exists())


class SendNotificationJobTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.a = PushDevice.register(self.user, "ExponentPushToken[a]")[0]
        self.b = PushDevice.register(self.user, "ExponentPushToken[b]")[0]
        self.expo = FakeExpo().install(self)
        patcher = mock.patch("django_rq.get_queue")
        self.queue = patcher.start().return_value
        self.addCleanup(patcher.stop)
        strategy = make_strategy(self.user, position=1)
        with self.captureOnCommitCallbacks(execute=False):
            strategy.check_signals(RISING)
        self.notification = Notification.objects.get()

    def status(self, device):
        return PushDelivery.objects.get(device=device)

    def test_sends_one_batch_with_the_strategy_and_trade_in_the_payload(self):
        send_notification_task(self.notification.pk)
        self.assertEqual(self.expo.requests, 1)
        self.assertEqual(len(self.expo.sent), 2)
        expected = {"strategy_id": self.notification.strategy_id, "trade_id": self.notification.trade_id}
        self.assertEqual(self.expo.payloads, [expected, expected])
        delivery = self.status(self.a)
        self.assertEqual((delivery.status, delivery.attempts), ("sent", 1))
        self.assertTrue(delivery.ticket_id and delivery.sent_at)
        self.queue.enqueue_in.assert_not_called()

    def test_running_the_job_twice_sends_once(self):
        send_notification_task(self.notification.pk)
        send_notification_task(self.notification.pk)
        self.assertEqual(len(self.expo.sent), 2)
        self.assertEqual(self.status(self.a).attempts, 1)

    def test_an_unregistered_device_is_deactivated_and_not_retried(self):
        self.expo.errors[self.a.token] = "DeviceNotRegistered"
        send_notification_task(self.notification.pk)
        delivery = self.status(self.a)
        self.assertEqual((delivery.status, delivery.last_error), ("dead_token", "DeviceNotRegistered"))
        self.a.refresh_from_db()
        self.assertFalse(self.a.active)
        self.assertEqual(self.status(self.b).status, "sent")
        self.queue.enqueue_in.assert_not_called()

    def test_a_dropped_request_is_retried_later_with_backoff(self):
        self.expo.fail_request = ConnectionError("network down")
        send_notification_task(self.notification.pk)
        delivery = self.status(self.a)
        self.assertEqual((delivery.status, delivery.attempts), ("pending", 1))
        self.assertIn("network down", delivery.last_error)
        self.assertGreater(delivery.next_attempt_at, timezone.now())
        self.queue.enqueue_in.assert_called_once_with(timedelta(seconds=2), send_notification_task, self.notification.pk)

        send_notification_task(self.notification.pk)  # not due yet: nothing happens
        self.assertEqual(self.status(self.a).attempts, 1)

    def test_a_retry_that_succeeds_sends_only_the_failed_device(self):
        self.expo.errors[self.a.token] = "MessageRateExceeded"
        send_notification_task(self.notification.pk)
        self.assertEqual(self.status(self.b).status, "sent")
        self.assertEqual(self.status(self.a).status, "pending")

        self.expo.errors.clear()
        PushDelivery.objects.update(next_attempt_at=timezone.now())
        send_notification_task(self.notification.pk)
        self.assertEqual(self.status(self.a).status, "sent")
        self.assertEqual(self.expo.sent.count((self.b.token, self.notification.message)), 1)

    def test_a_delivery_gives_up_after_the_last_attempt(self):
        self.expo.fail_request = ConnectionError("network down")
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            PushDelivery.objects.update(next_attempt_at=timezone.now())
            send_notification_task(self.notification.pk)
        delivery = self.status(self.a)
        self.assertEqual((delivery.status, delivery.attempts), ("failed", MAX_DELIVERY_ATTEMPTS))
        self.assertEqual(self.queue.enqueue_in.call_count, MAX_DELIVERY_ATTEMPTS - 1)


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
