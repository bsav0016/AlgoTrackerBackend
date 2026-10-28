from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from api import push_sweep
from api.models import Notification, PushDelivery, PushDevice
from api.tasks import send_notification_task
from api.tests.helpers import FakeExpo
from api.tests.test_strategy import RISING, make_strategy, make_user


class SweepTestCase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.a = PushDevice.register(self.user, "ExponentPushToken[a]")[0]
        self.b = PushDevice.register(self.user, "ExponentPushToken[b]")[0]
        self.expo = FakeExpo().install(self)
        patcher = mock.patch("django_rq.get_queue")
        patcher.start()
        self.addCleanup(patcher.stop)
        # The queue runs the job straight away, as a worker would a moment later.
        patcher = mock.patch.object(send_notification_task, "delay", side_effect=send_notification_task)
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def owed_notification(self):
        """A trade whose notification was written but whose send job never ran (the worker died after the commit)."""
        strategy = make_strategy(self.user, position=1)
        with self.captureOnCommitCallbacks(execute=False):
            strategy.check_signals(RISING)
        return Notification.objects.get()

    def status(self, device):
        return PushDelivery.objects.get(device=device).status


class RequeueMissedTests(SweepTestCase):
    def test_a_notification_whose_job_never_ran_is_sent(self):
        notification = self.owed_notification()
        now = timezone.now() + timedelta(minutes=2)
        self.assertEqual(push_sweep.sweep(now), (1, 0))
        self.delay.assert_called_once_with(notification.pk)
        self.assertEqual(len(self.expo.sent), 2)
        self.assertEqual(self.status(self.a), "sent")

    def test_a_delivery_that_is_only_just_due_is_left_to_its_own_job(self):
        self.owed_notification()
        self.assertEqual(push_sweep.requeue_missed(timezone.now()), 0)
        self.delay.assert_not_called()

    def test_a_lost_retry_is_picked_up_once_it_is_overdue(self):
        self.expo.fail_request = ConnectionError("network down")
        notification = self.owed_notification()
        send_notification_task(notification.pk)  # fails, schedules a retry that then gets lost
        self.assertEqual(self.status(self.a), "pending")
        self.expo.fail_request = None
        PushDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(minutes=2))  # the retry never came
        self.assertEqual(push_sweep.requeue_missed(timezone.now()), 1)
        self.assertEqual(self.status(self.a), "sent")

    def test_sent_and_finished_deliveries_are_not_requeued(self):
        notification = self.owed_notification()
        send_notification_task(notification.pk)
        self.assertEqual(push_sweep.requeue_missed(timezone.now() + timedelta(hours=1)), 0)
        self.assertEqual(len(self.expo.sent), 2)


class ReceiptTests(SweepTestCase):
    def sent_notification(self):
        notification = self.owed_notification()
        send_notification_task(notification.pk)
        return notification

    def later(self, minutes):
        return timezone.now() + timedelta(minutes=minutes)

    def test_receipts_are_not_requested_before_they_are_due(self):
        self.sent_notification()
        self.assertEqual(push_sweep.settle_receipts(self.later(5)), 0)
        self.assertEqual(self.expo.receipt_requests, 0)

    def test_an_ok_receipt_marks_the_delivery_delivered(self):
        self.sent_notification()
        self.assertEqual(push_sweep.settle_receipts(self.later(20)), 2)
        self.assertEqual({d.status for d in PushDelivery.objects.all()}, {"delivered"})
        self.assertEqual(push_sweep.settle_receipts(self.later(25)), 0)  # settled rows are not asked about again
        self.assertEqual(self.expo.receipt_requests, 1)

    def test_an_unregistered_receipt_deactivates_the_device(self):
        self.sent_notification()
        self.expo.receipt_errors[self.a.token] = "DeviceNotRegistered"
        push_sweep.settle_receipts(self.later(20))
        self.assertEqual((self.status(self.a), self.status(self.b)), ("dead_token", "delivered"))
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.active, self.b.active), (False, True))

    def test_another_receipt_error_marks_the_delivery_failed(self):
        self.sent_notification()
        self.expo.receipt_errors[self.a.token] = "InvalidCredentials"
        push_sweep.settle_receipts(self.later(20))
        delivery = PushDelivery.objects.get(device=self.a)
        self.assertEqual((delivery.status, delivery.last_error), ("failed", "error: InvalidCredentials"))

    def test_a_receipt_that_is_not_ready_is_asked_for_again_later(self):
        self.sent_notification()
        self.expo.receipts_ready = False
        self.assertEqual(push_sweep.settle_receipts(self.later(20)), 0)
        self.assertEqual(self.status(self.a), "sent")
        self.expo.receipts_ready = True
        self.assertEqual(push_sweep.settle_receipts(self.later(40)), 2)

    def test_a_failed_receipt_request_changes_nothing(self):
        self.sent_notification()
        self.expo.fail_request = ConnectionError("network down")
        self.assertEqual(push_sweep.settle_receipts(self.later(20)), 0)
        self.assertEqual(self.status(self.a), "sent")

    def test_old_sent_deliveries_stop_being_checked(self):
        self.sent_notification()
        self.assertEqual(push_sweep.settle_receipts(self.later(25 * 60)), 0)
        self.assertEqual(self.expo.receipt_requests, 0)


class BacklogAndCommandTests(SweepTestCase):
    def test_backlog_counts_pending_and_failed_and_the_oldest_wait(self):
        self.assertEqual(push_sweep.backlog(), {
            "pending_deliveries": 0, "failed_deliveries_24h": 0, "oldest_pending_seconds": None})
        self.owed_notification()
        PushDelivery.objects.filter(device=self.b).update(status="failed")
        stats = push_sweep.backlog(timezone.now() + timedelta(seconds=90))
        self.assertEqual((stats["pending_deliveries"], stats["failed_deliveries_24h"]), (1, 1))
        self.assertAlmostEqual(stats["oldest_pending_seconds"], 90, delta=3)

    def test_the_command_runs_one_pass_with_once(self):
        with mock.patch("api.management.commands.push_sweep.sweep", return_value=(0, 0)) as sweep:
            call_command("push_sweep", "--once")
        sweep.assert_called_once_with()

    def test_the_command_survives_a_failing_pass(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db blip")
            return (0, 0)

        with mock.patch("api.management.commands.push_sweep.sweep", side_effect=flaky), \
                mock.patch("api.management.commands.push_sweep.time.sleep", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):  # ends the otherwise endless loop
                call_command("push_sweep", "--interval", "1")
        self.assertEqual(len(calls), 2)
