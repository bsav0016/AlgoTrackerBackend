import base64
import datetime
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase, TestCase

from api.indicators.signal import Signal
from api.models import CustomUser
from api.services.backtest_service import BacktestService
from api.tests.helpers import FakeResponse, twelve_data_values

UTC = datetime.timezone.utc


def signal(name, buy, above, target, **kwargs):
    return Signal(name=name, above_target=above, buy_signal=buy, target_value=target, **kwargs)


class FindBuysAndSellsTests(SimpleTestCase):
    def setUp(self):
        self.data = pd.DataFrame({
            "RSI_14": [20.0, 40.0, 60.0, 80.0],
            "SMA_5": [0.9, 1.1, 1.1, 0.9],
        })

    def test_buy_requires_every_buy_signal_to_hold(self):
        signals = [signal("RSI", True, False, 50, window=14), signal("SMA", True, True, 1.0, window=5)]
        out = BacktestService.find_buys(self.data, signals)
        self.assertEqual(list(out["buy"]), [False, True, False, False])

    def test_sell_requires_every_sell_signal_to_hold_and_ignores_buy_signals(self):
        signals = [signal("RSI", True, False, 50, window=14),  # buy signal, must not affect sells
                   signal("RSI", False, True, 50, window=14),
                   signal("SMA", False, False, 1.0, window=5)]
        out = BacktestService.find_sells(self.data, signals)
        self.assertEqual(list(out["sell"]), [False, False, False, True])

    def test_without_buy_signals_every_row_is_a_buy(self):
        self.assertTrue(BacktestService.find_buys(self.data, [])["buy"].all())

    def test_without_sell_signals_no_row_is_a_sell(self):
        buy_only = [signal("RSI", True, True, 50, window=14)]
        self.assertFalse(BacktestService.find_sells(self.data, buy_only)["sell"].any())
        self.assertFalse(BacktestService.find_sells(self.data, [])["sell"].any())

    def test_a_strategy_with_no_sell_signal_buys_and_holds(self):
        buy_only = [signal("RSI", True, True, 50, window=14)]
        data = self.data.assign(close=[10.0, 11.0, 12.0, 13.0])
        data = BacktestService.find_sells(BacktestService.find_buys(data, buy_only), buy_only)
        out = BacktestService.calculate_returns(data)
        self.assertEqual(list(out["owned"]), [0, 0, 1, 1])
        self.assertAlmostEqual(out["return"].iloc[-1], 10.0 * (13.0 / 12.0), places=6)  # held through the last candle

    def test_target_is_a_strict_comparison(self):
        out = BacktestService.find_buys(self.data, [signal("RSI", True, True, 60, window=14)])
        self.assertEqual(list(out["buy"]), [False, False, False, True])

    def test_macd_signals_use_the_three_window_column_for_buys_and_sells(self):
        data = pd.DataFrame({"MACD_12_26_9": [0.5, 1.5]})
        kwargs = dict(fast_window=12, slow_window=26, signal_window=9)
        buys = BacktestService.find_buys(data, [signal("MACD", True, True, 1.0, **kwargs)])
        sells = BacktestService.find_sells(data, [signal("MACD", False, False, 1.0, **kwargs)])
        self.assertEqual(list(buys["buy"]), [False, True])
        self.assertEqual(list(sells["sell"]), [True, False])

    def test_input_frame_is_not_mutated(self):
        BacktestService.find_buys(self.data, [signal("RSI", True, True, 1, window=14)])
        self.assertNotIn("buy", self.data)


class CalculateReturnsTests(SimpleTestCase):
    @staticmethod
    def frame(close, buy, sell):
        return pd.DataFrame({"close": close, "buy": buy, "sell": sell})

    def test_position_is_entered_on_buy_and_exited_on_sell(self):
        out = BacktestService.calculate_returns(self.frame(
            [100.0, 110, 121, 110, 100],
            [False, True, True, False, False],
            [False, False, False, True, True]))
        self.assertEqual(list(out["owned"]), [0, 1, 1, 0, 0])
        # The return series starts at the first close and only moves on bars that followed a
        # held bar: bought on bar 1 (110), so bar 2 gains 10% and bar 3 loses 9.09%.
        self.assertEqual([round(v, 6) for v in out["return"]], [100, 100, 110, 100, 100])

    def test_conflicting_buy_and_sell_leaves_the_position_unchanged(self):
        out = BacktestService.calculate_returns(self.frame(
            [100.0, 100, 100, 100], [True] * 4, [True] * 4))
        self.assertEqual(list(out["owned"]), [0, 0, 0, 0])

    def test_never_buying_keeps_the_return_flat(self):
        out = BacktestService.calculate_returns(self.frame([50.0, 60, 70], [False] * 3, [False] * 3))
        self.assertEqual(list(out["return"]), [50, 50, 50])

    def test_holding_through_the_end_tracks_the_asset(self):
        out = BacktestService.calculate_returns(self.frame([100.0, 100, 150, 120], [True] * 4, [False] * 4))
        self.assertEqual(list(out["owned"]), [0, 1, 1, 1])
        self.assertAlmostEqual(out["return"].iloc[-1], 120)

    def test_input_frame_is_not_mutated(self):
        data = self.frame([1.0, 2.0], [True, True], [False, False])
        BacktestService.calculate_returns(data)
        self.assertNotIn("owned", data)


