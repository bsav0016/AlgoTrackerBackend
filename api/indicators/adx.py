import pandas as pd
import numpy as np
from api.indicators.signal import Signal, LEGACY_VERSION


def calculate_adx(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """ADX. Version 1 uses rolling sums and a rolling mean; version 2 is Wilder's ADX."""
    if signal.version == LEGACY_VERSION:
        return _legacy_adx(data, signal)
    return _wilder_adx(data, signal)


def _wilder_adx(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Wilder's smoothing: sums of the first n true ranges and directional moves, then each later sum is
    previous - previous / n + new. +DM counts only when the up-move beats the down-move (and the reverse for -DM).
    The first ADX, a plain mean of the first n DX values, is at candle index 2n - 1."""
    data = data.copy()
    n = signal.window
    high, low, close = (data[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
    size = len(data)
    up, down = np.diff(high, prepend=np.nan), -np.diff(low, prepend=np.nan)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev_close = np.concatenate(([np.nan], close[:-1]))
    true_range = np.fmax.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])

    atr, plus_di, minus_di, dx, adx = (np.full(size, np.nan) for _ in range(5))
    if size > n:
        sum_tr, sum_plus, sum_minus = true_range[1:n + 1].sum(), plus_dm[1:n + 1].sum(), minus_dm[1:n + 1].sum()
        for i in range(n, size):
            if i > n:
                sum_tr += true_range[i] - sum_tr / n
                sum_plus += plus_dm[i] - sum_plus / n
                sum_minus += minus_dm[i] - sum_minus / n
            atr[i] = sum_tr
            plus_di[i] = 100 * sum_plus / sum_tr if sum_tr else 0.0
            minus_di[i] = 100 * sum_minus / sum_tr if sum_tr else 0.0
            total = plus_di[i] + minus_di[i]
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / total if total else 0.0
        if size >= 2 * n:
            value = dx[n:2 * n].mean()
            adx[2 * n - 1] = value
            for i in range(2 * n, size):
                value = (value * (n - 1) + dx[i]) / n
                adx[i] = value
    data["ATR"], data["+DI"], data["-DI"], data["DX"] = atr, plus_di, minus_di, dx
    data[f"{signal.name}_{signal.window}"] = adx
    return data


def _legacy_adx(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    data = data.copy()

    column_name = f"{signal.name}_{signal.window}"

    data['high-low'] = data['high'] - data['low']
    data['high-prev_close'] = abs(data['high'] - data['close'].shift(1))
    data['low-prev_close'] = abs(data['low'] - data['close'].shift(1))

    data['TR'] = data[['high-low', 'high-prev_close', 'low-prev_close']].max(axis=1)

    data['+DM'] = data['high'] - data['high'].shift(1)
    data['-DM'] = data['low'].shift(1) - data['low']

    data['+DM'] = data['+DM'].where(data['+DM'] > 0, 0)
    data['-DM'] = data['-DM'].where(data['-DM'] > 0, 0)

    data['ATR'] = data['TR'].rolling(window=signal.window).sum()
    data['+DI'] = 100 * data['+DM'].rolling(window=signal.window).sum() / data['ATR']
    data['-DI'] = 100 * data['-DM'].rolling(window=signal.window).sum() / data['ATR']

    data['DX'] = 100 * abs(data['+DI'] - data['-DI']) / (data['+DI'] + data['-DI'])
    data[column_name] = data['DX'].rolling(window=signal.window).mean()

    return data
