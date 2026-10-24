import importlib
from unittest import mock

from django.apps import apps
from django.contrib import admin
from django.db import IntegrityError
from django.test import RequestFactory, TestCase

from api import credits
from api.tests.helpers import ledger
from api.models import CreditTransaction, CustomUser


def make_user(username="alice", monthly=500, account=0):
    return CustomUser.objects.create_user(
        username=username, email=f"{username}@example.com", password="x", first_name="A", last_name="B",
        monthly_credits=monthly, account_credits=account)


def balances(user):
    user.refresh_from_db()
    return user.monthly_credits, user.account_credits


class ApplyTests(TestCase):
    def setUp(self):
        self.user = make_user(monthly=10, account=20)

    def apply(self, changes, key="k1", kind="purchase", **kwargs):
        return credits.apply(self.user.pk, changes, key, kind, **kwargs)

    def test_adds_credits_and_writes_a_ledger_row(self):
        result = self.apply({"account": 500}, meta={"product": "FundsDeposit"})
        self.assertEqual((result.account, result.monthly, result.replayed), (520, 10, False))
        self.assertEqual(balances(self.user), (10, 520))
        row = ledger().get()
        self.assertEqual((row.user, row.bucket, row.amount, row.balance_after, row.kind, row.idempotency_key),
                         (self.user, "account", 500, 520, "purchase", "k1"))
        self.assertEqual(row.meta, {"product": "FundsDeposit"})

    def test_spending_from_two_buckets_writes_one_row_per_bucket_with_the_same_key(self):
        self.apply({"monthly": -10, "account": -4}, key="bt1", kind="backtest")
        self.assertEqual(balances(self.user), (0, 16))
        rows = {r.bucket: r for r in ledger().all()}
        self.assertEqual((rows["monthly"].amount, rows["monthly"].balance_after), (-10, 0))
        self.assertEqual((rows["account"].amount, rows["account"].balance_after), (-4, 16))

    def test_the_same_key_twice_changes_nothing_the_second_time(self):
        self.apply({"account": 500})
        again = self.apply({"account": 500})
        self.assertTrue(again.replayed)
        self.assertEqual((again.account, again.monthly), (520, 10))
        self.assertEqual(balances(self.user), (10, 520))
        self.assertEqual(ledger().count(), 1)

    def test_a_replay_with_different_amounts_is_still_ignored(self):
        self.apply({"account": 500})
        self.assertTrue(self.apply({"account": 9999}).replayed)
        self.assertEqual(balances(self.user), (10, 520))

    def test_different_keys_both_apply(self):
        self.apply({"account": 5}, key="a")
        self.apply({"account": 5}, key="b")
        self.assertEqual(balances(self.user), (10, 30))

    def test_a_key_already_used_for_another_user_is_refused(self):
        other = make_user("bob", monthly=0, account=0)
        self.apply({"account": 5}, key="shared")
        with self.assertRaises(ValueError):
            credits.apply(other.pk, {"account": 5}, "shared", "purchase")
        self.assertEqual(balances(other), (0, 0))

    def test_insufficient_credits_raises_and_writes_nothing(self):
        with self.assertRaises(credits.InsufficientCredits):
            self.apply({"monthly": -5, "account": -21}, kind="backtest")
        self.assertEqual(balances(self.user), (10, 20))
        self.assertFalse(ledger().exists())

    def test_a_failed_attempt_does_not_use_up_the_key(self):
        with self.assertRaises(credits.InsufficientCredits):
            self.apply({"account": -999}, key="retry", kind="backtest")
        self.apply({"account": -5}, key="retry", kind="backtest")
        self.assertEqual(balances(self.user), (10, 15))

    def test_balance_after_chains_across_changes(self):
        self.apply({"account": 100}, key="a")
        self.apply({"account": -30}, key="b", kind="backtest")
        self.apply({"account": 5}, key="c")
        self.assertEqual(list(ledger().order_by("id").values_list("balance_after", flat=True)),
                         [120, 90, 95])

    def test_zero_changes_write_nothing(self):
        result = self.apply({"account": 0})
        self.assertFalse(result.replayed)
        self.assertFalse(ledger().exists())

    def test_unknown_bucket_and_empty_key_are_rejected(self):
        with self.assertRaises(ValueError):
            self.apply({"gold": 5})
        with self.assertRaises(ValueError):
            self.apply({"account": 5}, key="")
        self.assertEqual(balances(self.user), (10, 20))

    def test_a_race_on_the_same_key_counts_as_a_replay(self):
        """A rival request committed the same key after our first lookup: the unique constraint stops our insert."""
        CreditTransaction.objects.create(user=self.user, bucket="account", amount=5, balance_after=25,
                                         kind="purchase", idempotency_key="k1")
        real_filter = CreditTransaction.objects.filter
        lookups = []

        def first_lookup_misses(*args, **kwargs):
            lookups.append(1)
            return CreditTransaction.objects.none() if len(lookups) == 1 else real_filter(*args, **kwargs)

        with mock.patch.object(CreditTransaction.objects, "filter", side_effect=first_lookup_misses):
            result = self.apply({"account": 5})
        self.assertTrue(result.replayed)
        self.assertEqual(ledger().count(), 1)

    def test_the_database_refuses_a_duplicate_event_row(self):
        CreditTransaction.objects.create(user=self.user, bucket="account", amount=1, balance_after=1,
                                         kind="purchase", idempotency_key="dup")
        with self.assertRaises(IntegrityError):
            CreditTransaction.objects.create(user=self.user, bucket="account", amount=1, balance_after=2,
                                             kind="purchase", idempotency_key="dup")