def results(strategy, close):
    return pd.DataFrame({"return": strategy, "close": close})


class MetricsTests(SimpleTestCase):
    def test_percent_return_is_relative_to_first_close_and_adjusted_for_asset(self):
        pct, pct_adj = BacktestService.calculate_percent_return(results([100, 105, 110], [100, 90, 120]))
        self.assertAlmostEqual(pct, 10)
        self.assertAlmostEqual(pct_adj, 10 - 20)

    def test_sharpe_ratio_matches_hand_calculation(self):
        # step returns 10%, 10%, 0%: mean 0.0667, sample std 0.0577
        sharpe, sharpe_adj = BacktestService.calculate_sharpe_ratio(
            results([100, 110, 121, 121], [100, 100, 100, 100]))
        self.assertAlmostEqual(sharpe, 1.1547005, places=6)
        self.assertAlmostEqual(sharpe_adj, 1.1547005, places=6)  # flat asset -> same excess returns

    def test_sortino_ratio_matches_hand_calculation(self):
        sortino, sortino_adj = BacktestService.calculate_sortino_ratio(
            results([100, 95, 85, 90], [100, 100, 100, 100]))
        self.assertAlmostEqual(sortino, -0.8226471, places=6)
        self.assertAlmostEqual(sortino_adj, -0.8226471, places=6)

    def test_ratios_are_a_pair_even_when_the_strategy_never_moves(self):
        # The view unpacks both values, so a strategy that never trades must not return a bare NaN.
        flat = results([100, 100, 100, 100], [100, 101, 102, 103])
        for calculate in (BacktestService.calculate_sharpe_ratio, BacktestService.calculate_sortino_ratio):
            with self.subTest(metric=calculate.__name__):
                first, _ = calculate(flat)
                self.assertTrue(pd.isna(first))  # no variance / no downside -> undefined

    def test_metrics_reject_empty_or_incomplete_results(self):
        empty = pd.DataFrame(columns=["return", "close"])
        for calculate in (BacktestService.calculate_sharpe_ratio, BacktestService.calculate_sortino_ratio,
                          BacktestService.calculate_percent_return):
            with self.subTest(metric=calculate.__name__), self.assertRaises(ValueError):
                calculate(empty)
        with self.assertRaises(ValueError):
            BacktestService.calculate_percent_return(pd.DataFrame({"close": [1, 2]}))


class ChartTests(SimpleTestCase):
    def test_chart_returns_two_base64_pngs(self):
        data = pd.DataFrame({
            "datetime": pd.date_range("2024-01-01", periods=4, tz="UTC"),
            "close": [100.0, 110, 105, 120], "return": [100.0, 100, 95, 109],
            "buy": [False, True, False, False], "sell": [False, False, True, False],
            "owned": [0, 1, 0, 0],
        })
        for image in BacktestService.generate_backtest_chart(data):
            self.assertTrue(base64.b64decode(image).startswith(b"\x89PNG"))

    def test_chart_rejects_empty_results(self):
        with self.assertRaises(ValueError):
            BacktestService.generate_backtest_chart(pd.DataFrame())


class ChargeUserTests(TestCase):
    @staticmethod
    def make_user(monthly, account):
        return CustomUser.objects.create_user(
            username="u", email="u@example.com", password="x", first_name="U", last_name="U",
            monthly_credits=monthly, account_credits=account)

    def test_monthly_credits_are_spent_first(self):
        user = self.make_user(500, 10)
        self.assertEqual(BacktestService.charge_user(user, 3), (True, 10, 497))
        user.refresh_from_db()
        self.assertEqual((user.monthly_credits, user.account_credits), (497, 10))

    def test_shortfall_is_taken_from_account_credits(self):
        user = self.make_user(2, 10)
        self.assertEqual(BacktestService.charge_user(user, 5), (True, 7, 0))

    def test_exactly_the_remaining_monthly_credits_can_be_spent(self):
        user = self.make_user(3, 0)
        self.assertEqual(BacktestService.charge_user(user, 3), (True, 0, 0))

    def test_insufficient_credits_charges_nothing(self):
        user = self.make_user(2, 1)
        self.assertEqual(BacktestService.charge_user(user, 5), (False, None, None))
        user.refresh_from_db()
        self.assertEqual((user.monthly_credits, user.account_credits), (2, 1))


