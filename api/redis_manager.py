import json
from django.conf import settings
import pandas as pd
from asgiref.sync import sync_to_async
from api.tasks import process_strategy_task
import requests
from datetime import datetime, timezone
import asyncio
import logging
from django_redis import get_redis_connection
from api.candles import bucket_start, merge_tick, new_candle

logger = logging.getLogger(__name__)


redis_client = get_redis_connection("default")

TWELVE_DATA_API_KEY = settings.TWELVE_DATA_API_KEY
SYMBOLS = settings.SYMBOLS
INTERVAL_MAP = settings.INTERVAL_MAP


class CreditsUsedError(ValueError):
    """Custom exception for when API credits are exhausted."""
    pass


class RedisManager:
    @staticmethod
    async def populate_redis_all(force: bool = False):
        """Load history for every configured symbol and interval (the `seed_candles` command runs this).

        A pair that already has candles is left alone, because history stored on top of live candles would
        duplicate them; `force` clears it and loads again. Strategies are not run on history.
        """
        unread_pairs = [[symbol, interval] for symbol in list(SYMBOLS.keys()) for interval in list(INTERVAL_MAP.keys())]
        if force:
            for symbol, interval in unread_pairs:
                RedisManager.clear_close_prices(symbol, interval)
        else:
            unread_pairs = [pair for pair in unread_pairs if RedisManager.get_last_close_time(*pair) is None]

        while unread_pairs:
            end_date = datetime.now(tz=timezone.utc)
            new_unread_pairs = []
            credits_used = False

            for symbol, interval in unread_pairs:
                if credits_used:
                    new_unread_pairs.append([symbol, interval])  # Skip remaining if credits are used
                else:
                    try:
                        await RedisManager.populate_redis_symbol(symbol, interval, end_date)
                    except CreditsUsedError:
                        credits_used = True
                        new_unread_pairs.append([symbol, interval])  # Retry later
                    except Exception as e:
                        new_unread_pairs.append([symbol, interval])
                        logger.warning("Redis load failed for %s %s: %s", symbol, interval, e)

            unread_pairs = new_unread_pairs

            if unread_pairs:
                new_time = datetime.now(tz=timezone.utc)
                wait_time = max(0., 60 - (new_time - end_date).total_seconds())  # Prevent negative wait time
                logger.info("Waiting %.2f seconds before retrying", wait_time)
                await asyncio.sleep(wait_time)

        logger.info("Finished loading redis")

    @staticmethod
    async def populate_redis_symbol(symbol: str, interval: str, end_date: datetime):
        """Fetch data from TwelveData API and store it in Redis."""
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 500,
            # With a date alone intraday history stops at midnight UTC, leaving a gap up to now.
            "end_date": end_date.strftime("%Y-%m-%d %H:%M:%S"),
            "apikey": TWELVE_DATA_API_KEY
        }

        response = requests.get(url, params=params)

        if response.status_code == 429:
            raise CreditsUsedError("All credits used (429 Too Many Requests)")

        if response.status_code == 200:
            data = response.json()
            if "values" in data:
                df = pd.DataFrame(data["values"], columns=["datetime", "close", "high", "low"])
                df["datetime"] = pd.to_datetime(df["datetime"])
                for column in ("close", "high", "low"):
                    df[column] = pd.to_numeric(df[column], errors="coerce")
                df = df[::-1].reset_index(drop=True)  # Reverse for chronological order

                for _, row in df.iterrows():
                    timestamp = int(row["datetime"].timestamp())
                    high, low = (None, None) if pd.isna(row["high"]) or pd.isna(row["low"]) else (row["high"], row["low"])
                    await RedisManager.store_close_price(
                        symbol, interval, row["close"], timestamp, high=high, low=low, enqueue_jobs=False)
                return

            if "code" in data and data["code"] == 429:
                raise CreditsUsedError("All credits used (429 Too Many Requests)")

            raise ValueError("Values not found in response")

        raise Exception(f"API request failed with status {response.status_code}: {response.text}")

    @staticmethod
    async def store_close_price(symbol: str, interval: str, close_price: float, timestamp: int, max_candles=500,
                                high: float = None, low: float = None, enqueue_jobs: bool = True):
        """Store a close price in Redis, keeping only the last `max_candles` values.

        `high` and `low` are stored with the close when given, because the SO and ADX
        indicators need them.

        `enqueue_jobs=False` stores the candle without checking strategies on it (used when loading history).
        """
        from api.models import Strategy
        key = f"close_prices:{symbol}:{interval}"
        entry = {"timestamp": timestamp, "close": close_price}
        if high is not None and low is not None:
            entry.update(high=high, low=low)
        data = json.dumps(entry)

        await sync_to_async(redis_client.lpush)(key, data)
        await sync_to_async(redis_client.ltrim)(key, 0, max_candles - 1)

        if not enqueue_jobs:
            return
        strategy_ids = await sync_to_async(list)(Strategy.objects.filter(symbol=symbol, interval=interval).values_list('id', flat=True))
        if strategy_ids:
            for strategy_id in strategy_ids:
                process_strategy_task.delay(strategy_id, symbol, interval, timestamp)

    @staticmethod
    async def record_tick(symbol: str, interval: str, price: float, timestamp: int, seconds: int):
        """Fold one price tick into the candle for its `seconds`-long bucket.

        The candle being built is kept in Redis. When a tick lands in a later bucket, the finished candle is
        stored (with its high and low) and strategies are checked on it; a candle is only ever stored once, and
        buckets with no ticks produce no candle. A tick for an earlier bucket than the open one is ignored.
        """
        key = f"open_candle:{symbol}:{interval}"
        bucket = bucket_start(timestamp, seconds)
        raw = await sync_to_async(redis_client.get)(key)
        candle = json.loads(raw) if raw else None

        if candle is not None and bucket < candle["timestamp"]:
            return
        if candle is not None and bucket == candle["timestamp"]:
            candle = merge_tick(candle, price)
        else:
            if candle is not None:
                await RedisManager.store_close_price(
                    symbol, interval, candle["close"], candle["timestamp"], high=candle["high"], low=candle["low"])
            candle = new_candle(bucket, price)

        await sync_to_async(redis_client.set)(key, json.dumps(candle))

    @staticmethod
    def mark_tick(now: int):
        """Remember when the websocket worker last received a price (shown by the health endpoint)."""
        redis_client.set("last_tick_at", now)

    @staticmethod
    def seconds_since_last_tick(now: int):
        """Seconds since the last price arrived, or None if none has been seen."""
        raw = redis_client.get("last_tick_at")
        return None if raw is None else max(0, now - int(raw))

    @staticmethod
    def get_recent_closes(symbol: str, interval: str, count=500):
        """Retrieve the latest `count` close prices, oldest first.

        The list is stored newest-first (lpush), but indicators and check_signals expect chronological rows.
        """
        key = f"close_prices:{symbol}:{interval}"
        raw_data = redis_client.lrange(key, 0, count - 1)
        newest_first = {}
        for entry in map(json.loads, raw_data):
            newest_first.setdefault(entry["timestamp"], entry)  # seeded history may overlap a live candle: keep the newer write
        return sorted(newest_first.values(), key=lambda entry: entry["timestamp"])

    @staticmethod
    def clear_close_prices(symbol: str, interval: str):
        """Clear all stored close prices for a given symbol & interval."""
        key = f"close_prices:{symbol}:{interval}"
        redis_client.delete(key)

    @staticmethod
    def get_last_close_time(symbol: str, interval: str):
        """Get the most recent close price."""
        key = f"close_prices:{symbol}:{interval}"
        data = redis_client.lindex(key, 0)
        try:
            json_data = json.loads(data)
            return json_data["timestamp"]
        except Exception:
            return None

    @staticmethod
    def store_otp(email: str, otp: int):
        redis_client.setex(f"password_reset_otp:{email}", 600, otp)
        redis_client.delete(f"password_reset_attempts:{email}")  # a new code starts with a clean count

    @staticmethod
    def get_otp(email: str):
        otp = redis_client.get(f"password_reset_otp:{email}")
        return otp.decode() if isinstance(otp, bytes) else otp  # redis-py returns bytes

    @staticmethod
    def delete_otp(email: str):
        redis_client.delete(f"password_reset_otp:{email}")
        redis_client.delete(f"password_reset_attempts:{email}")

    @staticmethod
    def record_failed_otp_attempt(email: str) -> int:
        """Count one wrong guess at the current reset code and return the running total."""
        key = f"password_reset_attempts:{email}"
        attempts = redis_client.incr(key)
        if attempts == 1:
            redis_client.expire(key, 600)  # same lifetime as the code itself
        return attempts