class SplitSpendTests(TestCase):
    def test_monthly_credits_are_spent_first(self):
        self.assertEqual(credits.split_spend(10, 20, 4), {"monthly": -4, "account": 0})

    def test_the_rest_comes_from_account_credits(self):
        self.assertEqual(credits.split_spend(1, 10, 3), {"monthly": -1, "account": -2})
        self.assertEqual(credits.split_spend(0, 10, 3), {"monthly": 0, "account": -3})

    def test_exactly_enough_is_allowed_and_one_short_is_not(self):
        self.assertEqual(credits.split_spend(1, 1, 2), {"monthly": -1, "account": -1})
        with self.assertRaises(credits.InsufficientCredits):
            credits.split_spend(1, 1, 3)

    def test_negative_amounts_are_rejected(self):
        with self.assertRaises(ValueError):
            credits.split_spend(5, 5, -1)

    def test_the_result_feeds_apply(self):
        user = make_user(monthly=1, account=10)
        credits.apply(user.pk, credits.split_spend(1, 10, 3), "bt", "backtest")
        self.assertEqual(balances(user), (0, 8))


class OpeningBalanceMigrationTests(TestCase):
    def run_backfill(self):
        CreditTransaction.objects.all().delete()  # pre-ledger users had no rows; new users get them on creation
        migration = importlib.import_module("api.migrations.0020_credittransaction")
        migration.write_opening_balances(apps, None)

    def test_every_nonzero_balance_gets_one_opening_row(self):
        user = make_user("carol", monthly=40, account=7)
        make_user("dave", monthly=0, account=0)
        self.run_backfill()
        rows = CreditTransaction.objects.filter(user=user).order_by("bucket")
        self.assertEqual([(r.bucket, r.amount, r.balance_after, r.kind) for r in rows],
                         [("account", 7, 7, "adjustment"), ("monthly", 40, 40, "adjustment")])
        self.assertFalse(CreditTransaction.objects.filter(user__username="dave").exists())

    def test_the_ledger_then_sums_to_the_cached_totals(self):
        user = make_user("erin", monthly=12, account=3)
        self.run_backfill()
        credits.apply(user.pk, {"account": 100, "monthly": -2}, "later", "purchase")
        user.refresh_from_db()
        for bucket, cached in (("account", user.account_credits), ("monthly", user.monthly_credits)):
            total = sum(CreditTransaction.objects.filter(user=user, bucket=bucket).values_list("amount", flat=True))
            self.assertEqual(total, cached, bucket)


class AdminTests(TestCase):
    def test_ledger_admin_is_read_only(self):
        model_admin = admin.site._registry[CreditTransaction]
        request = RequestFactory().get("/")
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))


class SpendTests(TestCase):
    def test_spend_uses_monthly_credits_first_inside_the_lock(self):
        user = make_user(monthly=2, account=10)
        result = credits.spend(user.pk, 5, "s1", "backtest")
        self.assertEqual((result.monthly, result.account), (0, 7))
        self.assertEqual(sorted(ledger().values_list("bucket", "amount")),
                         [("account", -3), ("monthly", -2)])

    def test_spend_that_cannot_be_covered_writes_nothing(self):
        user = make_user(monthly=1, account=1)
        with self.assertRaises(credits.InsufficientCredits):
            credits.spend(user.pk, 3, "s1", "backtest")
        self.assertEqual(balances(user), (1, 1))
        self.assertFalse(ledger().exists())

    def test_spend_twice_with_one_key_charges_once(self):
        user = make_user(monthly=10, account=0)
        credits.spend(user.pk, 3, "s1", "backtest")
        self.assertTrue(credits.spend(user.pk, 3, "s1", "backtest").replayed)
        self.assertEqual(balances(user), (7, 0))


class RefundTests(TestCase):
    def test_a_refund_puts_each_amount_back_in_its_own_bucket(self):
        user = make_user(monthly=1, account=10)
        credits.spend(user.pk, 4, "bt:reserve", "backtest")
        self.assertEqual(balances(user), (0, 7))
        result = credits.refund(user.pk, "bt:reserve", "bt:reserve:refund", "backtest")
        self.assertEqual((result.monthly, result.account), (1, 10))
        refunds = CreditTransaction.objects.filter(idempotency_key="bt:reserve:refund")
        self.assertEqual(sorted(refunds.values_list("bucket", "amount")), [("account", 3), ("monthly", 1)])
        self.assertEqual(refunds.first().meta, {"refund_of": "bt:reserve"})

    def test_refunding_twice_gives_back_once(self):
        user = make_user(monthly=10, account=0)
        credits.spend(user.pk, 4, "bt:reserve", "backtest")
        credits.refund(user.pk, "bt:reserve", "bt:reserve:refund", "backtest")
        self.assertTrue(credits.refund(user.pk, "bt:reserve", "bt:reserve:refund", "backtest").replayed)
        self.assertEqual(balances(user), (10, 0))

    def test_refunding_something_that_never_happened_does_nothing(self):
        user = make_user(monthly=10, account=0)
        self.assertIsNone(credits.refund(user.pk, "missing", "missing:refund", "backtest"))
        self.assertEqual(balances(user), (10, 0))
        self.assertFalse(ledger().exists())

    def test_the_ledger_still_sums_to_the_balance_after_a_spend_and_a_refund(self):
        user = make_user(monthly=0, account=0)
        credits.apply(user.pk, {"account": 20, "monthly": 5}, "buy", "purchase")
        credits.spend(user.pk, 8, "bt", "backtest")
        credits.refund(user.pk, "bt", "bt:refund", "backtest")
        user.refresh_from_db()
        for bucket, cached in (("account", user.account_credits), ("monthly", user.monthly_credits)):
            total = sum(CreditTransaction.objects.filter(user=user, bucket=bucket).values_list("amount", flat=True))
            self.assertEqual(total, cached, bucket)
