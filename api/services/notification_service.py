#from firebase_admin import messaging
# Used: https://github.com/expo-community/expo-server-sdk-python
from exponent_server_sdk import (
    DeviceNotRegisteredError,
    PushClient,
    PushMessage,
    PushServerError,
    PushTicketError,
)
import os
import requests
from requests.exceptions import ConnectionError, HTTPError
import threading

# Optionally providing an access token within a session if you have enabled push security
session = requests.Session()
session.headers.update(
    {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
    }
)

MAX_RETRIES = 5

class NotificationService:
    #@staticmethod
    #def send_notification(device_id: str, title: str, body: str, data=None):
    #    message = messaging.Message(
    #        notification=messaging.Notification(
    #            title=title,
    #            body=body,
    #        ),
    #        data=data,
    #        token=device_id
    #    )
    #    try:
    #        response = messaging.send(message)
    #        return response
    #    except Exception as e:
    #        return f"Error sending message: {e}"

    @staticmethod
    def send_push_message(token, message, extra=None, attempts=0):
        try:
            response = PushClient(session=session).publish(
                PushMessage(to=token,
                            body=message,
                            data=extra))
        except PushServerError as exc:
            # Encountered some likely formatting/validation error.
            print("Formatting or validation error: ", exc)
            raise
        except (ConnectionError, HTTPError) as exc:
            # Encountered some Connection or HTTP error - retry a few times in
            # case it is transient.
            print("Push notification failed")
            if attempts < MAX_RETRIES:
                wait_time = 2 ** attempts
                print(f"Retrying in {wait_time} seconds...")
                threading.Timer(wait_time, NotificationService.send_push_message,
                                args=[token, message, extra, attempts + 1]).start()
                return
            else:
                raise

        try:
            # We got a response back, but we don't know whether it's an error yet.
            # This call raises errors so we can handle them with normal exception
            # flows.
            response.validate_response()
            return
        except DeviceNotRegisteredError:
            # Mark the push token as inactive
            pass
        except PushTicketError as exc:
            # Encountered some other per-notification error.
            print("Push notification failed")
            if attempts < MAX_RETRIES:
                wait_time = 2 ** attempts
                print(f"Retrying in {wait_time} seconds...")
                threading.Timer(wait_time, NotificationService.send_push_message,
                                args=[token, message, extra, attempts + 1]).start()
            else:
                raise
