from django.conf import settings
import requests
from api.services.signal_service import SignalService
import pandas as pd
from api.indicators.signal import Signal
import datetime
from typing import List
import matplotlib.pyplot as plt
import io
import base64
from decimal import Decimal


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
        new_data = data.copy().reset_index(drop=True)
        new_data["owned"] = 0
        new_data["return"] = 1.0
        new_data["return"] = new_data["return"].astype(float)

        print(new_data.index)
        print(new_data.head())
        new_data.at[0, "return"] = new_data.at[0, "close"]

        for i in range(1, len(new_data)):
            prev_owned = new_data.iloc[i - 1]["owned"]
            curr_buy = new_data.iloc[i]["buy"]
            curr_sell = new_data.iloc[i]["sell"]
            prev_return = new_data.iloc[i - 1]["return"]
            prev_close = new_data.iloc[i - 1]["close"]
            curr_close = new_data.iloc[i]["close"]

            if curr_buy and not prev_owned and not curr_sell:
                new_data.at[i, "owned"] = 1
            elif curr_sell and prev_owned and not curr_buy:
                new_data.at[i, "owned"] = 0
            else:
                new_data.at[i, "owned"] = prev_owned
            if prev_owned:
                new_data.at[i, "return"] = prev_return * (curr_close / prev_close)
            else:
                new_data.at[i, "return"] = prev_return

        return new_data

    @staticmethod
    def generate_backtest_chart(backtest_results: pd.DataFrame) -> str:
        if backtest_results.empty:
            raise ValueError("Backtest results are empty. Cannot generate chart.")
        results = backtest_results.copy().dropna()
        print(results)

        dates = results["datetime"]
        prices = results["close"]
        returns = results["return"]

        results["buy_signal_transition"] = (
                (results["buy"] == True) & (results["sell"] == False) & (results["owned"].shift(1, fill_value=0) == 0)
        )

        results["sell_signal_transition"] = (
                (results["sell"] == True) & (results["buy"] == False) & (results["owned"].shift(1, fill_value=0) == 1)
        )

        buy_signals = results[results["buy_signal_transition"]]
        sell_signals = results[results["sell_signal_transition"]]

        min_value = min(prices.min(), returns.min())
        max_value = max(prices.max(), returns.max())
        buffer = (max_value - min_value) * 0.02

        fig, ax1 = plt.subplots(figsize=(10, 5))

        ax1.plot(dates, prices, color="blue", label="Asset Price")
        ax1.plot(dates, returns, color="red", label="Strategy Value")
        ax1.set_xlabel("Date")
        ax1.set_ylabel("Price")
        ax1.set_ylim(min_value - buffer, max_value + buffer)
        ax1.grid(True, which='both', axis='both', linestyle='--', color='gray', alpha=0.5)

        for idx, date in enumerate(buy_signals["datetime"]):
            if idx == 0:
                ax1.axvline(x=date, color="green", linewidth=0.5, label="Buy Signal")
            else:
                ax1.axvline(x=date, color="green", linewidth=0.5)

        for idx, date in enumerate(sell_signals["datetime"]):
            if idx == 0:
                ax1.axvline(x=date, color="red", linewidth=0.5, label="Sell Signal")
            else:
                ax1.axvline(x=date, color="red", linewidth=0.5)

        fig.autofmt_xdate()
        ax1.legend(loc="upper left")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)

        img_base64 = base64.b64encode(buf.read()).decode("utf-8")
        buf.close()
        plt.close(fig)
        del fig

        return img_base64

    @staticmethod
    def calculate_sharpe_ratio(backtest_results: pd.DataFrame, risk_free_rate: float = 0.0) -> float:
        """Calculates the Sharpe Ratio for the given backtest results."""
        if backtest_results.empty or "return" not in backtest_results:
            raise ValueError("Backtest results must contain return data to calculate Sharpe Ratio.")

        returns = backtest_results["return"].dropna().pct_change()
        excess_returns = returns - risk_free_rate

        if returns.std() == 0:
            return float("nan")

        sharpe_ratio = excess_returns.mean() / excess_returns.std()
        return sharpe_ratio

    @staticmethod
    def calculate_sortino_ratio(backtest_results: pd.DataFrame) -> float:
        """Calculates the Sortino Ratio using the stock's return as the benchmark."""
        if backtest_results.empty or "return" not in backtest_results or "close" not in backtest_results:
            raise ValueError("Backtest results must contain return and stock price data.")

        strategy_returns = backtest_results["return"].dropna().pct_change()
        stock_returns = backtest_results["close"].dropna().pct_change()

        min_length = min(len(strategy_returns), len(stock_returns))
        strategy_returns = strategy_returns.iloc[:min_length]
        stock_returns = stock_returns.iloc[:min_length]

        excess_returns = strategy_returns - stock_returns

        downside_returns = excess_returns[excess_returns < 0]

        if downside_returns.std() == 0:
            return float("nan")

        sortino_ratio = excess_returns.mean() / downside_returns.std()
        return sortino_ratio

    @staticmethod
    def charge_user(user, amount):
        amount_decimal = Decimal(str(amount))
        funds_diff = user.monthly_funds - amount_decimal
        if funds_diff > 0:
            user.monthly_funds = funds_diff
            user.save()
            return True
        elif user.monthly_funds + user.account_funds >= amount_decimal:
            user.monthly_funds = 0
            user.account_funds -= funds_diff
            user.save()
            return True
        else:
            return False
