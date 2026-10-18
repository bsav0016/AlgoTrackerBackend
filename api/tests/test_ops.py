"""Health endpoint, the seed_candles command, the tick heartbeat and the history loader. TwelveData and Redis are faked."""
import json
import time
from io import StringIO
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient

from api import websocket_client
from api.redis_manager import RedisManager
from api.tasks import process_strategy_task
from api.tests.helpers import FakeRedis, FakeResponse, twelve_data_values
from api.tests.test_strategy import make_strategy, make_user

DAY = 86400


class FakeRedisCase(TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch("api.redis_manager.redis_client", self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)


class HealthTests(FakeRedisCase):
    def get(self):
        return APIClient().get("/api/health/")  # no credentials: the container healthcheck has none

    def test_reports_ok_and_no_ticks_yet(self):
        response = self.get()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"status": "ok", "database": True, "redis": True,
                                         "seconds_since_last_tick": None})

    def test_reports_how_long_ago_the_last_tick_arrived(self):
        RedisManager.mark_tick(int(time.time()) - 42)
        response = self.get()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertAlmostEqual(response.data["seconds_since_last_tick"], 42, delta=3)

    def test_a_quiet_feed_is_not_a_failure(self):
        RedisManager.mark_tick(int(time.time()) - 3 * DAY)  # a closed market over a long weekend
        self.assertEqual(self.get().status_code, status.HTTP_200_OK)

    def test_redis_down_is_503_without_leaking_details(self):
        self.redis.get = mock.Mock(side_effect=ConnectionError("redis://secret-host:6379 refused"))
        response = self.get()
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual((response.data["status"], response.data["redis"], response.data["database"]),
                         ("unavailable", False, True))
        self.assertNotIn("secret-host", json.dumps(response.data))

    def test_database_down_is_503(self):
        with mock.patch("api.views.connection.ensure_connection", side_effect=RuntimeError("db down")):
            response = self.get()
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual((response.data["database"], response.data["redis"]), (False, True))


class TickHeartbeatTests(FakeRedisCase):
    def test_a_price_message_records_the_tick_time(self):
        self.assertIsNone(RedisManager.seconds_since_last_tick(int(time.time())))
        with mock.patch.object(RedisManager, "record_tick", new=mock.AsyncMock()):
            async_to_sync(websocket_client.on_message)(json.dumps({"symbol": "AAPL", "price": "10.5"}))
        self.assertLessEqual(RedisManager.seconds_since_last_tick(int(time.time())), 3)

    def test_messages_that_are_not_prices_do_not_count(self):
        async_to_sync(websocket_client.on_message)(json.dumps({"event": "subscribe-status", "status": "ok"}))
        self.assertIsNone(RedisManager.seconds_since_last_tick(int(time.time())))


class SeedHistoryTests(FakeRedisCase):
    def setUp(self):
        super().setUp()
        for name, value in (("SYMBOLS", {"AAPL": "Apple"}), ("INTERVAL_MAP", {"1day": {"seconds": DAY}})):
            patcher = mock.patch.object(__import__("api.redis_manager", fromlist=[name]), name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("api.redis_manager.requests.get")
        self.get = patcher.start()
        self.addCleanup(patcher.stop)
        self.get.return_value = FakeResponse(200, {"values": twelve_data_values("2024-01-01", [10.0, 11.0, 12.0])})
        patcher = mock.patch.object(process_strategy_task, "delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, **kwargs):
        async_to_sync(RedisManager.populate_redis_all)(**kwargs)

    def test_history_is_stored_oldest_first_with_highs_and_lows(self):
        self.seed()
        closes = RedisManager.get_recent_closes("AAPL", "1day")
        self.assertEqual([c["close"] for c in closes], [10.0, 11.0, 12.0])
        self.assertEqual([(c["high"], c["low"]) for c in closes], [(11.0, 9.0), (12.0, 10.0), (13.0, 11.0)])

    def test_loading_history_does_not_run_strategies(self):
        make_strategy(make_user(), position=1)
        self.seed()
        self.delay.assert_not_called()

    def test_a_pair_that_already_has_candles_is_left_alone(self):
        self.seed()
        self.get.reset_mock()
        self.seed()
        self.get.assert_not_called()
        self.assertEqual(len(RedisManager.get_recent_closes("AAPL", "1day")), 3)

    def test_force_reloads_without_duplicating(self):
        self.seed()
        self.seed(force=True)
        self.assertEqual(len(RedisManager.get_recent_closes("AAPL", "1day")), 3)

    def test_a_missing_high_or_low_stores_the_close_only(self):
        bars = twelve_data_values("2024-01-01", [10.0])
        bars[0]["high"] = None
        self.get.return_value = FakeResponse(200, {"values": bars})
        self.seed()
        self.assertNotIn("high", RedisManager.get_recent_closes("AAPL", "1day")[0])

    def test_a_candle_stored_twice_for_one_time_keeps_the_newer_write(self):
        # TwelveData's newest bar is still forming; the live worker later stores the finished candle.
        async_to_sync(RedisManager.store_close_price)("AAPL", "1day", 10.0, DAY, enqueue_jobs=False)
        async_to_sync(RedisManager.store_close_price)("AAPL", "1day", 10.8, DAY, high=11.0, low=9.5)
        closes = RedisManager.get_recent_closes("AAPL", "1day")
        self.assertEqual([(c["timestamp"], c["close"]) for c in closes], [(DAY, 10.8)])


class SeedCandlesCommandTests(SimpleTestCase):
    def run_command(self, *args):
        with mock.patch.object(RedisManager, "populate_redis_all", new=mock.AsyncMock()) as populate:
            call_command("seed_candles", *args, stdout=StringIO())
        return populate

    def test_loads_history(self):
        self.run_command().assert_awaited_once_with(force=False)

    def test_force_flag_is_passed_on(self):
        self.run_command("--force").assert_awaited_once_with(force=True)
