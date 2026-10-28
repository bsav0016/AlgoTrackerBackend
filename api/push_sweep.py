"""Housekeeping for push notifications: re-queue sends that were missed, and settle sent ones from Expo's receipts.

Safe to run as often and as concurrently as you like: the send job only touches `pending` rows that are due,
and a receipt is applied only to a row still `sent`.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from api.models import PushDelivery
from api.services.notification_service import DEVICE_NOT_REGISTERED, NotificationService

logger = logging.getLogger(__name__)

# A pending delivery normally goes out within seconds of becoming due; only one this overdue was missed (the
# worker died after the commit, or a retry job was lost).
OVERDUE_AFTER = timedelta(seconds=60)
# Expo has receipts ready about 15 minutes after sending and keeps them for roughly a day.
RECEIPT_AFTER = timedelta(minutes=15)
RECEIPT_UNTIL = timedelta(hours=24)


def requeue_missed(now):
    """Queue the send job again for every notification that has a pending delivery long overdue."""
    from api.tasks import send_notification_task
    ids = (PushDelivery.objects.filter(status=PushDelivery.PENDING, next_attempt_at__lte=now - OVERDUE_AFTER)
           .values_list("notification_id", flat=True).distinct())
    ids = list(ids)
    for notification_id in ids:
        send_notification_task.delay(notification_id)
    return len(ids)


def settle_receipts(now):
    """Ask Expo how the sent pushes ended up. A receipt marks the delivery `delivered`, or `dead_token` (and
    deactivates the device) when the app was uninstalled, or `failed` for any other error."""
    sent = list(PushDelivery.objects
                .filter(status=PushDelivery.SENT, sent_at__lte=now - RECEIPT_AFTER, sent_at__gte=now - RECEIPT_UNTIL)
                .exclude(ticket_id="").select_related("device"))
    if not sent:
        return 0
    try:
        receipts = NotificationService.fetch_receipts([d.ticket_id for d in sent])
    except Exception:
        logger.exception("Could not fetch push receipts")
        return 0
    settled = 0
    for delivery in sent:
        receipt = receipts.get(delivery.ticket_id)
        if receipt is None:
            continue
        if receipt.is_success():
            delivery.status = PushDelivery.DELIVERED
        elif (receipt.details or {}).get("error") == DEVICE_NOT_REGISTERED:
            delivery.status = PushDelivery.DEAD_TOKEN
            delivery.last_error = DEVICE_NOT_REGISTERED
            delivery.device.active = False
            delivery.device.save(update_fields=["active"])
        else:
            delivery.status = PushDelivery.FAILED
            delivery.last_error = f"{receipt.status}: {receipt.message}"[:255]
        delivery.save(update_fields=["status", "last_error"])
        settled += 1
    return settled


def sweep(now=None):
    """One pass. Returns `(notifications re-queued, receipts settled)`."""
    now = now or timezone.now()
    return requeue_missed(now), settle_receipts(now)


def backlog(now=None):
    """Counts for the health check: pending and failed deliveries, and how overdue the oldest pending one is."""
    now = now or timezone.now()
    pending = PushDelivery.objects.filter(status=PushDelivery.PENDING)
    oldest = pending.order_by("next_attempt_at").values_list("next_attempt_at", flat=True).first()
    return {
        "pending_deliveries": pending.count(),
        "failed_deliveries_24h": PushDelivery.objects.filter(
            status=PushDelivery.FAILED, notification__created_at__gte=now - timedelta(hours=24)).count(),
        "oldest_pending_seconds": max(0, int((now - oldest).total_seconds())) if oldest else None,
    }
