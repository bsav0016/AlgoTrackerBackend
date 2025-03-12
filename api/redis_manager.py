import redis
import json
from django.conf import settings
import pandas as pd
from asgiref.sync import sync_to_async
from api.tasks import process_strategy_task


redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DATABASE,
    decode_responses=True
)


class RedisManager:
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