# Used: https://github.com/expo-community/expo-server-sdk-python
from exponent_server_sdk import PushClient, PushMessage
import requests

# Optionally providing an access token within a session if you have enabled push security
session = requests.Session()
session.headers.update(
    {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
    }
)

DEVICE_NOT_REGISTERED = "DeviceNotRegistered"


class NotificationService:
    @staticmethod
    def send_batch(messages):
        """Send `[(token, body, data), ...]` to Expo in as few requests as it allows.

        Returns one `PushTicket` per message, in order. Raises `requests` errors or `PushServerError` when the
        request as a whole fails; the caller decides about retrying. Nothing is retried or swallowed here.
        """
        push_messages = [PushMessage(to=token, body=body, data=data) for token, body, data in messages]
        return PushClient(session=session).publish_multiple(push_messages)

    @staticmethod
    def error_code(ticket):
        """Expo's error name for a failed ticket (for example `DeviceNotRegistered`), or ''."""
        return (ticket.details or {}).get("error", "")
