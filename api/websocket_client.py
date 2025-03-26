import websockets
import asyncio
from django.conf import settings
from api.redis_manager import RedisManager
import json
from datetime import timezone, datetime

SYMBOLS = settings.SYMBOLS
TWELVE_DATA_API_KEY = settings.TWELVE_DATA_API_KEY
INTERVAL_MAP = settings.INTERVAL_MAP


async def on_open(ws):
    """Send subscription request on WebSocket connection."""
    try:
        await ws.send(json.dumps({
            "action": "subscribe",
            "params": {
                "symbols": ",".join(list(SYMBOLS.keys())),
                "apikey": TWELVE_DATA_API_KEY
            }
        }))
    except Exception as e:
        print(f"Error sending subscription request: {e}")


async def on_message(message):
    """Handle incoming WebSocket messages."""
    try:
        data = json.loads(message)

        print(data)
        if data.get("event") == "subscribe-status":
            if not data.get("status") == "ok":
                print(f"Subscription failed: {data}")

        elif "symbol" in data and "price" in data:
            for interval_key, interval_values in INTERVAL_MAP.items():
                last_close_time = RedisManager.get_last_close_time(data["symbol"], interval_key)
                current_timestamp = int(datetime.now(timezone.utc).timestamp())
                if last_close_time is None or (current_timestamp - last_close_time >= interval_values['seconds']):
                    await RedisManager.store_close_price(data["symbol"], interval_key, data["price"], current_timestamp)
                    print("Saved data")

    except json.JSONDecodeError as e:
        print(f"JSON decoding error: {e}")
        raise e
    except Exception as e:
        print(f"Error processing message: {e}")
        raise e


async def connect_to_twelvedata():
    """Connect to TwelveData WebSocket with automatic reconnection when needed."""
    url = f"wss://ws.twelvedata.com/v1/quotes/price?apikey={TWELVE_DATA_API_KEY}"

    while True:
        try:
            async with websockets.connect(url) as ws:
                await on_open(ws)

                while True:
                    message = await ws.recv()
                    await on_message(message)

        except websockets.exceptions.ConnectionClosedError as e:
            if str(e) == "no close frame received or sent":
                continue
            else:
                print(f"WebSocket connection lost: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

        except Exception as e:
            print(f"Unexpected error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)
