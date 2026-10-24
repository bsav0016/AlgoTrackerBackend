"""Backtest, strategy and config endpoints. TwelveData is always mocked."""
import math
from unittest import mock

from rest_framework import status
from rest_framework.test import APITestCase

from api.models import CreditTransaction, CustomUser, Strategy
from api.services.backtest_service import BacktestService
from api.tests.helpers import FakeResponse, ledger, twelve_data_values

CLOSES = [10, 11, 12, 13, 12, 11, 10, 11, 12]
BUY_SMA = {"name": "SMA", "above_target": False, "buy_signal": True, "target_value": 1.0, "window": 2}
SELL_SMA = {"name": "SMA", "above_target": True, "buy_signal": False, "target_value": 1.0, "window": 2}


def make_user(username="alice", **kwargs):
    return CustomUser.objects.create_user(
        username=username, email=f"{username}@example.com", password="x", first_name="A", last_name="B",
        **kwargs)


def backtest_payload(**overrides):
    return {"symbol": "AAPL", "interval": "1day", "start_date": "2024-01-01T00:00:00Z",
            "end_date": "2024-02-01T00:00:00Z", "signals": [BUY_SMA, SELL_SMA], **overrides}


def twelve_data(closes=CLOSES):
    return FakeResponse(200, {"values": twelve_data_values("2024-01-01", closes)})


