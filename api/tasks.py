from django_rq import job
import pandas as pd
from io import StringIO


@job
def process_strategy_task(strategy_id, prices_json):
    from api.models import Strategy
    strategy = Strategy.objects.get(id=strategy_id)
    prices_data = StringIO(prices_json)
    prices = pd.read_json(prices_data)
    strategy.check_signals(prices)
