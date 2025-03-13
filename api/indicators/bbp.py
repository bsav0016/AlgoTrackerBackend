import pandas as pd
from api.indicators.signal import Signal


def calculate_bbp(data: pd.DataFrame, signal: Signal, num_std_dev: float = 2) -> pd.DataFrame:
    """Calculates the Bollinger Bands %B and adds it to the DataFrame."""
    data = data.copy()

    column_name = f"{signal.name}_{signal.window}"
    sma = data["close"].rolling(window=signal.window).mean()

    rolling_std = data["close"].rolling(window=signal.window).std()

    upper_band = sma + (rolling_std * num_std_dev)
    lower_band = sma - (rolling_std * num_std_dev)

    data[column_name] = (data["close"] - lower_band) / (upper_band - lower_band)

    return data
