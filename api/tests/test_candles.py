import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from api.candles import bucket_start
from api.redis_manager import RedisManager
from api.tests.helpers import FakeRedis

SYMBOL, INTERVAL, MINUTE = "AAPL", "1min", 60


class BucketStartTests(SimpleTestCase):
    def test_rounds_down_to_the_bucket_open_time(self):
        self.assertEqual(bucket_start(125, 60), 120)

    def test_a_boundary_timestamp_opens_its_own_bucket(self):
        self.assertEqual(bucket_start(120, 60), 120)
        self.assertEqual(bucket_start(119, 60), 60)


class RecordTickTests(TestCase):
    """Ticks (a last price each) are folded into candles; a candle is stored once its bucket has ended."""

    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch("api.redis_manager.redis_client", self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, price, timestamp):
        async_to_sync(RedisManager.record_tick)(SYMBOL, INTERVAL, price, timestamp, MINUTE)

    def closed(self):
        return RedisManager.get_recent_closes(SYMBOL, INTERVAL)

    def open_candle(self):
        return json.loads(self.redis.get(f"open_candle:{SYMBOL}:{INTERVAL}"))

    def test_ticks_in_one_bucket_build_a_candle_that_is_not_stored_yet(self):
        for price, ts in [(10.0, 1), (10.8, 20), (9.8, 40), (10.5, 59)]:
            self.tick(price, ts)

        self.assertEqual(self.open_candle(), {"timestamp": 0, "open": 10.0, "high": 10.8, "low": 9.8, "close": 10.5})
        self.assertEqual(self.closed(), [])

    def test_a_tick_on_the_boundary_closes_the_previous_candle_with_its_high_and_low(self):
        for price, ts in [(10.0, 1), (10.8, 20), (9.8, 40), (10.5, 59), (10.6, 60)]:
            self.tick(price, ts)

        self.assertEqual(self.closed(), [{"timestamp": 0, "close": 10.5, "high": 10.8, "low": 9.8}])
        self.assertEqual(self.open_candle(), {"timestamp": 60, "open": 10.6, "high": 10.6, "low": 10.6, "close": 10.6})

    def test_empty_buckets_produce_no_candle(self):
        self.tick(10.0, 5)
        self.tick(11.0, 5 * MINUTE + 5)  # buckets 1-4 had no ticks

        self.assertEqual([c["timestamp"] for c in self.closed()], [0])
        self.assertEqual(self.open_candle()["timestamp"], 5 * MINUTE)

    def test_a_repeated_tick_changes_nothing_and_stores_nothing(self):
        self.tick(10.0, 5)
        before = self.open_candle()
        self.tick(10.0, 5)

        self.assertEqual(self.open_candle(), before)
        self.assertEqual(self.closed(), [])

    def test_each_candle_is_stored_exactly_once(self):
        for ts in [5, 65, 70, 125, 130, 135]:
            self.tick(10.0, ts)

        self.assertEqual([c["timestamp"] for c in self.closed()], [0, 60])

    def test_a_late_tick_for_an_earlier_bucket_is_ignored(self):
        self.tick(10.0, 65)
        self.tick(99.0, 5)

        self.assertEqual(self.open_candle()["timestamp"], 60)
        self.assertEqual(self.open_candle()["high"], 10.0)
        self.assertEqual(self.closed(), [])
