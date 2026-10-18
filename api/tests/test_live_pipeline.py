from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase

from api.models import Trade
from api.redis_manager import RedisManager
from api.services.notification_service import NotificationService
from api.tasks import process_strategy_task
from api.tests.helpers import FakeRedis
from api.tests.test_strategy import make_strategy, make_user

SYMBOL, INTERVAL = "AAPL", "1day"
DAY = 86400




# Realistic prices have cents. (pd.read_json turns a window of all-whole-number closes into int64,
# which Django's DecimalField rejects when the Trade is saved.)
class LivePipelineTests(TestCase):
    """store_close_price -> get_recent_closes -> process_strategy_task -> check_signals, with Redis
    replaced by an in-memory fake and the RQ queue run inline. Push delivery is mocked."""

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
