import websockets
import json
import asyncio
from django.conf import settings
import global_storage


async def on_open(ws):
    """Send subscription request on WebSocket connection."""
    try:
        await ws.send(json.dumps({
            "action": "subscribe",
            "params": {
                "symbols": ",".join(global_storage.SYMBOLS),
                "apikey": settings.TWELVE_DATA_API_KEY
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
            global_storage.latest_prices[data["symbol"]] = data["price"]

    except json.JSONDecodeError as e:
        print(f"JSON decoding error: {e}")
        raise e
    except Exception as e:
        print(f"Error processing message: {e}")
        raise e


async def connect_to_twelvedata():
    """Connect to TwelveData WebSocket with automatic reconnection when needed."""
    url = f"wss://ws.twelvedata.com/v1/quotes/price?apikey={settings.TWELVE_DATA_API_KEY}"

    while True:
        try:
            async with websockets.connect(url) as ws:
                await on_open(ws)  # Send initial subscription or authentication message

                while True:
                    message = await ws.recv()  # Wait for incoming message
                    await on_message(message)  # Process message

        except websockets.exceptions.ConnectionClosedError as e:
            if str(e) == "no close frame received or sent":
                continue
            else:
                print(f"WebSocket connection lost: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

        except Exception as e:
            print(f"Unexpected error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)  # Wait before retrying


def get_latest_price(symbol: str):
    """Safely retrieves the latest price for a given symbol."""
    return global_storage.latest_prices.get(symbol, None)
