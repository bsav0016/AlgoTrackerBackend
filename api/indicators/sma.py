import pandas as pd
from api.indicators.signal import Signal


def calculate_sma(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Calculates the Simple Moving Average (SMA) and adds it to the DataFrame."""
    data = data.copy()

    column_name = f"{signal.name}_{signal.window}"
    data[column_name] = data["close"].rolling(window=signal.window).mean()
    data[column_name] = data[column_name] / data["close"]

    return data
