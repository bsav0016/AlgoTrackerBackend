"""The opening ledger rows, the `check_credits` reconciliation command and the credit-history endpoint."""
import io

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from api import credits
from api.models import CreditTransaction, CustomUser


def make_user(username="alice", monthly=500, account=0):
    return CustomUser.objects.create_user(
        username=username, email=f"{username}@example.com", password="x", first_name="A", last_name="B",
        monthly_credits=monthly, account_credits=account)


def run_check(*args):
    out, err = io.StringIO(), io.StringIO()
    try:
        call_command("check_credits", *args, stdout=out, stderr=err)
    except CommandError as exc:
        return out.getvalue(), err.getvalue(), exc
    return out.getvalue(), err.getvalue(), None


class OpeningBalanceTests(TestCase):
    def test_a_new_user_starts_with_a_ledger_row_per_nonzero_balance(self):
        user = make_user(monthly=500, account=0)
        rows = CreditTransaction.objects.filter(user=user)
        self.assertEqual([(r.bucket, r.amount, r.balance_after, r.kind) for r in rows],
                         [("monthly", 500, 500, "adjustment")])

    def test_both_buckets_get_a_row_when_both_are_nonzero(self):
        user = make_user(monthly=10, account=20)
        self.assertEqual(sorted(CreditTransaction.objects.filter(user=user).values_list("bucket", "amount")),
                         [("account", 20), ("monthly", 10)])

    def test_a_user_who_starts_at_zero_has_no_rows(self):
        user = make_user(monthly=0, account=0)
        self.assertFalse(CreditTransaction.objects.filter(user=user).exists())

    def test_saving_an_existing_user_again_adds_nothing(self):
        user = make_user()
        user.first_name = "Changed"
        user.save()
        self.assertEqual(CreditTransaction.objects.filter(user=user).count(), 1)


class CheckCreditsTests(TestCase):
    def test_a_user_who_only_uses_the_ledger_reconciles(self):
        user = make_user(monthly=5, account=0)
        credits.apply(user.pk, {"account": 100}, "buy", "purchase")
        credits.spend(user.pk, 20, "bt", "backtest")
        credits.refund(user.pk, "bt", "bt:refund", "backtest")
        credits.spend(user.pk, 7, "bt2", "backtest")
        out, err, error = run_check()
        self.assertIsNone(error)
        self.assertEqual(err, "")
        self.assertIn("OK: 1 user(s) checked", out)

    def test_a_monthly_reset_keeps_the_ledger_in_step(self):
        from datetime import timedelta
        from django.utils import timezone
        user = make_user(monthly=40)
        CustomUser.objects.filter(pk=user.pk).update(reset_monthly_credits=timezone.now() - timedelta(days=1))
        self.assertTrue(credits.reset_monthly(user.pk, 500))
        self.assertIsNone(run_check()[2])

    def test_users_with_no_rows_and_no_credits_are_fine(self):
        make_user(monthly=0, account=0)
        self.assertIsNone(run_check()[2])

    def test_a_cached_balance_changed_behind_the_ledgers_back_is_reported(self):
        user = make_user()
        CustomUser.objects.filter(pk=user.pk).update(account_credits=999)  # what the old code paths could do
        _, err, error = run_check()
        self.assertIsNotNone(error)
        self.assertIn("alice account: balance is 999 but the ledger ends at 0", err)
        self.assertIn("1 credit mismatch(es) across 1 user(s)", str(error))

    def test_a_balance_with_no_ledger_rows_is_reported(self):
        user = make_user(monthly=40)
        CreditTransaction.objects.filter(user=user).delete()
        _, err, error = run_check()
        self.assertIn("alice monthly: balance is 40 but the ledger ends at 0", err)
        self.assertIsNotNone(error)

    def test_a_broken_chain_in_the_ledger_is_reported(self):
        user = make_user(monthly=100)
        credits.spend(user.pk, 10, "bt", "backtest")
        row = CreditTransaction.objects.get(idempotency_key="bt")
        CreditTransaction.objects.filter(pk=row.pk).update(amount=-30)  # history edited after the fact
        _, err, error = run_check()
        self.assertIsNotNone(error)
        self.assertIn(f"ledger row {row.pk}", err)

    def test_every_problem_is_listed_and_other_users_are_still_checked(self):
        bad = make_user("bad")
        make_user("good")
        CustomUser.objects.filter(pk=bad.pk).update(monthly_credits=1, account_credits=2)
        _, err, error = run_check()
        self.assertEqual(len(err.strip().splitlines()), 2)
        self.assertIn("2 credit mismatch(es) across 2 user(s)", str(error))
        self.assertNotIn("good", err)

    def test_one_user_can_be_checked_alone(self):
        bad = make_user("bad")
        make_user("good")
        CustomUser.objects.filter(pk=bad.pk).update(account_credits=2)
        self.assertIsNone(run_check("--user", "good")[2])
        self.assertIsNotNone(run_check("--user", "bad")[2])

    def test_an_unknown_user_is_an_error(self):
        _, _, error = run_check("--user", "nobody")
        self.assertIn("No user named", str(error))


