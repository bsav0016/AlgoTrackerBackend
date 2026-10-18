import os
import django

# Manually set Django settings before using anything from Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "algo_tracker_backend.settings")
django.setup()

import asyncio
import logging
from api.websocket_client import connect_to_twelvedata

async def run_websocket():
    """Start the WebSocket connection and keep it alive."""
    while True:
        try:
            await connect_to_twelvedata()
        except Exception as e:
            logging.getLogger(__name__).exception("WebSocket worker error")
            await asyncio.sleep(5)  # Wait before retrying

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_websocket())
