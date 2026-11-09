import datetime
import math

import pandas as pd
import pytz
from django.core.management.base import BaseCommand, CommandError

from api.indicators.adx import calculate_adx
from api.indicators.macd import calculate_macd
from api.indicators.rsi import calculate_rsi
from api.indicators.signal import Signal
from api.services.backtest_service import BacktestService
from api.tests.reference_indicators import macd_lines, wilder_adx, wilder_rsi
from api.tests.test_indicator_reference import synthetic_ohlc


class Command(BaseCommand):
    help = (
        "Compare the app's RSI, ADX and MACD with the standard (Wilder) definitions on real price history, and show "
        "the candles per year used to annualise Sharpe and Sortino. Uses TwelveData credits (about one request per "
        "5000 candles). Pass --synthetic to run on made-up prices with no network."
    )

    def add_arguments(self, parser):
        parser.add_argument("symbol", nargs="?", help="A symbol from SYMBOLS, e.g. BTC/USD")
        parser.add_argument("interval", nargs="?", help="An interval from INTERVALS, e.g. 1day")
        parser.add_argument("--days", type=int, default=365, help="How far back to fetch (default 365)")
        parser.add_argument("--window", type=int, default=14, help="RSI and ADX window (default 14)")
        parser.add_argument("--synthetic", action="store_true", help="Use made-up prices instead of TwelveData")

    def handle(self, *args, **options):
        from django.conf import settings
        n = options["window"]
        if options["synthetic"]:
            close, high, low = synthetic_ohlc(500)
            label = "synthetic prices"
            symbol = interval = None
        else:
            symbol, interval = options["symbol"], options["interval"]
            if symbol not in settings.SYMBOLS or interval not in settings.INTERVAL_MAP:
                raise CommandError("Give a symbol and interval that are in SYMBOLS and INTERVALS (or use --synthetic).")
            end = datetime.datetime.now(pytz.UTC)
            data, _ = BacktestService.get_data(symbol, interval, end - datetime.timedelta(days=options["days"]), end)
            data = data.dropna()
            if len(data) < 5 * n:
                raise CommandError(f"Only {len(data)} candles came back; need at least {5 * n}.")
            close, high, low = list(data["close"]), list(data["high"]), list(data["low"])
            label = f"{symbol} {interval}, {len(close)} candles from {data['datetime'].iloc[0]} to {data['datetime'].iloc[-1]}"

        frame = pd.DataFrame({"close": close, "high": high, "low": low})
        out = self.stdout.write
        out(f"Indicator report on {label}, window {n}\n")

        def signal(name, **kw):
            return Signal(name=name, above_target=True, buy_signal=True, target_value=0, **kw)

        app_rsi = calculate_rsi(frame, signal("RSI", window=n))[f"RSI_{n}"]
        std_rsi = wilder_rsi(close, n)
        pairs = [(app_rsi.iloc[i], v) for i, v in enumerate(std_rsi) if v is not None and not math.isnan(app_rsi.iloc[i])]
        diffs = [abs(a - b) for a, b in pairs]
        out(f"RSI: mean absolute difference {sum(diffs) / len(diffs):.2f} points, largest {max(diffs):.2f}")
        for level in (30, 70):
            flag = (lambda a, b: (a < level) != (b < level)) if level == 30 else (lambda a, b: (a > level) != (b > level))
            wrong = sum(flag(a, b) for a, b in pairs)
            out(f"RSI {'below' if level == 30 else 'above'} {level}: disagrees with the standard on "
                f"{100 * wrong / len(pairs):.1f}% of {len(pairs)} candles")

        app_adx = calculate_adx(frame, signal("ADX", window=n))[f"ADX_{n}"]
        std_adx = wilder_adx(high, low, close, n)
        pairs = [(app_adx.iloc[i], v) for i, v in enumerate(std_adx) if v is not None and not math.isnan(app_adx.iloc[i])]
        diffs = [abs(a - b) for a, b in pairs]
        out(f"ADX: mean absolute difference {sum(diffs) / len(diffs):.2f} points, largest {max(diffs):.2f} over {len(pairs)} candles")

        fast, slow, sig = 12, 26, 9
        line, signal_line = macd_lines(close, fast, slow, sig)
        app_macd = calculate_macd(frame, signal("MACD", fast_window=fast, slow_window=slow, signal_window=sig))[f"MACD_{fast}_{slow}_{sig}"]
        usable = range(slow, len(close))
        bullish = [i for i in usable if line[i] > signal_line[i]]
        wrong = sum(app_macd.iloc[i] <= 0 for i in bullish) + sum(app_macd.iloc[i] > 0 for i in usable if i not in set(bullish))
        out(f"MACD: the value's sign disagrees with 'MACD line above its signal line' on {100 * wrong / len(usable):.1f}% of {len(usable)} candles")

        if symbol:
            hours = settings.SYMBOLS[symbol]
            seconds = settings.INTERVAL_MAP[interval]["seconds"]
            periods = BacktestService.periods_per_year(hours, seconds)
            out(f"Annualisation: SYMBOLS setting {hours:g} (trading hours per year) and {seconds}s candles "
                f"give {periods:,.1f} candles a year; Sharpe and Sortino are multiplied by {math.sqrt(periods):.2f}")