class GetDataTests(SimpleTestCase):
    START = datetime.datetime(2024, 1, 3, tzinfo=UTC)
    END = datetime.datetime(2024, 1, 10, tzinfo=UTC)

    def get(self, responses):
        with mock.patch("api.services.backtest_service.requests.get", side_effect=responses) as get:
            return BacktestService.get_data("AAPL", "1day", self.START, self.END), get

    def test_returns_chronological_data_from_a_single_page(self):
        page = twelve_data_values("2024-01-01", [10, 11, 12, 13])  # API sends newest first
        (df, attempts), get = self.get([FakeResponse(200, {"values": page})])
        self.assertEqual(attempts, 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(list(df["close"]), [10, 11, 12, 13])
        self.assertTrue(df["datetime"].is_monotonic_increasing)
        self.assertEqual(str(df["datetime"].dt.tz), "UTC")

    def test_request_uses_configured_key_symbol_interval_and_end_date(self):
        _, get = self.get([FakeResponse(200, {"values": twelve_data_values("2024-01-01", [1, 2, 3, 4])})])
        self.assertEqual(get.call_args[0][0], "https://api.twelvedata.com/time_series")
        params = get.call_args[1]["params"]
        self.assertEqual(params["symbol"], "AAPL")
        self.assertEqual(params["interval"], "1day")
        self.assertEqual(params["end_date"], "2024-01-10")
        self.assertEqual(params["apikey"], "test-twelve-data-key")

    def test_pages_backwards_until_the_start_date_is_covered(self):
        newer = twelve_data_values("2024-01-05", [15, 16, 17])  # does not reach the 01-03 start
        older = twelve_data_values("2024-01-01", [11, 12, 13, 14])
        (df, attempts), get = self.get([
            FakeResponse(200, {"values": newer}), FakeResponse(200, {"values": older})])
        self.assertEqual(attempts, 2)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args[1]["params"]["end_date"], "2024-01-05")
        self.assertEqual(list(df["close"]), [11, 12, 13, 14, 15, 16, 17])

    def test_paging_stops_after_the_configured_iteration_limit(self):
        def never_reaches_start(*args, **kwargs):
            return FakeResponse(200, {"values": twelve_data_values("2024-01-05", [1, 2, 3])})

        with mock.patch("api.services.backtest_service.BACKTEST_ITERATIONS", 2), \
                mock.patch("api.services.backtest_service.requests.get",
                           side_effect=never_reaches_start) as get:
            BacktestService.get_data("AAPL", "1day", self.START, self.END)
        self.assertEqual(get.call_count, 2)  # never an unbounded number of paid API calls

    def test_http_error_raises(self):
        with self.assertRaisesRegex(Exception, "status 500"):
            self.get([FakeResponse(500, text="boom")])

    def test_error_payload_without_values_raises(self):
        body = {"code": 400, "message": "bad symbol", "status": "error"}
        with self.assertRaisesRegex(ValueError, "Values not found"):
            self.get([FakeResponse(200, body)])

    def test_empty_values_list_returns_an_empty_frame(self):
        (df, attempts), _ = self.get([FakeResponse(200, {"values": []})])
        self.assertTrue(df.empty)
        self.assertEqual(attempts, 1)


class ConductBacktestTests(SimpleTestCase):
    # Closes rise then fall then rise. "Below its 2-bar average ratio" (price above average) buys;
    # "above" sells.
    CLOSES = [10, 11, 12, 13, 12, 11, 10, 11, 12]
    SIGNALS = [signal("SMA", True, False, 1.0, window=2), signal("SMA", False, True, 1.0, window=2)]

    def run_backtest(self, start):
        page = twelve_data_values("2024-01-01", self.CLOSES)
        with mock.patch("api.services.backtest_service.requests.get",
                        return_value=FakeResponse(200, {"values": page})):
            return BacktestService.conduct_backtest(
                "AAPL", "1day", start, datetime.datetime(2024, 2, 1, tzinfo=UTC), self.SIGNALS)

    def test_end_to_end_enters_the_uptrend_and_exits_the_downtrend(self):
        result, attempts = self.run_backtest(datetime.datetime(2024, 1, 1))  # naive -> treated as UTC
        self.assertEqual(attempts, 1)
        self.assertEqual(list(result["owned"]), [0, 1, 1, 1, 0, 0, 0, 1, 1])
        # 10 -> held 11->12->13 -> sold at 13 -> flat -> re-bought at 11 (bar 7), ends at 12
        self.assertAlmostEqual(result["return"].iloc[3], 10 * 12 / 11 * 13 / 12)
        self.assertAlmostEqual(result["return"].iloc[-1], 10 * 12 / 11 * 12 / 11)

    def test_data_before_the_start_date_only_warms_up_indicators(self):
        result, _ = self.run_backtest(datetime.datetime(2024, 1, 4))
        self.assertEqual(len(result), 6)
        self.assertEqual(result["datetime"].iloc[0].day, 4)
        self.assertFalse(result["SMA_2"].isna().any())  # computed from the earlier bars
