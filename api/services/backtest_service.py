from django.conf import settings
import requests
from api.services.signal_service import SignalService
import pandas as pd
from api.indicators.signal import Signal
import datetime
from typing import List


class BacktestService:
    @staticmethod
    def conduct_backtest(
            symbol: str,
            interval: str,
            start_date: datetime,
            end_date: datetime,
            signals: List[Signal]
    ) -> pd.DataFrame:
        data = BacktestService.get_data(symbol, interval, end_date)
        indicator_data = SignalService.calculate_indicators(data, signals)

        start_date = pd.Timestamp(start_date).tz_localize(None)
        indicator_data["datetime"] = pd.to_datetime(indicator_data["datetime"], errors="coerce")
        indicator_data = indicator_data[indicator_data["datetime"] >= start_date]

        indicator_data = BacktestService.find_buys(indicator_data, signals)
        indicator_data = BacktestService.find_sells(indicator_data, signals)
        return BacktestService.calculate_returns(indicator_data)

    @staticmethod
    def get_data(symbol: str, interval: str, end_date: datetime) -> pd.DataFrame:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 5000,
            "end_date": end_date.strftime("%Y-%m-%d"),
            "apikey": settings.TWELVE_DATA_API_KEY
        }

        response = requests.get(url, params=params)

        if response.status_code == 200:
            data = response.json()
            if 'values' in data:
                df = pd.DataFrame(data["values"], columns=["datetime", "close"])
                df["datetime"] = df["datetime"].astype(str)
                df["datetime"] = pd.to_datetime(df["datetime"].astype(str))
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df = df[::-1].reset_index(drop=True)
                return df
            else:
                raise ValueError(f"Values not found in response")
        else:
            raise Exception(f"API request failed with status {response.status_code}: {response.text}")

    @staticmethod
    def find_buys(data: pd.DataFrame, signals: List[Signal]) -> pd.DataFrame:
        new_data = data.copy()
        new_data["buy"] = True
        for signal in signals:
            if signal.buy_signal:
                indicator_column = f"{signal.name}_{signal.window}"
                condition = new_data[indicator_column] > signal.target_value if signal.above_target else (
                        new_data[indicator_column] < signal.target_value)
                new_data["buy"] = new_data["buy"] & condition
        return new_data

    @staticmethod
    def find_sells(data: pd.DataFrame, signals: List[Signal]) -> pd.DataFrame:
        new_data = data.copy()
        new_data["sell"] = True
        for signal in signals:
            if not signal.buy_signal:
                indicator_column = f"{signal.name}_{signal.window}"
                condition = new_data[indicator_column] > signal.target_value if signal.above_target else (
                        new_data[indicator_column] < signal.target_value)
                new_data["sell"] = new_data["sell"] & condition
        return new_data

    @staticmethod
    def calculate_returns(data: pd.DataFrame) -> pd.DataFrame:
        new_data = data.copy()
        new_data["owned"] = 0
        new_data["return"] = 1.0
        new_data["return"] = new_data["return"].astype(float)

        for i in range(1, len(new_data)):
            prev_owned = new_data.iloc[i - 1]["owned"]
            curr_buy = new_data.iloc[i]["buy"]
            curr_sell = new_data.iloc[i]["sell"]
            prev_return = new_data.iloc[i - 1]["return"]
            prev_close = new_data.iloc[i - 1]["close"]
            curr_close = new_data.iloc[i]["close"]

            if curr_buy and not prev_owned and not curr_sell:
                new_data.iloc[i, new_data.columns.get_loc("owned")] = 1
            elif curr_sell and prev_owned and not curr_buy:
                new_data.iloc[i, new_data.columns.get_loc("owned")] = 0
            else:
                new_data.iloc[i, new_data.columns.get_loc("owned")] = prev_owned

            if prev_owned:
                new_data.iloc[i, new_data.columns.get_loc("return")] = prev_return * (curr_close / prev_close)
            else:
                new_data.iloc[i, new_data.columns.get_loc("return")] = prev_return

        return new_data
