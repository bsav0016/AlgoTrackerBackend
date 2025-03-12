from firebase_admin import messaging


class NotificationService:
    @staticmethod
    def send_notification(device_id: str, title: str, body: str, data=None):
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data,
            token=device_id
        )
        try:
            response = messaging.send(message)
            return response
        except Exception as e:
            return f"Error sending message: {e}"
