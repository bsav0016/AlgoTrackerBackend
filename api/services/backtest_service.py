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
from dataclasses import dataclass
from typing import Optional

TWELVE_DATA_API_KEY = settings.TWELVE_DATA_API_KEY
BACKTEST_ITERATIONS = settings.BACKTEST_ITERATIONS
TWELVE_DATA_TIMEOUT = settings.TWELVE_DATA_TIMEOUT
PAGE_SIZE = 5000  # candles TwelveData returns per request


class MarketDataUnavailable(Exception):
    """TwelveData could not be reached in time (timeout, connection error). Safe to try again later."""

matplotlib.use("Agg")


@dataclass
class DataWindow:
    """What a backtest actually covered, so the user is never shown numbers for a period they did not get."""
    requested_start: datetime.datetime
    requested_end: datetime.datetime
    covered_start: datetime.datetime
    covered_end: datetime.datetime
    candles: int
    requests: int                # paid market-data requests actually made
    truncated: bool              # the data does not reach the requested start
    reason: Optional[str]        # why: "request_limit" (we stopped asking) or "history_ends" (no older data exists)
    warm_up_candles: int         # candles before the start the indicators wanted
    warm_up_complete: bool

    def as_dict(self) -> dict:
        return {
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "covered_start": self.covered_start.isoformat(),
            "covered_end": self.covered_end.isoformat(),
            "candles": self.candles,
            "requests": self.requests,
            "truncated": self.truncated,
            "truncated_reason": self.reason,
            "warm_up_candles": self.warm_up_candles,
            "warm_up_complete": self.warm_up_complete,
        }


@dataclass
class FetchedHistory:
    frame: pd.DataFrame
    requests: int
    start_reached: bool
    warm_up_complete: bool
    stop_reason: Optional[str]


class NoDataForPeriod(Exception):
    """The market-data provider has no candles in the requested period."""


class BacktestService:
    @staticmethod
    def warm_up_candles(signals: List[Signal]) -> int:
        """Candles needed before the start date so every indicator has settled by the first candle shown.
        Indicators that smooth recursively (RSI, ADX, EMA, MACD) keep a memory of the early data, so they get
        several windows to settle; plain windows need just their length."""
        needed = 0
        for signal in signals:
            window = signal.window or 0
            if signal.name in ("SMA", "BBP", "SO"):
                needed = max(needed, window)
            elif signal.name in ("RSI", "EMA"):
                needed = max(needed, 4 * window)
            elif signal.name == "ADX":
                needed = max(needed, 6 * window)
            elif signal.name == "MACD":
                needed = max(needed, 4 * (signal.slow_window or 0) + (signal.signal_window or 0))
        return needed

    @staticmethod
    def conduct_backtest(
            symbol: str,
            interval: str,
            start_date: datetime,
            end_date: datetime,
            signals: List[Signal]
    ) -> tuple[pd.DataFrame, DataWindow]:
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=pytz.UTC)
        warm_up = BacktestService.warm_up_candles(signals)
        history = BacktestService.fetch_history(symbol, interval, start_date, end_date, warm_up)
        indicator_data = SignalService.calculate_indicators(history.frame, signals)
        indicator_data["datetime"] = pd.to_datetime(indicator_data["datetime"], errors="coerce")
        indicator_data = indicator_data[indicator_data["datetime"] >= start_date]
        if indicator_data.empty:
            raise NoDataForPeriod(f"No {symbol} {interval} prices from {start_date:%Y-%m-%d}")
        indicator_data = BacktestService.find_buys(indicator_data, signals)
        indicator_data = BacktestService.find_sells(indicator_data, signals)
        window = DataWindow(
            requested_start=start_date, requested_end=end_date,
            covered_start=indicator_data["datetime"].iloc[0].to_pydatetime(),
            covered_end=indicator_data["datetime"].iloc[-1].to_pydatetime(),
            candles=len(indicator_data), requests=history.requests,
            truncated=not history.start_reached,
            reason=None if history.start_reached else history.stop_reason,
            warm_up_candles=warm_up, warm_up_complete=history.warm_up_complete,
        )
        return BacktestService.calculate_returns(indicator_data), window

    @staticmethod
    def fetch_page(symbol: str, interval: str, end_date: datetime) -> pd.DataFrame:
        """One paid request: up to PAGE_SIZE candles ending on `end_date`, oldest first."""
        response = None
        try:
            response = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": symbol, "interval": interval, "outputsize": PAGE_SIZE,
                        "end_date": end_date.strftime("%Y-%m-%d"), "apikey": TWELVE_DATA_API_KEY},
                timeout=TWELVE_DATA_TIMEOUT)
        except requests.RequestException as e:
            # The message must not contain the URL: it carries the API key in its query string.
            raise MarketDataUnavailable(f"Market data request failed ({type(e).__name__})") from None

        if response.status_code != 200:
            raise Exception(f"API request failed with status {response.status_code}: {response.text}")
        data = response.json()
        if 'values' not in data:
            raise ValueError("Values not found in response")
        df = pd.DataFrame(data["values"], columns=["datetime", "close", "low", "high"])
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
        for column in ("close", "low", "high"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        return df[::-1].reset_index(drop=True)

    @staticmethod
    def fetch_history(
            symbol: str,
            interval: str,
            start_date: datetime,
            end_date: datetime,
            warm_up_candles: int = 0,
    ) -> FetchedHistory:
        """Pages backwards from `end_date` until `start_date` is covered with `warm_up_candles` candles before it,
        the provider has nothing older, or BACKTEST_ITERATIONS requests have been made. `requests` counts the
        requests actually made, which is what the user pays for."""
        frame = pd.DataFrame(columns=["datetime", "close", "low", "high"])
        made = 0
        cursor = end_date
        while True:
            if made >= BACKTEST_ITERATIONS:
                reason = "request_limit"
                break
            page = BacktestService.fetch_page(symbol, interval, cursor)
            made += 1
            if page.empty:
                reason = "history_ends"
                break
            previous_oldest = frame["datetime"].iloc[0] if not frame.empty else None
            frame = pd.concat([page, frame], ignore_index=True).drop_duplicates("datetime").sort_values("datetime", ignore_index=True)
            oldest = frame["datetime"].iloc[0]
            if oldest <= start_date and (frame["datetime"] < start_date).sum() >= warm_up_candles:
                reason = None
                break
            if len(page) < PAGE_SIZE or (previous_oldest is not None and oldest >= previous_oldest):
                reason = "history_ends"  # a short page, or no progress: there is nothing older to fetch
                break
            cursor = oldest

        start_reached = not frame.empty and frame["datetime"].iloc[0] <= start_date
        warm_up_complete = (not frame.empty) and bool((frame["datetime"] < start_date).sum() >= warm_up_candles)
        return FetchedHistory(frame, made, start_reached, warm_up_complete, reason)

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
        sell_signals = [signal for signal in signals if not signal.buy_signal]
        # With no sell signals a strategy never sells (it holds once bought); it must not sell on every row,
        # because a row that is both a buy and a sell counts as neither and the strategy could never buy.
        new_data["sell"] = bool(sell_signals)
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
