import websockets
import json
from django.conf import settings
import global_storage
import asyncio


async def on_open(ws):
    # Send the subscribe message when WebSocket opens
    await ws.send(json.dumps({
        "action": "subscribe",
        "params": {
            "symbols": ",".join(global_storage.SYMBOLS),
            "apikey": "9c78f96ff6d8419a9488ab7abb268ac6"
        }
    }))


async def on_message(ws, message):
    data = json.loads(message)
    print(f"Received message: {data}")

    # Process the status event (subscription confirmation)
    if data.get("event") == "subscribe-status":
        if data.get("status") == "ok":
            print("Subscription successful!")
        else:
            print(f"Subscription failed: {data}")

    # Process price events
    elif "symbol" in data and "price" in data:
        global_storage.latest_prices[data["symbol"]] = data["price"]
        print(f"Updated: {data['symbol']} - {data['price']}")


async def connect_to_twelvedata():
    url = f"wss://ws.twelvedata.com/v1/quotes/price?apikey=9c78f96ff6d8419a9488ab7abb268ac6"

    # Establish the WebSocket connection
    async with websockets.connect(url) as ws:
        await on_open(ws)  # Send subscribe message on connection

        # Listen for incoming messages
        while True:
            message = await ws.recv()  # Receive a message
            await on_message(ws, message)  # Process the message


asyncio.run(connect_to_twelvedata())
