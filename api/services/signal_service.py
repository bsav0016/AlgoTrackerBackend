import pandas as pd
from api.indicators.rsi import calculate_rsi
from api.indicators.sma import calculate_sma
from api.indicators.ema import calculate_ema
from api.indicators.bbp import calculate_bbp
from api.indicators.adx import calculate_adx
from api.indicators.macd import calculate_macd
from api.indicators.so import calculate_so
from api.indicators.price_average import calculate_ema_price, calculate_sma_price
from api.indicators.signal import Signal, indicator_signals


# Averages on the price scale, used by comparison rules (a rule that compares two indicators with each other).
PRICE_AVERAGES = {"SMA_PRICE": calculate_sma_price, "EMA_PRICE": calculate_ema_price}


class SignalService:
    @staticmethod
    def calculate_indicators(data: pd.DataFrame, indicators: list) -> pd.DataFrame:
        indicator_data = data
        # A rule that compares two indicators needs both calculated.
        for indicator in indicator_signals(indicators):
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
        elif signal.name in PRICE_AVERAGES:
            if f"{signal.name}_{signal.window}" in data:
                return data
            else:
                return PRICE_AVERAGES[signal.name](data, signal)
        #Add indicator data here
        else:
            raise ValueError("Indicator not recognized")
