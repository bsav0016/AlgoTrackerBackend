import threading
import asyncio
import sys
from django.apps import AppConfig
from api.redis_manager import RedisManager


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    #def ready(self):
    #    """Starts the WebSocket client in a separate thread when Django starts."""
    #    if "runserver" in sys.argv:
    #        self.ws_thread = threading.Thread(target=self.start_websocket, daemon=True)
    #        self.ws_thread.start()
        #self.redis_thread = threading.Thread(target=self.run_populate_redis, daemon=True)
        #self.redis_thread.start()

    @staticmethod
    def start_websocket():
        """Runs the WebSocket client inside a new event loop in a thread."""
        from api.websocket_client import connect_to_twelvedata
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        asyncio.ensure_future(connect_to_twelvedata())
        loop.run_forever()

    @staticmethod
    def run_populate_redis():
        """Runs the populate_redis_all function inside a new event loop in a separate thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(RedisManager.populate_redis_all())