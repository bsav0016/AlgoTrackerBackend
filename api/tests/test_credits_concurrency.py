"""Concurrent credit changes. These need row locks, which SQLite does not have, so they only run on Postgres.

Locally: start Postgres (`docker run --rm -p 5433:5432 -e POSTGRES_PASSWORD=pw postgres:16`), then run
`TEST_DATABASE_URL=postgres://postgres:pw@127.0.0.1:5433/postgres python manage.py test api.tests.test_credits_concurrency`.
CI runs them against a Postgres service container.
"""
import threading
import unittest
from datetime import timedelta

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from api import credits
from api.models import CreditTransaction, CustomUser


def run_in_threads(count, work):
    """Run `work(index)` in `count` threads released together; returns the results (or exceptions) in order."""
    results = [None] * count
    start = threading.Barrier(count)

    def target(index):
        try:
            start.wait()
            results[index] = work(index)
        except Exception as exc:  # reported to the test, not lost in the thread
            results[index] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


@unittest.skipUnless(connection.vendor == "postgresql", "needs Postgres row locks (set TEST_DATABASE_URL)")
class ConcurrentCreditTests(TransactionTestCase):
    def make_user(self, monthly=0, account=0):
        return CustomUser.objects.create_user(
            username="alice", email="alice@example.com", password="x", first_name="A", last_name="B",
            monthly_credits=monthly, account_credits=account)

    def test_parallel_spends_never_take_more_than_the_balance(self):
        user = self.make_user(monthly=3, account=2)  # five credits, eight one-credit spends
        results = run_in_threads(8, lambda i: credits.spend(user.pk, 1, f"spend:{i}", "backtest"))

        paid = [r for r in results if isinstance(r, credits.CreditResult)]
        refused = [r for r in results if isinstance(r, credits.InsufficientCredits)]
        self.assertEqual((len(paid), len(refused)), (5, 3), results)
        user.refresh_from_db()
        self.assertEqual((user.monthly_credits, user.account_credits), (0, 0))
        self.assertEqual(CreditTransaction.objects.count(), 5)

    def test_parallel_deliveries_of_one_event_credit_once(self):
        user = self.make_user()
        results = run_in_threads(8, lambda i: credits.apply(user.pk, {"account": 500}, "revenuecat:tx1", "purchase"))

        self.assertTrue(all(isinstance(r, credits.CreditResult) for r in results), results)
        self.assertEqual(sum(1 for r in results if not r.replayed), 1)
        user.refresh_from_db()
        self.assertEqual(user.account_credits, 500)
        self.assertEqual(CreditTransaction.objects.count(), 1)

    def test_a_purchase_and_a_charge_at_the_same_time_both_count(self):
        user = self.make_user(monthly=0, account=10)

        def work(index):
            if index % 2:
                return credits.apply(user.pk, {"account": 100}, f"buy:{index}", "purchase")
            return credits.spend(user.pk, 1, f"charge:{index}", "backtest")

        results = run_in_threads(8, work)
        self.assertTrue(all(isinstance(r, credits.CreditResult) for r in results), results)
        user.refresh_from_db()
        self.assertEqual(user.account_credits, 10 + 4 * 100 - 4)

    def test_the_monthly_reset_happens_once_when_requests_race(self):
        user = self.make_user(monthly=40)
        CustomUser.objects.filter(pk=user.pk).update(reset_monthly_credits=timezone.now() - timedelta(days=1))
        results = run_in_threads(6, lambda i: credits.reset_monthly(user.pk, 500))

        self.assertEqual(sum(1 for r in results if r is True), 1, results)
        user.refresh_from_db()
        self.assertEqual(user.monthly_credits, 500)
        self.assertEqual(CreditTransaction.objects.count(), 1)
