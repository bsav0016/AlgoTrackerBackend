from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase

from api.models import Strategy, Trade
from api.redis_manager import RedisManager
from api.services.notification_service import NotificationService
from api.tasks import process_strategy_task
from api.tests.helpers import FakeRedis
from api.tests.test_strategy import make_strategy, make_user

SYMBOL, INTERVAL = "AAPL", "1day"
DAY = 86400




class LivePipelineCase(TestCase):
    """store_close_price -> get_recent_closes -> process_strategy_task -> check_signals, with Redis
    replaced by an in-memory fake and the RQ queue run by the test. Push delivery is mocked."""

    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch("api.redis_manager.redis_client", self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Collect enqueued jobs instead of sending them to RQ; store() runs them like a worker would.
        self.queued = []
        patcher = mock.patch.object(process_strategy_task, "delay", side_effect=lambda *args: self.queued.append(args))
        patcher.start()
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(NotificationService, "send_push_message")
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

        self.user = make_user(device_ids=["ExponentPushToken[a]"])

    def store(self, closes, first_timestamp=0):
        """Feeds closes through store_close_price in the order the websocket worker would (oldest first),
        then runs any jobs it enqueued."""
        for i, close in enumerate(closes):
            async_to_sync(RedisManager.store_close_price)(SYMBOL, INTERVAL, close, first_timestamp + i * DAY)
        while self.queued:
            process_strategy_task(*self.queued.pop(0))


class LivePipelineTests(LivePipelineCase):
    def test_recent_closes_come_back_oldest_first(self):
        self.store([10.0, 11.0, 12.0])

        closes = RedisManager.get_recent_closes(SYMBOL, INTERVAL)

        self.assertEqual([c["close"] for c in closes], [10.0, 11.0, 12.0])
        self.assertEqual([c["timestamp"] for c in closes], [0, DAY, 2 * DAY])

    def test_recent_closes_keep_the_newest_candles_when_trimmed(self):
        async_to_sync(RedisManager.store_close_price)(SYMBOL, INTERVAL, 1.0, 0, max_candles=2)
        async_to_sync(RedisManager.store_close_price)(SYMBOL, INTERVAL, 2.0, DAY, max_candles=2)
        async_to_sync(RedisManager.store_close_price)(SYMBOL, INTERVAL, 3.0, 2 * DAY, max_candles=2)

        closes = RedisManager.get_recent_closes(SYMBOL, INTERVAL)

        self.assertEqual([c["close"] for c in closes], [2.0, 3.0])

    def test_rising_prices_trigger_a_buy_through_the_whole_pipeline(self):
        self.store([10.5, 11.5])  # history before anyone is subscribed: no jobs run
        strategy = make_strategy(self.user, position=1)

        self.store([12.5], first_timestamp=2 * DAY)  # the candle that makes the strategy signal

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)
        trade = Trade.objects.get()
        self.assertTrue(trade.is_buy)
        self.assertEqual(float(trade.price), 12.5)
        self.push.assert_called_once_with("ExponentPushToken[a]", "Momentum triggered a buy signal!")

    def test_falling_prices_trigger_a_sell_through_the_whole_pipeline(self):
        self.store([12.5, 11.5])
        strategy = make_strategy(self.user, position=2)

        self.store([10.5], first_timestamp=2 * DAY)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 0)
        trade = Trade.objects.get()
        self.assertFalse(trade.is_buy)
        self.assertEqual(float(trade.price), 10.5)
        self.push.assert_called_once_with("ExponentPushToken[a]", "Momentum triggered a sell signal!")

    def test_asset_return_is_the_change_between_the_last_two_candles(self):
        self.store([10.5, 11.5])
        strategy = make_strategy(self.user, position=1)

        self.store([12.5], first_timestamp=2 * DAY)

        strategy.refresh_from_db()
        self.assertAlmostEqual(float(strategy.asset_return), 12.5 / 11.5, places=4)


