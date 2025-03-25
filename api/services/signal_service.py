import pandas as pd
from api.indicators.rsi import calculate_rsi
from api.indicators.sma import calculate_sma
from api.indicators.ema import calculate_ema
from api.indicators.bbp import calculate_bbp
from api.indicators.adx import calculate_adx
from api.indicators.macd import calculate_macd
from api.indicators.so import calculate_so
from api.indicators.signal import Signal


class SignalService:
    @staticmethod
    def calculate_indicators(data: pd.DataFrame, indicators: list) -> pd.DataFrame:
        indicator_data = data
        for indicator in indicators:
            indicator_data = SignalService.get_indicator_values(indicator, indicator_data)
        return indicator_data

    @staticmethod
    def get_indicator_values(signal: Signal, data: pd.DataFrame) -> pd.DataFrame:
        if signal.name == "RSI":
            if f"{signal.name}_{signal.window}" in data:
                return data
            else:
                return calculate_rsi(data, signal)
        elif signal.name == "SMA":
            if f"{signal.name}_{signal.window}" in data:
                return data
            else:
                return calculate_sma(data, signal)
        elif signal.name == "EMA":
            if f"{signal.name}_{signal.window}" in data:
                return data
            else:
                return calculate_ema(data, signal)
        elif signal.name == "MACD":
            if f"{signal.name}_{signal.fast_window}_{signal.slow_window}_{signal.signal_window}" in data:
                return data
            else:
                return calculate_macd(data, signal)
        elif signal.name == "SO":
            if f"{signal.name}_{signal.window}" in data:
                return data
            else:
                return calculate_so(data, signal)
        elif signal.name == "BBP":
            if f"{signal.name}_{signal.window}" in data:
                return data
            else:
                return calculate_bbp(data, signal)
        elif signal.name == "ADX":
            if f"{signal.name}_{signal.window}" in data:
                return data
            else:
                return calculate_adx(data, signal)
        #Add indicator data here
        else:
            raise ValueError("Indicator not recognized")
