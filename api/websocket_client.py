import websockets
import asyncio
from django.conf import settings
from api.redis_manager import RedisManager
import json
import logging
from datetime import timezone, datetime

logger = logging.getLogger(__name__)

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
        logger.exception("Error sending subscription request")


async def on_message(message):
    """Handle incoming WebSocket messages."""
    try:
        data = json.loads(message)

        logger.debug("Message: %s", data)
        if data.get("event") == "subscribe-status":
            if data.get("status") == "ok":
                logger.info("Subscribed to price feed")
            else:
                logger.error("Subscription failed: %s", data)

        elif "symbol" in data and "price" in data:
            current_timestamp = int(datetime.now(timezone.utc).timestamp())
            RedisManager.mark_tick(current_timestamp)
            for interval_key, interval_values in INTERVAL_MAP.items():
                await RedisManager.record_tick(
                    data["symbol"], interval_key, float(data["price"]), current_timestamp, interval_values['seconds'])

    except json.JSONDecodeError as e:
        logger.error("JSON decoding error: %s", e)
        raise e
    except Exception as e:
        logger.exception("Error processing message")
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
                logger.warning("WebSocket connection lost: %s. Reconnecting in 5 seconds", e)
                await asyncio.sleep(5)

        except Exception as e:
            logger.exception("Unexpected WebSocket error; retrying in 5 seconds")
            await asyncio.sleep(5)
