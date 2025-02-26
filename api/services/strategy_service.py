from celery import shared_task
import asyncio
from global_storage import latest_prices
from typing import List
from api.models import CustomUser, Strategy
from api.indicators.signal import Signal


async def collect_data(symbol: str, interval: int, strategy_id: int):
    while True:
        try:
            strategy = Strategy.objects.get(id=strategy_id)

            if not strategy.analyzing_flag:
                print(f"Stopped analyzing data for strategy {strategy_id}.")
                break

            close_price = latest_prices.get(symbol)
            if close_price is None:
                print(f"Skipping {symbol}, no data available.")
                await asyncio.sleep(interval)
                continue

            strategy.candles.append(close_price)
            strategy.save()

            print(f"Collected close price for {symbol}: {close_price}")

            await asyncio.sleep(interval)
        except Strategy.DoesNotExist:
            break
        except Exception as e:
            print(f"An error occurred: {e}. Retrying after 1 second.")
            await asyncio.sleep(1)


@shared_task
def analyze_data(symbol: str, interval: int, strategy_id: int):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(collect_data(symbol, interval, strategy_id))
    return f"Started analyzing data for {symbol}."


class StrategyService:

    def start_strategy(self, user: CustomUser, symbol: str, interval: str, signals: List[Signal]):
        buy_signals = List[Signal]()
        sell_signals = List[Signal]()
        for signal in signals:
            if signal.buy_signal:
                buy_signals.append(signal)
            else:
                sell_signals.append(signal)

        strategy = Strategy(
            user=user,
            symbol=symbol,
            interval=interval,
            buy_signals=buy_signals,
            sell_signals=sell_signals
        )
        strategy.save()

        analyze_data.delay(symbol, 10, strategy.pk) #TODO: Interval needs to be parsed to get the actual interval time
        return True
