import pandas as pd
from api.indicators.rsi import calculate_rsi
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
        else:
            raise ValueError("Indicator not recognized")