class CreditHistoryTests(APITestCase):
    url = "/api/credits/history/"

    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def history(self, query=""):
        return self.client.get(self.url + query)

    def test_login_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.history().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rows_come_newest_first_with_a_readable_label(self):
        credits.apply(self.user.pk, {"account": 1000}, "revenuecat:tx1", "purchase", meta={"customer_id": "secret"})
        credits.spend(self.user.pk, 3, "bt", "backtest", meta={"symbol": "AAPL", "interval": "1day"})
        results = self.history().data["results"]
        self.assertEqual([r["label"] for r in results], ["Backtest AAPL 1day", "Credit purchase", "Opening balance"])
        self.assertEqual([r["amount"] for r in results], [-3, 1000, 500])
        self.assertEqual(results[0]["balance_after"], 497)
        self.assertEqual(set(results[0]), {"id", "created_at", "bucket", "amount", "balance_after", "kind", "label"})

    def test_internal_details_are_not_exposed(self):
        credits.apply(self.user.pk, {"account": 1000}, "revenuecat:tx1", "purchase", meta={"customer_id": "secret"})
        body = self.history().content.decode()
        self.assertNotIn("secret", body)
        self.assertNotIn("revenuecat:tx1", body)

    def test_a_refund_is_labelled_as_one(self):
        credits.spend(self.user.pk, 3, "bt:reserve", "backtest", meta={"symbol": "AAPL", "interval": "1day"})
        credits.refund(self.user.pk, "bt:reserve", "bt:reserve:refund", "backtest")
        self.assertEqual(self.history().data["results"][0]["label"], "Backtest (refund)")

    def test_only_the_signed_in_users_rows_are_returned(self):
        other = make_user("bob")
        credits.apply(other.pk, {"account": 77}, "bobs", "purchase")
        credits.apply(self.user.pk, {"account": 5}, "mine", "purchase")
        amounts = [r["amount"] for r in self.history().data["results"]]
        self.assertEqual(amounts, [5, 500])

    def test_pages_chain_with_next_before_until_the_end(self):
        for index in range(5):
            credits.apply(self.user.pk, {"account": index + 1}, f"k{index}", "purchase")  # plus the opening row
        first = self.history("?limit=4").data
        self.assertEqual([r["amount"] for r in first["results"]], [5, 4, 3, 2])
        self.assertEqual(first["next_before"], first["results"][-1]["id"])
        second = self.history(f"?limit=4&before={first['next_before']}").data
        self.assertEqual([r["amount"] for r in second["results"]], [1, 500])
        self.assertIsNone(second["next_before"])

    def test_an_exact_fit_has_no_next_page(self):
        data = self.history("?limit=1").data  # one row exists (the opening balance)
        self.assertEqual(len(data["results"]), 1)
        self.assertIsNone(data["next_before"])

    def test_limit_is_clamped(self):
        self.assertEqual(len(self.history("?limit=0").data["results"]), 1)
        self.assertEqual(self.history("?limit=100000").status_code, status.HTTP_200_OK)

    def test_non_numeric_parameters_are_a_400(self):
        self.assertEqual(self.history("?limit=abc").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.history("?before=x").status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_user_with_no_history_gets_an_empty_page(self):
        empty = make_user("empty", monthly=0, account=0)
        self.client.force_authenticate(empty)
        self.assertEqual(self.history().data, {"results": [], "next_before": None})
