import threading
import asyncio
import sys
from django.apps import AppConfig
from api.websocket_client import connect_to_twelvedata


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self):
        """Starts the WebSocket client in a separate thread when Django starts."""
        if "runserver" in sys.argv:
            self.ws_thread = threading.Thread(target=self.start_websocket, daemon=True)
            self.ws_thread.start()

    @staticmethod
    def start_websocket():
        """Runs the WebSocket client inside a new event loop in a thread."""
        loop = asyncio.new_event_loop()  # Create a new event loop for the thread
        asyncio.set_event_loop(loop)  # Set this event loop as the current one for the thread
        # Use ensure_future to schedule the coroutine for execution
        asyncio.ensure_future(connect_to_twelvedata())
        loop.run_forever()  # Start the event loop and run until manually stopped
