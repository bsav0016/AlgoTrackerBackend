import pandas as pd
from api.indicators.signal import Signal


def calculate_adx(data: pd.DataFrame, signal: Signal) -> pd.DataFrame:
    """Calculates the Average Directional Index (ADX) and adds it to the DataFrame."""
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
