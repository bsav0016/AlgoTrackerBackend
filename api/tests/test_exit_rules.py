"""Stop-loss, take-profit and trailing-stop exits: the engine, the backtest, the live job, and the API."""
import math
from unittest import mock

import pandas as pd
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.test import APITestCase

from api.exit_rules import (BUY, SELL, STOP_LOSS, TAKE_PROFIT, TRAILING_STOP, PositionState, advance, clean_rules,
                            describe_rules, exit_reason)
from api.indicators.signal import Signal
from api.models import Strategy, Trade
from api.redis_manager import RedisManager
from api.services.backtest_service import BacktestService
from api.services.signal_service import SignalService
from api.tests.test_api_trading import BUY_SMA, SELL_SMA, backtest_payload, make_user, twelve_data
from api.tests.test_live_pipeline import DAY, INTERVAL, SYMBOL, LivePipelineCase
from api.tests.test_strategy import make_strategy

HELD = PositionState(owned=True, entry_price=100.0, peak_price=100.0)


class CleanRulesTests(SimpleTestCase):
    def test_off_values_are_dropped(self):
        self.assertEqual(clean_rules({STOP_LOSS: 5, TAKE_PROFIT: None, TRAILING_STOP: 0}), {STOP_LOSS: 5.0})
        self.assertEqual(clean_rules(None), {})
        self.assertEqual(clean_rules({}), {})

    def test_bad_rules_are_refused(self):
        for bad in ({"stop": 5}, {STOP_LOSS: -1}, {STOP_LOSS: 100}, {STOP_LOSS: "5"}, {STOP_LOSS: True},
                    {TAKE_PROFIT: 1001}, [5]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_rules(bad)

    def test_rules_read_as_text(self):
        self.assertEqual(describe_rules({STOP_LOSS: 5, TAKE_PROFIT: 12.5}), ["Stop-loss 5%", "Take-profit 12.5%"])
        self.assertEqual(describe_rules({}), [])


class ExitReasonTests(SimpleTestCase):
    def test_stop_loss_fires_at_the_boundary_and_not_before(self):
        rules = {STOP_LOSS: 5}
        self.assertIsNone(exit_reason(rules, HELD, 95.01))
        self.assertEqual(exit_reason(rules, HELD, 95.0), STOP_LOSS)
        self.assertEqual(exit_reason(rules, HELD, 80.0), STOP_LOSS)

    def test_take_profit_fires_at_the_boundary_and_not_before(self):
        rules = {TAKE_PROFIT: 10}
        self.assertIsNone(exit_reason(rules, HELD, 109.99))
        self.assertEqual(exit_reason(rules, HELD, 110.0), TAKE_PROFIT)

    def test_trailing_stop_follows_the_peak_not_the_entry(self):
        rules = {TRAILING_STOP: 5}
        state = PositionState(owned=True, entry_price=100.0, peak_price=120.0)
        self.assertIsNone(exit_reason(rules, state, 114.1))
        self.assertEqual(exit_reason(rules, state, 114.0), TRAILING_STOP)
        self.assertIsNone(exit_reason(rules, HELD, 96))  # still near entry, so the peak is the entry

    def test_the_stop_wins_when_several_fire_on_one_candle(self):
        rules = {STOP_LOSS: 5, TRAILING_STOP: 5, TAKE_PROFIT: 1}
        state = PositionState(owned=True, entry_price=100.0, peak_price=100.0)
        self.assertEqual(exit_reason(rules, state, 90), STOP_LOSS)

    def test_nothing_happens_without_rules_or_a_position(self):
        self.assertIsNone(exit_reason({}, HELD, 1))
        self.assertIsNone(exit_reason(None, HELD, 1))
        self.assertIsNone(exit_reason({STOP_LOSS: 5}, PositionState(), 1))


class AdvanceTests(SimpleTestCase):
    def test_without_rules_it_is_the_old_buy_and_sell_logic(self):
        state, action = advance(PositionState(), True, False, 100.0)
        self.assertEqual((state.owned, state.entry_price, action), (True, 100.0, BUY))
        state, action = advance(state, False, False, 90.0)
        self.assertEqual((state.owned, action), (True, None))
        state, action = advance(state, False, True, 80.0)
        self.assertEqual((state.owned, action), (False, SELL))

    def test_a_candle_that_is_both_buy_and_sell_does_nothing(self):
        self.assertEqual(advance(PositionState(), True, True, 100.0), (PositionState(), None))
        self.assertEqual(advance(HELD, True, True, 100.0)[1], None)

    def test_the_peak_rises_with_the_price_and_never_falls(self):
        state, _ = advance(HELD, False, False, 108.0)
        state, _ = advance(state, False, False, 103.0)
        self.assertEqual(state.peak_price, 108.0)

    def test_a_rule_exit_waits_for_the_buy_signals_to_clear_before_buying_again(self):
        rules = {STOP_LOSS: 5}
        state, action = advance(HELD, True, False, 94.0, rules)
        self.assertEqual(action, STOP_LOSS)
        self.assertTrue(state.blocked and not state.owned)
        state, action = advance(state, True, False, 94.0, rules)  # buy signals still hold: stay out
        self.assertEqual((action, state.owned, state.blocked), (None, False, True))
        state, action = advance(state, False, False, 95.0, rules)  # they clear
        self.assertEqual((action, state.owned, state.blocked), (None, False, False))
        state, action = advance(state, True, False, 96.0, rules)
        self.assertEqual((action, state.owned, state.entry_price), (BUY, True, 96.0))

    def test_a_rule_exit_with_no_buy_signal_does_not_block(self):
        state, action = advance(HELD, False, False, 94.0, {STOP_LOSS: 5})
        self.assertEqual((action, state.blocked), (STOP_LOSS, False))

    def test_a_rule_wins_over_a_sell_signal_on_the_same_candle(self):
        self.assertEqual(advance(HELD, False, True, 94.0, {STOP_LOSS: 5})[1], STOP_LOSS)
        self.assertEqual(advance(HELD, False, True, 99.0, {STOP_LOSS: 5})[1], SELL)

    def test_a_position_from_before_entry_prices_were_kept_starts_from_the_first_candle_seen(self):
        state, action = advance(PositionState(owned=True), False, False, 50.0, {STOP_LOSS: 5})
        self.assertEqual((state.entry_price, state.peak_price, action), (50.0, 50.0, None))


def frame(closes, buys, sells=None):
    sells = sells or [False] * len(closes)
    return pd.DataFrame({"datetime": pd.date_range("2024-01-01", periods=len(closes)), "close": closes,
                         "buy": buys, "sell": sells})


class BacktestExitTests(SimpleTestCase):
    def test_no_rules_gives_the_same_positions_and_returns_as_before(self):
        closes = [100, 102, 105, 103, 99, 101, 104, 100]
        buys = [False, True, True, False, False, True, True, False]
        sells = [False, False, False, True, True, False, False, True]
        plain = BacktestService.calculate_returns(frame(closes, buys, sells))
        ruled = BacktestService.calculate_returns(frame(closes, buys, sells), exit_rules={})
        self.assertEqual(list(plain["owned"]), [0, 1, 1, 0, 0, 1, 1, 0])
        pd.testing.assert_frame_equal(plain, ruled)
        self.assertTrue((plain["exit_reason"] == "").all())

    def test_a_stop_loss_sells_on_the_candle_it_hits_and_says_why(self):
        closes = [100, 100, 98, 94, 90, 91]
        result = BacktestService.calculate_returns(frame(closes, [False, True, True, True, True, True]),
                                                   exit_rules={STOP_LOSS: 5})
        self.assertEqual(list(result["owned"]), [0, 1, 1, 0, 0, 0])  # buy signals never cleared, so it stays out
        self.assertEqual(list(result["exit_reason"]), ["", "", "", STOP_LOSS, "", ""])
        self.assertEqual(int(result["trades"].sum()), 2)
        self.assertAlmostEqual(result["return"].iloc[-1] / result["return"].iloc[0], 0.94)

    def test_a_take_profit_locks_in_the_gain(self):
        closes = [100, 100, 106, 111, 120, 90]
        result = BacktestService.calculate_returns(frame(closes, [False, True, True, True, True, True]),
                                                   exit_rules={TAKE_PROFIT: 10})
        self.assertEqual(list(result["exit_reason"]), ["", "", "", TAKE_PROFIT, "", ""])
        self.assertAlmostEqual(result["return"].iloc[-1] / result["return"].iloc[0], 1.11)

    def test_a_trailing_stop_gives_back_only_its_percentage_from_the_peak(self):
        closes = [100, 100, 110, 120, 113, 100]
        result = BacktestService.calculate_returns(frame(closes, [False, True, True, True, True, True]),
                                                   exit_rules={TRAILING_STOP: 5})
        self.assertEqual(list(result["exit_reason"]), ["", "", "", "", TRAILING_STOP, ""])
        self.assertAlmostEqual(result["return"].iloc[-1] / result["return"].iloc[0], 1.13)

    def test_it_buys_again_once_the_buy_signals_clear_and_return(self):
        closes = [100, 100, 94, 94, 96, 97]
        buys = [False, True, True, False, True, True]
        result = BacktestService.calculate_returns(frame(closes, buys), exit_rules={STOP_LOSS: 5})
        self.assertEqual(list(result["owned"]), [0, 1, 0, 0, 1, 1])
        self.assertEqual(int(result["trades"].sum()), 3)

    def test_the_exit_applies_to_the_signalled_state_when_trades_are_delayed_a_candle(self):
        closes = [100, 100, 94, 90, 90]
        result = BacktestService.calculate_returns(frame(closes, [False, True, True, True, True]), delay=True,
                                                   exit_rules={STOP_LOSS: 5})
        self.assertEqual(list(result["owned"]), [0, 1, 0, 0, 0])
        self.assertEqual(list(result["held"]), [0, 0, 1, 0, 0])


class BacktestEndpointExitTests(APITestCase):
    def setUp(self):
        self.client.force_authenticate(make_user())
        patcher = mock.patch("api.services.backtest_service.requests.get", return_value=twelve_data())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_rules_the_positions_are_unchanged(self):
        data = self.client.post("/api/backtest/", backtest_payload(), format="json").data
        self.assertEqual([row["owned"] for row in data["result"]], [0, 1, 1, 1, 0, 0, 0, 1, 1])
        self.assertEqual({row["exit_reason"] for row in data["result"]}, {""})

    def test_a_take_profit_ends_the_trade_early_and_names_the_rule(self):
        data = self.client.post("/api/backtest/", backtest_payload(exit_rules={"take_profit": 10}), format="json").data
        rows = data["result"]
        self.assertEqual(rows[3]["exit_reason"], "take_profit")  # 11 -> 13 is +18%
        self.assertEqual(rows[3]["owned"], 0)
        self.assertTrue(all(math.isfinite(data[key]) for key in ("percent_return", "sharpe_ratio")))

    def test_bad_rules_are_a_400(self):
        for bad in ({"stop_loss": 100}, {"take_profit": -5}, {"trailing_stop": "x"}, {"stop_loss": 0.001}):
            with self.subTest(bad=bad):
                response = self.client.post("/api/backtest/", backtest_payload(exit_rules=bad), format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("exit_rules", response.data)


class StrategyEndpointExitTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)

    def subscribe(self, **extra):
        return self.client.post("/api/strategy/", {"title": "Risky", "symbol": "AAPL", "interval": "1day",
                                                   "signals": [BUY_SMA, SELL_SMA], **extra}, format="json")

    def test_rules_are_stored_and_shown_and_off_values_are_dropped(self):
        response = self.subscribe(exit_rules={"stop_loss": 5, "take_profit": None, "trailing_stop": 0})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Strategy.objects.get().exit_rules, {"stop_loss": 5.0})
        shown = self.client.get("/api/user-details/").data["strategies"][0]
        self.assertEqual(shown["exit_rules"], {"stop_loss": 5.0})

    def test_a_strategy_with_no_rules_has_an_empty_object(self):
        self.subscribe()
        self.assertEqual(Strategy.objects.get().exit_rules, {})

    def test_bad_rules_create_nothing(self):
        self.assertEqual(self.subscribe(exit_rules={"stop_loss": 150}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Strategy.objects.count(), 0)

    def test_a_clone_carries_the_rules(self):
        strategy = make_strategy(self.user, exit_rules={"trailing_stop": 4.0})
        draft = strategy.clone_for(make_user("bob"))
        self.assertEqual(draft.exit_rules, {"trailing_stop": 4.0})
        self.assertIsNone(draft.entry_price)


class LiveExitTests(LivePipelineCase):
    """The live job and the backtest must agree, with and without rules."""
    RULES = {"stop_loss": 3.0, "take_profit": 4.0, "trailing_stop": 2.0}
    MESSAGE_WORDS = {"stop_loss": "stop-loss", "take_profit": "take-profit", "trailing_stop": "trailing stop"}

    def run_both(self, rules):
        closes = [round(100 + 6 * math.sin(i / 3) + 2 * math.sin(i * 1.7) + i * 0.05, 2) for i in range(90)]
        prices = pd.DataFrame({"timestamp": [i * DAY for i in range(90)], "close": closes,
                               "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes]})
        buy = Signal(name="SMA", above_target=False, buy_signal=True, target_value=100.0, window=3)
        sell = Signal(name="SMA", above_target=True, buy_signal=False, target_value=103.0, window=3)
        history = 35
        data = SignalService.calculate_indicators(prices, [buy, sell])
        data = BacktestService.find_sells(BacktestService.find_buys(data, [buy, sell]), [buy, sell])
        expected = BacktestService.calculate_returns(data.iloc[history - 1:], exit_rules=rules)

        for _, row in prices.iloc[:history].iterrows():
            async_to_sync(RedisManager.store_close_price)(
                SYMBOL, INTERVAL, row["close"], int(row["timestamp"]), high=row["high"], low=row["low"])
        strategy = Strategy.objects.create(
            user=self.user, title="Risk", symbol=SYMBOL, interval=INTERVAL, position=1, exit_rules=rules,
            buy_signals=[{**vars(buy)}], sell_signals=[{**vars(sell)}])
        for offset, (_, row) in enumerate(prices.iloc[history:].iterrows(), start=1):
            async_to_sync(RedisManager.store_close_price)(
                SYMBOL, INTERVAL, row["close"], int(row["timestamp"]), high=row["high"], low=row["low"])
            while self.queued:
                self.run_job(*self.queued.pop(0))
            strategy.refresh_from_db()
            self.assertEqual(strategy.position >= 2, bool(expected.at[offset, "owned"]), f"candle {offset}")
        return strategy, expected

    def test_with_rules_the_live_job_matches_the_backtest_candle_by_candle(self):
        strategy, expected = self.run_both(self.RULES)
        reasons = [r for r in expected["exit_reason"] if r]
        self.assertTrue(reasons, "the series should trip a rule")
        self.assertEqual(sorted(Trade.objects.exclude(exit_reason="").values_list("exit_reason", flat=True)),
                         sorted(reasons))
        self.assertAlmostEqual(
            float(strategy.strategy_return), expected["return"].iloc[-1] / expected["return"].iloc[0], places=3)

    def test_without_rules_nothing_changes(self):
        strategy, expected = self.run_both({})
        self.assertFalse(Trade.objects.exclude(exit_reason="").exists())
        self.assertAlmostEqual(
            float(strategy.strategy_return), expected["return"].iloc[-1] / expected["return"].iloc[0], places=3)

    def test_a_rule_sell_is_recorded_and_the_user_is_told_which_rule_fired(self):
        self.run_both(self.RULES)
        trade = Trade.objects.exclude(exit_reason="").first()
        self.assertFalse(trade.is_buy)
        self.assertIn(self.MESSAGE_WORDS[trade.exit_reason], trade.notification.message)
