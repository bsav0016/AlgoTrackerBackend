import pandas as pd
from api.indicators.signal import Signal


def calculate_ema(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Calculates the Exponential Moving Average (EMA) and adds it to the DataFrame."""
    data = data.copy()

    column_name = f"{signal.name}_{signal.window}"
    data[column_name] = data["close"].ewm(span=signal.window, adjust=False).mean()

    return data
