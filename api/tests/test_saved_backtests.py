"""Saved backtests: kept automatically, listed, reopened, renamed, deleted, capped, and private to their owner."""
from unittest import mock

from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.test import APITestCase

from api import saved_backtests
from api.models import CustomUser, SavedBacktest
from api.services.backtest_service import NoDataForPeriod
from api.tests.test_api_trading import BUY_SMA, CLOSES, SELL_SMA, backtest_payload, make_user, twelve_data


def row(i, close=100.0, owned=0, value=100.0, exit_reason=""):
    return {"i": i, "datetime": f"2024-01-{i % 28 + 1:02d}T00:00:00Z", "close": close, "owned": owned,
            "return": value, "exit_reason": exit_reason}


class ThinRowsTests(SimpleTestCase):
    def test_a_short_series_is_unchanged(self):
        rows = [row(i) for i in range(10)]
        self.assertEqual(saved_backtests.thin_rows(rows, limit=20), rows)

    def test_a_long_series_is_thinned_but_keeps_its_ends_and_order(self):
        rows = [row(i, close=100 + i % 7) for i in range(5000)]
        thin = saved_backtests.thin_rows(rows, limit=200)
        self.assertLessEqual(len(thin), 260)
        self.assertGreater(len(thin), 50)
        self.assertIs(thin[0], rows[0])
        self.assertIs(thin[-1], rows[-1])
        positions = [r["i"] for r in thin]
        self.assertEqual(positions, sorted(set(positions)))  # in order, none twice

    def test_every_change_of_position_and_every_rule_exit_survives(self):
        rows = [row(i, owned=1 if 1000 <= i < 1500 or 3000 <= i < 3100 else 0) for i in range(5000)]
        rows[3099]["exit_reason"] = "stop_loss"
        thin = saved_backtests.thin_rows(rows, limit=200)
        kept = {id(r) for r in thin}
        for i in (999, 1000, 1499, 1500, 2999, 3000, 3098, 3099, 3100):
            self.assertIn(id(rows[i]), kept, i)
        # read back the way the app does, comparing neighbours: the same two changes
        changes = [(a["owned"], b["owned"]) for a, b in zip(thin, thin[1:]) if a["owned"] != b["owned"]]
        self.assertEqual(changes, [(0, 1), (1, 0), (0, 1), (1, 0)])

    def test_a_strategy_that_flips_every_candle_is_still_bounded(self):
        rows = [row(i, owned=i % 2) for i in range(20000)]
        thin = saved_backtests.thin_rows(rows)
        self.assertLessEqual(len(thin), saved_backtests.MAX_ROWS + 10)

    def test_numbers_are_stored_to_eight_significant_digits_so_a_tiny_price_keeps_its_precision(self):
        stored = saved_backtests._row({"datetime": "2024-01-01T00:00:00Z", "close": 0.000012345678912, "owned": 1,
                                       "return": 123456.78912345, "exit_reason": None})
        self.assertEqual(stored["close"], 0.000012345679)
        self.assertEqual(stored["return"], 123456.79)
        self.assertEqual(stored["exit_reason"], "")

    def test_spikes_and_dips_are_not_averaged_away(self):
        rows = [row(i) for i in range(5000)]
        rows[2222]["close"] = 900.0
        rows[3333]["return"] = -50.0
        thin = saved_backtests.thin_rows(rows, limit=200)
        self.assertEqual(max(r["close"] for r in thin), 900.0)
        self.assertEqual(min(r["return"] for r in thin), -50.0)


