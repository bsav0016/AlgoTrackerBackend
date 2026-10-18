from django_rq import job
import pandas as pd


@job
def process_strategy_task(strategy_id, symbol, interval, ts):
    """Evaluate one strategy on the candle at `ts`.

    Safe to run twice or out of order: the strategy row is locked, and a candle that is not newer than
    `last_processed_ts` is skipped. Prices are read from Redis here, up to `ts`, so a late job never sees
    candles that arrived after the one it was queued for.
    """
    from django.db import transaction
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