class BacktestEndpointTests(APITestCase):
    url = "/api/backtest/"

    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)
        patcher = mock.patch("api.services.backtest_service.requests.get", return_value=twelve_data())
        self.get = patcher.start()
        self.addCleanup(patcher.stop)

    def set_credits(self, monthly, account):
        CustomUser.objects.filter(pk=self.user.pk).update(monthly_credits=monthly, account_credits=account)
        self.user.refresh_from_db()  # the authenticated request user is this same object

    def credits(self):
        self.user.refresh_from_db()
        return self.user.monthly_credits, self.user.account_credits

    def test_successful_backtest_returns_results_metrics_and_charts_and_charges_credits(self):
        response = self.client.post(self.url, backtest_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(len(data["result"]), len(CLOSES))
        self.assertEqual([row["owned"] for row in data["result"]], [0, 1, 1, 1, 0, 0, 0, 1, 1])
        self.assertIn("SMA_2", data["result"][1]["indicators"])
        self.assertEqual(data["result"][0]["indicators"]["SMA_2"], 0)  # warm-up NaN is sent as 0
        for key in ("percent_return", "percent_return_adj", "sharpe_ratio", "sharpe_ratio_adj",
                    "sortino_ratio", "sortino_ratio_adj"):
            self.assertTrue(math.isfinite(data[key]), key)
        self.assertTrue(data["image"] and data["image_no_signals"])
        self.assertEqual(self.credits(), (500 - 3, 0))  # BACKTEST_BASE_COST x one data request
        self.assertEqual(data["user_monthly_credits"], 497)

    def test_the_cost_is_reserved_in_the_ledger(self):
        self.client.post(self.url, backtest_payload(), format="json")
        row = ledger().get()
        self.assertEqual((row.kind, row.bucket, row.amount), ("backtest", "monthly", -3))
        self.assertTrue(row.idempotency_key.endswith(":reserve"))
        self.assertEqual(row.meta, {"symbol": "AAPL", "interval": "1day"})

    def run_with_attempts(self, attempts):
        real = BacktestService.conduct_backtest

        def conduct(*args, **kwargs):
            result, _ = real(*args, **kwargs)
            return result, attempts

        with mock.patch("api.views.BacktestService.conduct_backtest", side_effect=conduct):
            return self.client.post(self.url, backtest_payload(), format="json")

    def test_extra_data_requests_are_settled_after_the_run(self):
        response = self.run_with_attempts(3)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.credits(), (500 - 9, 0))  # 3 x BACKTEST_BASE_COST
        self.assertEqual(sorted(k.rsplit(":", 1)[1] for k in
                                ledger().values_list("idempotency_key", flat=True)),
                         ["reserve", "settle"])
        self.assertEqual(response.data["user_monthly_credits"], 491)

    def test_when_the_extra_requests_cannot_be_paid_the_reservation_is_given_back(self):
        self.set_credits(4, 0)  # enough for the first request (3), not for two more (6)
        response = self.run_with_attempts(3)
        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(response.data, {"detail": "Requires $9"})
        self.assertEqual(self.credits(), (4, 0))

    def test_a_failed_run_gives_the_reservation_back_in_the_same_buckets(self):
        self.set_credits(1, 10)
        self.get.return_value = FakeResponse(503, text="service unavailable")
        response = self.client.post(self.url, backtest_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(self.credits(), (1, 10))
        suffixes = sorted(k.split(":", 2)[2] for k in
                          ledger().values_list("idempotency_key", flat=True))
        self.assertEqual(suffixes, ["reserve", "reserve", "reserve:refund", "reserve:refund"])

    def test_nothing_is_fetched_from_the_market_data_api_when_the_user_cannot_pay(self):
        self.set_credits(1, 1)
        self.client.post(self.url, backtest_payload(), format="json")
        self.get.assert_not_called()
        self.assertFalse(ledger().exists())

    def test_strategy_that_never_trades_still_returns_a_result(self):
        never = {**BUY_SMA, "above_target": True, "target_value": 100.0}
        response = self.client.post(self.url, backtest_payload(signals=[never, SELL_SMA]), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["percent_return"], 0)
        self.assertEqual(response.data["sharpe_ratio"], 0)

    def test_macd_sell_signal_is_supported(self):
        macd_sell = {"name": "MACD", "above_target": False, "buy_signal": False, "target_value": 1.0,
                     "fast_window": 2, "slow_window": 3, "signal_window": 2}
        response = self.client.post(self.url, backtest_payload(signals=[BUY_SMA, macd_sell]), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_insufficient_credits_returns_402_and_charges_nothing(self):
        self.set_credits(1, 1)
        response = self.client.post(self.url, backtest_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(self.credits(), (1, 1))

    def test_credits_beyond_monthly_allowance_come_from_account_credits(self):
        self.set_credits(1, 10)
        response = self.client.post(self.url, backtest_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.credits(), (0, 8))

    def test_upstream_failure_returns_500_and_does_not_charge(self):
        self.get.return_value = FakeResponse(503, text="service unavailable")
        response = self.client.post(self.url, backtest_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.data, {"detail": "Internal Error"})  # no upstream details leaked
        self.assertEqual(self.credits(), (500, 0))

    def test_upstream_error_payload_returns_500_and_does_not_charge(self):
        self.get.return_value = FakeResponse(200, {"code": 400, "message": "symbol not found"})
        response = self.client.post(self.url, backtest_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(self.credits(), (500, 0))

    def test_no_price_data_returns_500_and_does_not_charge(self):
        self.get.return_value = FakeResponse(200, {"values": []})
        response = self.client.post(self.url, backtest_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(self.credits(), (500, 0))

    def test_unsupported_symbol_or_interval_is_rejected_before_calling_the_market_data_api(self):
        for override in ({"symbol": "TSLA"}, {"interval": "5min"}):
            with self.subTest(override=override):
                response = self.client.post(self.url, backtest_payload(**override), format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.get.assert_not_called()
        self.assertEqual(self.credits(), (500, 0))

    def test_unknown_indicator_does_not_charge(self):
        bogus = {**BUY_SMA, "name": "FOO"}
        response = self.client.post(self.url, backtest_payload(signals=[bogus]), format="json")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.credits(), (500, 0))

    def test_invalid_payloads_are_rejected_with_field_errors(self):
        cases = {
            "missing symbol": {k: v for k, v in backtest_payload().items() if k != "symbol"},
            "bad date": backtest_payload(start_date="yesterday"),
            "signal missing target": backtest_payload(signals=[{"name": "SMA", "above_target": True,
                                                                "buy_signal": True}]),
            "non numeric target": backtest_payload(signals=[{**BUY_SMA, "target_value": "high"}]),
            "missing window": backtest_payload(signals=[{k: v for k, v in BUY_SMA.items() if k != "window"}]),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                response = self.client.post(self.url, payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.get.assert_not_called()


class StrategyEndpointTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def payload(self, **overrides):
        return {"title": "Momentum", "symbol": "AAPL", "interval": "1day",
                "signals": [BUY_SMA, SELL_SMA], **overrides}

    def test_subscribing_stores_buy_and_sell_signals_separately(self):
        response = self.client.post("/api/strategy/", self.payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        strategy = Strategy.objects.get()
        self.assertEqual((strategy.user, strategy.symbol, strategy.interval, strategy.position),
                         (self.user, "AAPL", "1day", 1))
        self.assertEqual([s["name"] for s in strategy.buy_signals], ["SMA"])
        self.assertTrue(all(s["buy_signal"] for s in strategy.buy_signals))
        self.assertFalse(any(s["buy_signal"] for s in strategy.sell_signals))

    def test_new_strategy_appears_in_the_users_details(self):
        self.client.post("/api/strategy/", self.payload(), format="json")
        strategies = self.client.get("/api/user-details/").data["strategies"]
        self.assertEqual([s["title"] for s in strategies], ["Momentum"])

    def test_user_details_show_when_a_strategy_was_last_checked(self):
        Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day", title="Checked",
                                last_processed_ts=1700000000)
        Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day", title="New")
        strategies = self.client.get("/api/user-details/").data["strategies"]
        self.assertEqual({s["title"]: s["last_processed_ts"] for s in strategies},
                         {"Checked": 1700000000, "New": None})

    def test_unsupported_symbol_or_interval_is_rejected(self):
        for override in ({"symbol": "TSLA"}, {"interval": "5min"}):
            with self.subTest(override=override):
                response = self.client.post("/api/strategy/", self.payload(**override), format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Strategy.objects.exists())

    BAD_SIGNALS = {
        "unknown indicator": [{**BUY_SMA, "name": "FOO"}],
        "missing window": [{k: v for k, v in BUY_SMA.items() if k != "window"}],
        "zero window": [{**BUY_SMA, "window": 0}],
        "window longer than the stored history": [{**BUY_SMA, "window": 501}],
        "MACD without a signal window": [{"name": "MACD", "above_target": True, "buy_signal": True,
                                          "target_value": 0.0, "fast_window": 3, "slow_window": 6}],
        "no buy signal": [SELL_SMA],
        "no signals": [],
    }

    def test_signals_that_could_never_work_are_rejected(self):
        for label, signals in self.BAD_SIGNALS.items():
            with self.subTest(label):
                response = self.client.post("/api/strategy/", self.payload(signals=signals), format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Strategy.objects.exists())

    def test_a_strategy_with_only_a_buy_signal_is_accepted(self):
        response = self.client.post("/api/strategy/", self.payload(signals=[BUY_SMA]), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Strategy.objects.get().sell_signals, [])

    def test_macd_signals_are_accepted(self):
        macd = {"name": "MACD", "above_target": True, "buy_signal": True, "target_value": 0.0,
                "fast_window": 3, "slow_window": 6, "signal_window": 3}
        response = self.client.post("/api/strategy/", self.payload(signals=[macd]), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_payload_is_rejected(self):
        response = self.client.post("/api/strategy/", {"symbol": "AAPL"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Strategy.objects.exists())

    def test_owner_can_delete_a_strategy(self):
        strategy = Strategy.objects.create(user=self.user, symbol="AAPL", interval="1day")
        response = self.client.delete(f"/api/strategy/{strategy.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Strategy.objects.exists())

    def test_other_users_strategy_cannot_be_deleted(self):
        other = Strategy.objects.create(user=make_user("bob"), symbol="AAPL", interval="1day")
        response = self.client.delete(f"/api/strategy/{other.pk}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Strategy.objects.filter(pk=other.pk).exists())

    def test_deleting_a_missing_strategy_is_404(self):
        self.assertEqual(self.client.delete("/api/strategy/999/").status_code, status.HTTP_404_NOT_FOUND)


class SymbolsAndIntervalsTests(APITestCase):
    def test_serves_the_configured_symbols_intervals_and_pricing(self):
        self.client.force_authenticate(make_user())
        response = self.client.post("/api/symbols-and-intervals/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["symbols"], ["AAPL", "BTC/USD"])
        self.assertEqual(response.data["intervals"], [
            {"interval": "1min", "monthly_charge": 100}, {"interval": "1day", "monthly_charge": 10}])
        self.assertEqual(response.data["backtest_base_cost"], 3)
        self.assertEqual(response.data["backtest_iterations"], 5)
