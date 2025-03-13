import pandas as pd
from api.indicators.signal import Signal


def calculate_so(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Calculates the Stochastic Oscillator (%K) using a single window."""
    data = data.copy()

    window = signal.window

    data['L14'] = data['low'].rolling(window=window).min()
    data['H14'] = data['high'].rolling(window=window).max()

    data['%K'] = ((data['close'] - data['L14']) / (data['H14'] - data['L14'])) * 100

    column_name = f"{signal.name}_{window}"
    data[column_name] = data['%K']

    return data
