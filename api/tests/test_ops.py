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

    def test_history_is_requested_up_to_now_not_just_up_to_midnight(self):
        # With a date alone TwelveData stops intraday history at 00:00, leaving a gap up to the first live candle.
        self.seed()
        self.assertRegex(self.get.call_args.kwargs["params"]["end_date"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

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


class RedisSettingsTests(SimpleTestCase):
    def test_a_plain_redis_url_gets_no_tls_options(self):
        # redis-py raises TypeError on ssl_cert_reqs for a non-TLS connection, which broke every Redis call.
        from algo_tracker_backend.settings import redis_pool_kwargs
        self.assertEqual(redis_pool_kwargs("redis://redis:6379/0"), {})

    def test_a_tls_redis_url_keeps_its_option(self):
        from algo_tracker_backend.settings import redis_pool_kwargs
        self.assertEqual(redis_pool_kwargs("rediss://example.com:6380/0"), {"ssl_cert_reqs": None})

    def test_the_configured_cache_connection_can_be_built(self):
        from django_redis import get_redis_connection
        get_redis_connection("default").connection_pool.make_connection()  # builds, does not connect


class LoggingTests(SimpleTestCase):
    def test_request_loggers_that_print_the_api_key_stay_quiet_at_debug(self):
        import logging
        for name in ("urllib3", "websockets", "requests"):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING, name)


class TickTimeTests(FakeRedisCase):
    def message(self, **fields):
        return json.dumps({"event": "price", "symbol": "AAPL", "price": "10.5", **fields})

    def record(self, message):
        with mock.patch.object(RedisManager, "record_tick", new=mock.AsyncMock()) as record_tick:
            async_to_sync(websocket_client.on_message)(message)
        return record_tick

    def test_a_tick_is_placed_by_the_exchange_time_in_the_message(self):
        record_tick = self.record(self.message(timestamp=1700000123))
        self.assertTrue(record_tick.await_count >= 1)
        self.assertTrue(all(call.args[3] == 1700000123 for call in record_tick.await_args_list))

    def test_a_missing_or_bad_time_falls_back_to_the_clock(self):
        for fields in ({}, {"timestamp": None}, {"timestamp": "soon"}):
            with self.subTest(fields=fields):
                before = int(time.time())
                record_tick = self.record(self.message(**fields))
                used = record_tick.await_args_list[0].args[3]
                self.assertTrue(before <= used <= before + 3)