class IdempotentJobTests(LivePipelineCase):
    """The same candle may be delivered twice, or two candles' jobs may run in the wrong order."""

    def history_then_subscribe(self, closes, position):
        self.store(closes)  # nobody subscribed yet, so no jobs
        return make_strategy(self.user, position=position)

    def queue_candle(self, close, day):
        async_to_sync(RedisManager.store_close_price)(SYMBOL, INTERVAL, close, day * DAY)
        return self.queued.pop(0)

    def test_running_the_same_job_twice_trades_and_notifies_once(self):
        strategy = self.history_then_subscribe([10.5, 11.5], position=1)
        job = self.queue_candle(12.5, 2)

        process_strategy_task(*job)
        process_strategy_task(*job)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)
        self.assertEqual(Trade.objects.count(), 1)
        self.push.assert_called_once()
        self.assertEqual(strategy.last_processed_ts, 2 * DAY)
        self.assertAlmostEqual(float(strategy.asset_return), 12.5 / 11.5, places=4)

    def test_an_older_candles_job_after_a_newer_one_is_skipped(self):
        strategy = self.history_then_subscribe([10.5, 11.5], position=1)
        older = self.queue_candle(12.5, 2)
        newer = self.queue_candle(13.5, 3)

        process_strategy_task(*newer)
        process_strategy_task(*older)

        strategy.refresh_from_db()
        self.assertEqual(strategy.last_processed_ts, 3 * DAY)
        self.assertEqual(Trade.objects.count(), 1)
        self.assertEqual(float(Trade.objects.get().price), 13.5)
        self.push.assert_called_once()

    def test_a_job_only_sees_candles_up_to_its_own_timestamp(self):
        strategy = self.history_then_subscribe([10.5, 11.5], position=1)
        older = self.queue_candle(12.5, 2)
        self.queue_candle(9.5, 3)  # arrives before the older job runs; must not leak into it

        process_strategy_task(*older)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)
        self.assertEqual(float(Trade.objects.get().price), 12.5)

    def test_jobs_in_order_each_process_their_candle(self):
        strategy = self.history_then_subscribe([10.5, 11.5], position=1)
        first = self.queue_candle(12.5, 2)
        second = self.queue_candle(13.5, 3)

        process_strategy_task(*first)
        process_strategy_task(*second)

        strategy.refresh_from_db()
        self.assertEqual(strategy.last_processed_ts, 3 * DAY)
        self.assertAlmostEqual(float(strategy.asset_return), (12.5 / 11.5) * (13.5 / 12.5), places=4)

    def test_whole_number_prices_can_be_saved_on_a_trade(self):
        # The job used to rebuild prices with pd.read_json, which turned an all-whole-number window into
        # int64 and made Trade.save() fail on its DecimalField.
        strategy = self.history_then_subscribe([10.0, 11.0], position=1)
        job = self.queue_candle(12.0, 2)

        process_strategy_task(*job)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)
        self.assertEqual(float(Trade.objects.get().price), 12.0)

    def test_a_job_for_a_deleted_strategy_does_nothing(self):
        strategy = self.history_then_subscribe([10.5, 11.5], position=1)
        job = self.queue_candle(12.5, 2)
        strategy.delete()

        process_strategy_task(*job)

        self.assertEqual(Strategy.objects.count(), 0)
        self.push.assert_not_called()

    def test_the_first_candle_alone_is_skipped_without_error(self):
        strategy = make_strategy(self.user, position=1)
        job = self.queue_candle(10.5, 0)

        process_strategy_task(*job)

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 1)
        self.assertIsNone(strategy.last_processed_ts)


class LiveHighLowIndicatorTests(LivePipelineCase):
    """SO and ADX need each candle's high and low. Candles built from ticks carry them to the job."""

    def history(self, candles):
        """Stores finished candles (timestamp index, high, low, close) before anyone is subscribed."""
        for i, (high, low, close) in enumerate(candles):
            async_to_sync(RedisManager.store_close_price)(SYMBOL, INTERVAL, close, i * DAY, high=high, low=low)

    def tick_day(self, day, prices):
        for j, price in enumerate(prices):
            async_to_sync(RedisManager.record_tick)(SYMBOL, INTERVAL, price, day * DAY + j * 60, DAY)
        while self.queued:
            process_strategy_task(*self.queued.pop(0))

    def subscribe(self, name, buy_above, sell_below):
        """A strategy that buys when the indicator is above `buy_above` and sells when it is below `sell_below`."""
        signal = {"above_target": True, "window": 2}
        return Strategy.objects.create(
            user=self.user, title="Momentum", symbol=SYMBOL, interval=INTERVAL, position=1,
            buy_signals=[{**signal, "name": name, "target_value": buy_above}],
            sell_signals=[{**signal, "name": name, "above_target": False, "target_value": sell_below}])

    def test_a_stochastic_oscillator_strategy_signals_from_live_ticks(self):
        self.history([(10.0, 9.0, 9.8)])
        strategy = self.subscribe("SO", buy_above=80, sell_below=20)

        self.tick_day(1, [9.9, 10.8, 9.8, 10.5])  # close 10.5 in a 9.0-10.8 range
        self.tick_day(2, [10.6])  # first tick of the next day closes day 1

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)
        trade = Trade.objects.get()
        self.assertTrue(trade.is_buy)
        self.assertEqual(float(trade.price), 10.5)

    def test_an_adx_strategy_signals_from_live_ticks(self):
        self.history([(10.0, 9.0, 9.5), (11.0, 10.0, 10.5), (12.0, 11.0, 11.5)])
        strategy = self.subscribe("ADX", buy_above=50, sell_below=10)

        self.tick_day(3, [12.0, 13.0, 11.9, 12.5])
        self.tick_day(4, [12.6])

        strategy.refresh_from_db()
        self.assertEqual(strategy.position, 3)
        self.assertTrue(Trade.objects.get().is_buy)
