import pandas as pd
from api.indicators.signal import Signal

# Moving averages on the price scale: the average itself, in the price's own units. SMA and EMA divide the average by
# the close (see sma.py and ema.py) and strategies saved with them depend on that, so these are separate indicators.
# They exist to be compared with each other ("the 20-candle average is above the 50-candle average").


def calculate_sma_price(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Simple moving average of the close, in price units."""
    data = data.copy()
    data[f"{signal.name}_{signal.window}"] = data["close"].rolling(window=signal.window).mean()
    return data


def calculate_ema_price(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Exponential moving average of the close, in price units."""
    data = data.copy()
    data[f"{signal.name}_{signal.window}"] = data["close"].ewm(span=signal.window, adjust=False).mean()
    return data
