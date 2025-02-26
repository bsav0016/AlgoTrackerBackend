import websockets
import json
from django.conf import settings
import global_storage


async def on_open(ws):
    await ws.send(json.dumps({
        "action": "subscribe",
        "params": {
            "symbols": ",".join(global_storage.SYMBOLS),
            "apikey": settings.TWELVE_DATA_API_KEY
        }
    }))


async def on_message(message):
    data = json.loads(message)
    print(f"Received message: {data}")

    if data.get("event") == "subscribe-status":
        if data.get("status") == "ok":
            print("Subscription successful!")
        else:
            print(f"Subscription failed: {data}")

    elif "symbol" in data and "price" in data:
        global_storage.latest_prices[data["symbol"]] = data["price"]
        print(f"Updated: {data['symbol']} - {data['price']}")


async def connect_to_twelvedata():
    url = f"wss://ws.twelvedata.com/v1/quotes/price?apikey={settings.TWELVE_DATA_API_KEY}"

    async with websockets.connect(url) as ws:
        await on_open(ws)

        while True:
            message = await ws.recv()
            await on_message(message)
