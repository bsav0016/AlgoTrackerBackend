from celery import shared_task
from api.models import Strategy, StockValue, CustomUser
from api.indicators.signal import Signal
from api.websocket_client import get_latest_price
from django.db.models import ObjectDoesNotExist
from typing import List
import time
import threading
import logging

logger = logging.getLogger(__name__)

# Refactor: Dictionary for intervals mapping
INTERVAL_MAP = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "30min": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1day": 86400,
    "1week": 604800
}

def get_strategy(strategy_id: int):
    """Get the strategy object asynchronously."""
    try:
        return Strategy.objects.get(id=strategy_id)
    except Exception as e:
        logger.error(f"Error fetching strategy: {e}")
        raise e

def create_stock_value(strategy, close_price):
    """Create a StockValue instance asynchronously."""
    try:
        return StockValue.objects.create(strategy=strategy, price=close_price)
    except Exception as e:
        logger.error(f"Error creating stock value: {e}")
        raise e

@shared_task
def collect_data_sync(symbol: str, interval: int, strategy_id: int):
    """Synchronous function that collects data and stores it."""
    try:
        while True:
            try:

                strategy = get_strategy(strategy_id)

                if strategy is None:
                    time.sleep(interval)
                    continue

                close_price = get_latest_price(symbol)
                if close_price is None:
                    time.sleep(interval)
                    continue

                # Create StockValue synchronously
                create_stock_value(strategy, close_price)

                # Continue the process every `interval` seconds
                time.sleep(interval)

            except ObjectDoesNotExist:
                logger.info(f"Strategy {strategy_id} no longer exists. Stopping data collection.")
                break
            except Exception as e:
                logger.error(f"An error occurred while collecting data for {symbol}: {e}")
                time.sleep(1)
    except Exception as e:
        logger.error(f"Error in collect_data_sync: {e}")

@shared_task
def analyze_data(symbol: str, interval: int, strategy_id: int):
    """Starts the data collection task inside Celery."""
    logger.info(f"Starting data collection task for {symbol}")
    thread = threading.Thread(target=collect_data_sync, args=(symbol, interval, strategy_id), daemon=True)
    thread.start()
    logger.info(f"✅ Thread started for {symbol}, is_alive: {thread.is_alive()}")
    return f"Started analyzing data for {symbol} in a separate thread."


class StrategyService:
    def start_strategy(self, user: CustomUser, symbol: str, interval: str, signals: List[Signal]):
        """Start a strategy with signals and begin analysis."""
        #buy_signals = [signal for signal in signals if signal.buy_signal]
        #sell_signals = [signal for signal in signals if not signal.buy_signal]


        strategy = Strategy(
            user=user,
            symbol=symbol,
            interval=interval
        )
        strategy.add_signals(signals)
        strategy.save()

        interval_time = self.convert_interval_to_int(interval)
        analyze_data.delay(symbol, interval_time, strategy.pk)  # Start the analysis
        return True

    @staticmethod
    def convert_interval_to_int(interval: str):
        """Convert interval string to integer representing seconds."""
        if interval in INTERVAL_MAP:
            return INTERVAL_MAP[interval]
        raise Exception(f"Unknown interval: {interval}")
