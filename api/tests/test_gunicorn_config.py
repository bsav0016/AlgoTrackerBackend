"""gunicorn.conf.py is plain Python that gunicorn executes; these tests pin the properties that matter."""
import os
import runpy
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

CONFIG = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"


def load(**env):
    with mock.patch.dict(os.environ, env):
        return runpy.run_path(str(CONFIG))


class GunicornConfigTests(SimpleTestCase):
    def test_more_than_one_request_can_run_at_a_time(self):
        conf = load()
        self.assertGreater(conf["workers"] * conf["threads"], 1)
        self.assertEqual(conf["worker_class"], "gthread")

    def test_the_worker_timeout_outlasts_the_slowest_backtest(self):
        # A backtest makes up to BACKTEST_ITERATIONS market-data requests, each allowed connect + read seconds.
        connect, read = settings.TWELVE_DATA_TIMEOUT
        worst = settings.BACKTEST_ITERATIONS * (connect + read)
        self.assertGreater(load()["timeout"], worst)

    def test_workers_are_recycled_with_jitter(self):
        conf = load()
        self.assertGreater(conf["max_requests"], 0)
        self.assertGreater(conf["max_requests_jitter"], 0)

    def test_values_can_be_overridden_from_the_environment(self):
        conf = load(GUNICORN_WORKERS="5", GUNICORN_THREADS="4", GUNICORN_TIMEOUT="200")
        self.assertEqual((conf["workers"], conf["threads"], conf["timeout"]), (5, 4, 200))

    def test_logs_go_to_the_container_output(self):
        conf = load()
        self.assertEqual((conf["accesslog"], conf["errorlog"]), ("-", "-"))
