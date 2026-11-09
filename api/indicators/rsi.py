import numpy as np
import pandas as pd
from api.indicators.signal import Signal, LEGACY_VERSION


def calculate_rsi(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """RSI. Version 1 averages gains and losses with a plain rolling mean; version 2 is Wilder's RSI."""
    if signal.version == LEGACY_VERSION:
        return _legacy_rsi(data, signal)
    return _wilder_rsi(data, signal)


def _legacy_rsi(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    data = data.copy()

    delta = data["close"].diff(1)
    gain = (delta.where(delta > 0, 0)).rolling(window=signal.window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=signal.window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    column_name = f"{signal.name}_{signal.window}"
    data[column_name] = rsi

    return data


def _wilder_rsi(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """The first average is a plain mean of the first n changes; each later one is (previous * (n - 1) + new) / n.
    The first value is at the (n+1)th candle, since n changes need n + 1 prices."""
    data = data.copy()
    n = signal.window
    change = data["close"].diff(1).to_numpy(dtype=float)
    gains, losses = np.where(change > 0, change, 0.0), np.where(change < 0, -change, 0.0)
    rsi = np.full(len(data), np.nan)
    if len(data) > n:
        avg_gain, avg_loss = gains[1:n + 1].mean(), losses[1:n + 1].mean()
        for i in range(n, len(data)):
            if i > n:
                avg_gain = (avg_gain * (n - 1) + gains[i]) / n
                avg_loss = (avg_loss * (n - 1) + losses[i]) / n
            rsi[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    data[f"{signal.name}_{signal.window}"] = rsi
    return data
