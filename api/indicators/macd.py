import pandas as pd
from api.indicators.signal import Signal, LEGACY_VERSION


def calculate_macd(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """MACD, its signal line and one value to compare against a target.

    Version 1 divides the MACD line by the signal line, which flips sign whenever the signal line is negative.
    Version 2 is the histogram (MACD line minus signal line) as a percent of the close, so it is above zero
    exactly when the MACD line is above its signal line, whatever the price level.
    """
    data = data.copy()

    fast_window = signal.fast_window
    slow_window = signal.slow_window
    signal_window = signal.signal_window

    data['EMA_fast'] = data['close'].ewm(span=fast_window, adjust=False).mean()
    data['EMA_slow'] = data['close'].ewm(span=slow_window, adjust=False).mean()

    data['MACD'] = data['EMA_fast'] - data['EMA_slow']

    data['Signal'] = data['MACD'].ewm(span=signal_window, adjust=False).mean()

    column_name = f"{signal.name}_{fast_window}_{slow_window}_{signal_window}"
    if signal.version == LEGACY_VERSION:
        data[column_name] = data['MACD'] / data['Signal']
    else:
        data[column_name] = 100 * (data['MACD'] - data['Signal']) / data['close']

    return data
