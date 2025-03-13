import pandas as pd
from api.indicators.signal import Signal


def calculate_rsi(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    data = data.copy()

    delta = data["close"].diff(1)
    gain = (delta.where(delta > 0, 0)).rolling(window=signal.window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=signal.window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    column_name = f"{signal.name}_{signal.window}"
    data[column_name] = rsi

    return data
