from datetime import timedelta
import logging

from django.db import transaction
from django.utils import timezone
from django_rq import job
import pandas as pd

logger = logging.getLogger(__name__)


@job
def process_strategy_task(strategy_id, symbol, interval, ts):
    """Evaluate one strategy on the candle at `ts`.

    Safe to run twice or out of order: the strategy row is locked, and a candle that is not newer than
    `last_processed_ts` is skipped. Prices are read from Redis here, up to `ts`, so a late job never sees
    candles that arrived after the one it was queued for.
    """
    from api.models import Strategy
    from api.redis_manager import RedisManager

    with transaction.atomic():
        strategy = Strategy.objects.select_for_update().filter(id=strategy_id).first()
        if strategy is None:  # deleted (for example, the user could not pay) since the job was queued
            return
        if strategy.last_processed_ts is not None and ts <= strategy.last_processed_ts:
            return
        rows = [row for row in RedisManager.get_recent_closes(symbol, interval) if row["timestamp"] <= ts]
        strategy.check_signals(pd.DataFrame(rows), ts=ts)


MAX_DELIVERY_ATTEMPTS = 5


def _retry_delay(attempts):
    return timedelta(seconds=2 ** attempts)  # 2, 4, 8, 16 seconds after attempts 1..4


@job
def send_notification_task(notification_id):
    """Send every due, pending delivery of one notification, in one batch.

    Safe to run twice (even at once): rows are claimed with a row lock and only `pending` rows that are due are
    sent, so a repeat finds nothing to do. A failure of the whole request or of one device's ticket is recorded
    on that delivery and retried with a growing delay (up to MAX_DELIVERY_ATTEMPTS) by queueing this job again
    for later; a token Expo reports as unregistered is deactivated and not retried.
    """
    import django_rq
    from api.models import PushDelivery
    from api.services.notification_service import DEVICE_NOT_REGISTERED, NotificationService

    now = timezone.now()
    retry_after = None
    with transaction.atomic():
        deliveries = list(
            PushDelivery.objects.select_for_update()
            .filter(notification_id=notification_id, status=PushDelivery.PENDING, next_attempt_at__lte=now)
            .select_related("notification", "device").order_by("pk"))
        if not deliveries:
            return
        messages = [
            (d.device.token, d.notification.message,
             {"strategy_id": d.notification.strategy_id, "trade_id": d.notification.trade_id})
            for d in deliveries]
        try:
            tickets = NotificationService.send_batch(messages)
            batch_error = ""
        except Exception as exc:
            logger.warning("Push request for notification %s failed: %s", notification_id, exc)
            tickets, batch_error = [None] * len(deliveries), f"{type(exc).__name__}: {exc}"[:255]

        for delivery, ticket in zip(deliveries, tickets):
            delivery.attempts += 1
            if ticket is not None and ticket.is_success():
                delivery.status, delivery.ticket_id, delivery.sent_at = PushDelivery.SENT, ticket.id, now
                delivery.last_error = ""
            elif ticket is not None and NotificationService.error_code(ticket) == DEVICE_NOT_REGISTERED:
                delivery.status = PushDelivery.DEAD_TOKEN
                delivery.last_error = DEVICE_NOT_REGISTERED
                delivery.device.active = False
                delivery.device.save(update_fields=["active"])
            else:
                delivery.last_error = batch_error or f"{ticket.status}: {ticket.message}"[:255]
                if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
                    delivery.status = PushDelivery.FAILED
                else:
                    delay = _retry_delay(delivery.attempts)
                    delivery.next_attempt_at = now + delay
                    retry_after = delay if retry_after is None else min(retry_after, delay)
            delivery.save()

    if retry_after is not None:
        django_rq.get_queue().enqueue_in(retry_after, send_notification_task, notification_id)
