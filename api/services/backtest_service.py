from django.conf import settings
import requests
from api.services.signal_service import SignalService
import pandas as pd
from api.indicators.signal import Signal
import datetime
from typing import List
import matplotlib
import matplotlib.pyplot as plt
import io
import base64
import pytz

TWELVE_DATA_API_KEY = settings.TWELVE_DATA_API_KEY
BACKTEST_ITERATIONS = settings.BACKTEST_ITERATIONS

matplotlib.use("Agg")


class BacktestService:
    @staticmethod
    def conduct_backtest(
            symbol: str,
            interval: str,
            start_date: datetime,
            end_date: datetime,
            signals: List[Signal]
    ) -> tuple[pd.DataFrame, int]:
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=pytz.UTC)
        data, attempts = BacktestService.get_data(symbol, interval, start_date, end_date)
        indicator_data = SignalService.calculate_indicators(data, signals)
        indicator_data["datetime"] = pd.to_datetime(indicator_data["datetime"], errors="coerce")
        indicator_data = indicator_data[indicator_data["datetime"] >= start_date]
        indicator_data = BacktestService.find_buys(indicator_data, signals)
        indicator_data = BacktestService.find_sells(indicator_data, signals)
        return BacktestService.calculate_returns(indicator_data), attempts

    @staticmethod
    def get_data(
            symbol: str,
            interval: str,
            start_date: datetime,
            end_date: datetime,
            attempts: int = 1
    ) -> tuple[pd.DataFrame, int]:
        if attempts > BACKTEST_ITERATIONS:
            return pd.DataFrame(columns=["datetime", "close", "low", "high"]), attempts

        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 5000,
            "end_date": end_date.strftime("%Y-%m-%d"),
            "apikey": TWELVE_DATA_API_KEY
        }

        response = requests.get(url, params=params)

        if response.status_code == 200:
            data = response.json()
            if 'values' in data:
                df = pd.DataFrame(data["values"], columns=["datetime", "close", "low", "high"])
                df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")

                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df["low"] = pd.to_numeric(df["low"], errors="coerce")
                df["high"] = pd.to_numeric(df["high"], errors="coerce")
                df = df[::-1].reset_index(drop=True)
                if df.empty or df["datetime"].iloc[0] <= start_date:
                    return df, attempts

                new_end_date = df["datetime"].iloc[0]
                previous_data, max_attempts = BacktestService.get_data(symbol, interval, start_date, new_end_date, attempts + 1)
                return pd.concat([previous_data, df], ignore_index=True), max_attempts
            else:
                raise ValueError("Values not found in response")
        else:
            raise Exception(f"API request failed with status {response.status_code}: {response.text}")

    @staticmethod
    def indicator_column(signal: Signal) -> str:
        if signal.name == "MACD":
            return f"{signal.name}_{signal.fast_window}_{signal.slow_window}_{signal.signal_window}"
        return f"{signal.name}_{signal.window}"

    @staticmethod
    def find_buys(data: pd.DataFrame, signals: List[Signal]) -> pd.DataFrame:
        new_data = data.copy()
        new_data["buy"] = True
        #Add indicator data here
        for signal in signals:
            if signal.buy_signal:
                indicator_column = BacktestService.indicator_column(signal)
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
                indicator_column = BacktestService.indicator_column(signal)
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
    def generate_backtest_chart(backtest_results: pd.DataFrame) -> tuple[str, str]:
        if backtest_results.empty:
            raise ValueError("Backtest results are empty. Cannot generate chart.")
        results = backtest_results.copy().dropna()

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

        fig.autofmt_xdate()
        ax1.legend(loc="upper left")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        img_base64_no_signals = base64.b64encode(buf.read()).decode("utf-8")
        buf.close()

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

        ax1.legend(loc="upper left")
        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode("utf-8")
        buf.close()

        plt.close(fig)
        del fig

        return img_base64, img_base64_no_signals

    @staticmethod
    def calculate_sharpe_ratio(backtest_results: pd.DataFrame) -> tuple[float, float]:
        """Calculates the Sharpe Ratio for the given backtest results."""
        if backtest_results.empty or "return" not in backtest_results:
            raise ValueError("Backtest results must contain return data to calculate Sharpe Ratio.")

        returns = backtest_results["return"].dropna().pct_change()
        if returns.std() == 0:
            return float("nan"), float("nan")
        sharpe_ratio = returns.mean() / returns.std()

        #Adjusted for asset price
        strategy_returns = backtest_results["return"].dropna().pct_change()
        asset_returns = backtest_results["close"].dropna().pct_change()
        min_length = min(len(strategy_returns), len(asset_returns))
        strategy_returns = strategy_returns.iloc[:min_length]
        asset_returns = asset_returns.iloc[:min_length]
        excess_returns = strategy_returns - asset_returns
        if excess_returns.std() == 0:
            return sharpe_ratio, float("nan")
        sharpe_ratio_adj = excess_returns.mean() / excess_returns.std()

        return sharpe_ratio, sharpe_ratio_adj

    @staticmethod
    def calculate_sortino_ratio(backtest_results: pd.DataFrame) -> tuple[float, float]:
        """Calculates the Sortino Ratio using the stock's return as the benchmark."""
        if backtest_results.empty or "return" not in backtest_results or "close" not in backtest_results:
            raise ValueError("Backtest results must contain return and stock price data.")

        # Risk-free (0% baseline)
        strategy_returns = backtest_results["return"].dropna().pct_change()
        downside_returns = strategy_returns[strategy_returns < 0]
        if downside_returns.std() == 0:
            return float("nan"), float("nan")
        sortino_ratio = strategy_returns.mean() / downside_returns.std()

        # Adjusted using asset price as the baseline
        asset_returns = backtest_results["close"].dropna().pct_change()
        min_length = min(len(strategy_returns), len(asset_returns))
        strategy_returns = strategy_returns.iloc[:min_length]
        asset_returns = asset_returns.iloc[:min_length]
        excess_returns = strategy_returns - asset_returns
        downside_returns = excess_returns[excess_returns < 0]
        if downside_returns.std() == 0:
            return sortino_ratio, float("nan")
        sortino_ratio_adj = excess_returns.mean() / downside_returns.std()

        return sortino_ratio, sortino_ratio_adj

    @staticmethod
    def calculate_percent_return(backtest_results: pd.DataFrame) -> tuple[float, float]:
        """Calculates the annual percentage gain of the strategy and compares it to the stock's annual percentage gain."""
        if backtest_results.empty or "return" not in backtest_results or "close" not in backtest_results:
            raise ValueError("Backtest results must contain return and stock price data.")

        first_value = backtest_results.iloc[0]["close"]
        final_value = backtest_results.iloc[-1]["close"]
        strategy_final_value = backtest_results.iloc[-1]["return"]

        percent_return = (strategy_final_value - first_value) / first_value
        asset_return = (final_value - first_value) / first_value
        percent_return_adj = percent_return - asset_return

        return percent_return * 100, percent_return_adj * 100

    @staticmethod
    def charge_user(user, amount):
        funds_diff = user.monthly_credits - int(amount)
        if funds_diff > 0:
            user.monthly_credits = funds_diff
            user.save()
            return True, user.account_credits, user.monthly_credits
        elif user.monthly_credits + user.account_credits >= amount:
            user.monthly_credits = 0
            user.account_credits += funds_diff
            user.save()
            return True, user.account_credits, user.monthly_credits
        else:
            return False, None, None
