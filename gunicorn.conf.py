"""gunicorn settings for the `web` container. Each can be overridden with the GUNICORN_* variable named beside it.

The default sync worker handles one request at a time, so one slow backtest (it fetches market data inside the
request and then draws charts) would queue every other request, including the container health check. Several
workers with a few threads each keep one slow request from blocking the rest.
"""
import os

bind = "0.0.0.0:8000"

# GUNICORN_WORKERS. Each worker holds pandas and matplotlib in memory (roughly 150 to 300 MB), so size this to the
# droplet: 3 is comfortable on 2 GB.
workers = int(os.getenv("GUNICORN_WORKERS", "3"))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "2"))

# GUNICORN_TIMEOUT. A backtest can page back through up to BACKTEST_ITERATIONS market-data requests, each allowed
# up to TWELVE_DATA_CONNECT_TIMEOUT + TWELVE_DATA_READ_TIMEOUT seconds (5 + 20 by default). The default
# BACKTEST_ITERATIONS of 5 gives 125 seconds at worst, so the worker is given a little longer than that.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "150"))
graceful_timeout = 30

# Restart each worker now and then (staggered) so slow memory growth cannot build up for ever.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 100

# The health check and Caddy talk to the worker over the Docker network; log to the container output.
accesslog = "-"
errorlog = "-"
