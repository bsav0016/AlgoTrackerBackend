import pandas as pd
from api.indicators.signal import Signal


def calculate_macd(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Calculates the Moving Average Convergence Divergence (MACD), Signal Line, and normalizes by current price."""
    data = data.copy()

    fast_window = signal.window
    slow_window = signal.fast_window
    signal_window = signal.signal_window

    data['EMA_fast'] = data['close'].ewm(span=fast_window, adjust=False).mean()
    data['EMA_slow'] = data['close'].ewm(span=slow_window, adjust=False).mean()

    data['MACD'] = data['EMA_fast'] - data['EMA_slow']

    data['Signal'] = data['MACD'].ewm(span=signal_window, adjust=False).mean()

    column_name = f"{signal.name}_{fast_window}_{slow_window}_{signal_window}"
    data[column_name] = data['Signal'] / data['close']

    return data
