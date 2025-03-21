import redis
import json
from django.conf import settings
import pandas as pd
from asgiref.sync import sync_to_async
from api.tasks import process_strategy_task
import requests
from datetime import datetime, timezone
import asyncio

redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DATABASE,
    decode_responses=True
)

TWELVE_DATA_API_KEY = settings.TWELVE_DATA_API_KEY
SYMBOLS = settings.SYMBOLS
INTERVAL_MAP = settings.INTERVAL_MAP


class CreditsUsedError(ValueError):
    """Custom exception for when API credits are exhausted."""
    pass


class RedisManager:
    @staticmethod
    async def populate_redis_all():
        unread_pairs = [[symbol, interval] for symbol in SYMBOLS for interval in INTERVAL_MAP.keys()]

        while unread_pairs:
            end_date = datetime.now(tz=timezone.utc)
            new_unread_pairs = []
            credits_used = False

            for symbol, interval in unread_pairs:
                if credits_used:
                    new_unread_pairs.append([symbol, interval])  # Skip remaining if credits are used
                else:
                    try:
                        print("Ran one")
                        await RedisManager.populate_redis_symbol(symbol, interval, end_date)
                    except CreditsUsedError:
                        credits_used = True
                        new_unread_pairs.append([symbol, interval])  # Retry later
                    except Exception as e:
                        new_unread_pairs.append([symbol, interval])
                        print(f"Error: {e} for {[symbol, interval]}")

            unread_pairs = new_unread_pairs

            if unread_pairs:
                new_time = datetime.now(tz=timezone.utc)
                wait_time = max(0., 60 - (new_time - end_date).total_seconds())  # Prevent negative wait time
                print(f"Waiting {wait_time:.2f} seconds before retrying...")
                await asyncio.sleep(wait_time)

        print("Finished loading redis")

    @staticmethod
    async def populate_redis_symbol(symbol: str, interval: str, end_date: datetime):
        """Fetch data from TwelveData API and store it in Redis."""
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 500,
            "end_date": end_date.strftime("%Y-%m-%d"),
            "apikey": TWELVE_DATA_API_KEY
        }

        response = requests.get(url, params=params)

        if response.status_code == 429:
            raise CreditsUsedError("All credits used (429 Too Many Requests)")

        if response.status_code == 200:
            data = response.json()
            if "values" in data:
                df = pd.DataFrame(data["values"], columns=["datetime", "close"])
                df["datetime"] = pd.to_datetime(df["datetime"])
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df = df[::-1].reset_index(drop=True)  # Reverse for chronological order

                for _, row in df.iterrows():
                    timestamp = int(row["datetime"].timestamp())
                    close_price = row["close"]
                    await RedisManager.store_close_price(symbol, interval, close_price, timestamp)
                return

            if "code" in data and data["code"] == 429:
                raise CreditsUsedError("All credits used (429 Too Many Requests)")

            raise ValueError("Values not found in response")

        raise Exception(f"API request failed with status {response.status_code}: {response.text}")

    @staticmethod
    async def store_close_price(symbol: str, interval: str, close_price: float, timestamp: int, max_candles=500):
        """Store a close price in Redis, keeping only the last `max_candles` values."""
        from api.models import Strategy
        key = f"close_prices:{symbol}:{interval}"
        data = json.dumps({"timestamp": timestamp, "close": close_price})

        await sync_to_async(redis_client.lpush)(key, data)
        await sync_to_async(redis_client.ltrim)(key, 0, max_candles - 1)

        strategy_ids = await sync_to_async(list)(Strategy.objects.filter(symbol=symbol, interval=interval).values_list('id', flat=True))
        if strategy_ids:
            prices_json = pd.DataFrame(RedisManager.get_recent_closes(symbol, interval)).to_json()
            for strategy_id in strategy_ids:
                process_strategy_task.delay(strategy_id, prices_json)

    @staticmethod
    def get_recent_closes(symbol: str, interval: str, count=500):
        """Retrieve the latest `count` close prices."""
        key = f"close_prices:{symbol}:{interval}"
        raw_data = redis_client.lrange(key, 0, count - 1)
        return [json.loads(entry) for entry in raw_data]

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

    @staticmethod
    def get_otp(email: str):
        return redis_client.get(f"password_reset_otp:{email}")

    @staticmethod
    def delete_otp(email: str):
        redis_client.delete(f"password_reset_otp:{email}")
