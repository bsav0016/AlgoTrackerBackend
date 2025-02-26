from django.apps import AppConfig
import asyncio
import threading
from api.websocket_client import connect_to_twelvedata


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        thread = threading.Thread(target=asyncio.run, args=(connect_to_twelvedata(),))
        thread.daemon = True
        #thread.start()