class SavedBacktestTestCase(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)
        patcher = mock.patch("api.services.backtest_service.requests.get", return_value=twelve_data())
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_backtest(self, **overrides):
        response = self.client.post("/api/backtest/", backtest_payload(**overrides), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data


class SavingTests(SavedBacktestTestCase):
    def test_a_backtest_is_kept_and_its_response_carries_the_id(self):
        data = self.run_backtest(exit_rules={"take_profit": 10})
        saved = SavedBacktest.objects.get()
        self.assertEqual(data["id"], saved.pk)
        self.assertEqual((saved.user, saved.symbol, saved.interval), (self.user, "AAPL", "1day"))
        self.assertEqual([s["name"] for s in saved.buy_signals], ["SMA"])
        self.assertTrue(all(s["buy_signal"] for s in saved.buy_signals))
        self.assertFalse(any(s["buy_signal"] for s in saved.sell_signals))
        self.assertEqual(saved.exit_rules, {"take_profit": 10.0})
        self.assertFalse(saved.next_candle_execution)
        self.assertEqual(saved.summary["trades"], data["trades"])
        self.assertAlmostEqual(saved.summary["period_return"], data["period_return"])

    def test_the_saved_series_reads_back_as_the_same_trades(self):
        data = self.run_backtest(exit_rules={"take_profit": 10})
        saved = SavedBacktest.objects.get()
        self.assertEqual(len(saved.series), len(CLOSES))  # short, so nothing is thinned
        self.assertEqual([r["owned"] for r in saved.series], [r["owned"] for r in data["result"]])
        self.assertEqual([r["exit_reason"] for r in saved.series], [r["exit_reason"] for r in data["result"]])
        self.assertEqual(set(saved.series[0]), set(saved_backtests.ROW_KEYS))

    def test_a_run_that_fails_or_is_refused_is_not_kept(self):
        CustomUser.objects.filter(pk=self.user.pk).update(monthly_credits=0, account_credits=0)
        self.assertEqual(self.client.post("/api/backtest/", backtest_payload(), format="json").status_code,
                         status.HTTP_402_PAYMENT_REQUIRED)
        with mock.patch("api.views.BacktestService.conduct_backtest", side_effect=NoDataForPeriod("none")):
            CustomUser.objects.filter(pk=self.user.pk).update(monthly_credits=500)
            self.client.post("/api/backtest/", backtest_payload(), format="json")
        self.assertEqual(SavedBacktest.objects.count(), 0)

    def test_a_failure_to_save_does_not_fail_the_backtest_the_user_paid_for(self):
        with mock.patch("api.views.saved_backtests.save", side_effect=RuntimeError("disk full")):
            data = self.run_backtest()
        self.assertIsNone(data["id"])
        self.assertTrue(data["result"])
        self.assertEqual(SavedBacktest.objects.count(), 0)

    def test_the_oldest_are_dropped_beyond_the_cap(self):
        with mock.patch.object(saved_backtests, "MAX_SAVED", 3):
            ids = [self.run_backtest()["id"] for _ in range(5)]
        self.assertEqual(sorted(SavedBacktest.objects.values_list("id", flat=True)), ids[2:])

    def test_the_cap_is_per_user(self):
        other = make_user("bob")
        with mock.patch.object(saved_backtests, "MAX_SAVED", 2):
            for _ in range(3):
                self.run_backtest()
            self.client.force_authenticate(other)
            self.run_backtest()
        self.assertEqual(SavedBacktest.objects.filter(user=self.user).count(), 2)
        self.assertEqual(SavedBacktest.objects.filter(user=other).count(), 1)

    def test_deleting_the_account_deletes_them(self):
        self.run_backtest()
        self.user.delete()
        self.assertEqual(SavedBacktest.objects.count(), 0)


class ListTests(SavedBacktestTestCase):
    def test_newest_first_without_the_series_and_only_the_owners(self):
        first, second = self.run_backtest()["id"], self.run_backtest(exit_rules={"stop_loss": 5})["id"]
        other = make_user("bob")
        self.client.force_authenticate(other)
        self.run_backtest()
        self.client.force_authenticate(self.user)
        data = self.client.get("/api/backtests/").data
        self.assertEqual([r["id"] for r in data["results"]], [second, first])
        self.assertIsNone(data["next_before"])
        item = data["results"][0]
        self.assertEqual(set(item), {"id", "created_at", "label", "symbol", "interval", "covered_start", "covered_end",
                                     "period_return", "trades", "has_exit_rules"})
        self.assertTrue(item["has_exit_rules"])
        self.assertFalse(data["results"][1]["has_exit_rules"])

    def test_it_pages_like_the_other_histories(self):
        ids = [self.run_backtest()["id"] for _ in range(5)]
        page = self.client.get("/api/backtests/?limit=2").data
        self.assertEqual([r["id"] for r in page["results"]], ids[::-1][:2])
        self.assertEqual(page["next_before"], ids[::-1][1])
        page2 = self.client.get(f"/api/backtests/?limit=2&before={page['next_before']}").data
        self.assertEqual([r["id"] for r in page2["results"]], ids[::-1][2:4])
        last = self.client.get(f"/api/backtests/?limit=2&before={page2['next_before']}").data
        self.assertEqual([r["id"] for r in last["results"]], [ids[0]])
        self.assertIsNone(last["next_before"])

    def test_bad_paging_values_are_refused_and_sign_in_is_required(self):
        self.assertEqual(self.client.get("/api/backtests/?limit=x").status_code, status.HTTP_400_BAD_REQUEST)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/backtests/").status_code, status.HTTP_401_UNAUTHORIZED)


class DetailTests(SavedBacktestTestCase):
    def test_a_saved_run_comes_back_in_the_shape_of_a_backtest_response_without_images_or_balances(self):
        data = self.run_backtest(exit_rules={"take_profit": 10})
        saved = self.client.get(f"/api/backtests/{data['id']}/").data
        for key in saved_backtests.SUMMARY_KEYS:
            self.assertEqual(saved[key], data[key], key)
        self.assertEqual(len(saved["result"]), len(data["result"]))
        self.assertEqual(saved["result"][3]["exit_reason"], "take_profit")
        self.assertEqual(saved["exit_rules"], {"take_profit": 10.0})
        self.assertEqual([s["name"] for s in saved["buy_signals"]], ["SMA"])
        self.assertEqual(saved["symbol"], "AAPL")
        for absent in ("image", "image_no_signals", "user_account_credits", "user_monthly_credits"):
            self.assertNotIn(absent, saved)

    def test_opening_a_saved_run_is_free_and_fetches_no_prices(self):
        data = self.run_backtest()
        self.user.refresh_from_db()
        before = (self.user.monthly_credits, self.user.account_credits)
        with mock.patch("api.services.backtest_service.requests.get") as get:
            self.assertEqual(self.client.get(f"/api/backtests/{data['id']}/").status_code, status.HTTP_200_OK)
        get.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual((self.user.monthly_credits, self.user.account_credits), before)

    def test_someone_elses_run_is_a_404_for_every_method(self):
        data = self.run_backtest()
        self.client.force_authenticate(make_user("bob"))
        url = f"/api/backtests/{data['id']}/"
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.patch(url, {"label": "mine now"}, format="json").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get("/api/backtests/999999/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(SavedBacktest.objects.get().label, "")

    def test_sign_in_is_required(self):
        data = self.run_backtest()
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/backtests/{data['id']}/").status_code, status.HTTP_401_UNAUTHORIZED)


class RenameAndDeleteTests(SavedBacktestTestCase):
    def test_a_label_can_be_set_trimmed_and_cleared(self):
        url = f"/api/backtests/{self.run_backtest()['id']}/"
        response = self.client.patch(url, {"label": "  RSI with a stop  "}, format="json")
        self.assertEqual((response.status_code, response.data["label"]), (status.HTTP_200_OK, "RSI with a stop"))
        self.assertEqual(self.client.get(url).data["label"], "RSI with a stop")
        self.assertEqual(self.client.patch(url, {"label": ""}, format="json").data["label"], "")

    def test_a_bad_label_is_refused(self):
        url = f"/api/backtests/{self.run_backtest()['id']}/"
        for bad in ({"label": "x" * 61}, {"label": 5}, {}):
            self.assertEqual(self.client.patch(url, bad, format="json").status_code, status.HTTP_400_BAD_REQUEST, bad)
        self.assertEqual(SavedBacktest.objects.get().label, "")

    def test_deleting_removes_it_and_a_repeat_is_a_404(self):
        url = f"/api/backtests/{self.run_backtest()['id']}/"
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(SavedBacktest.objects.count(), 0)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
