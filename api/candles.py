"""Builds OHLC candles from the TwelveData price ticks (which carry only a last price)."""


def bucket_start(timestamp: int, seconds: int) -> int:
    """Open time of the candle that `timestamp` falls in, for candles `seconds` long."""
    return timestamp - timestamp % seconds


def new_candle(bucket: int, price: float) -> dict:
    return {"timestamp": bucket, "open": price, "high": price, "low": price, "close": price}


def merge_tick(candle: dict, price: float) -> dict:
    """The candle after one more tick inside its bucket: high/low widen and the close is the latest price."""
    return {
        **candle,
        "high": max(candle["high"], price),
        "low": min(candle["low"], price),
        "close": price,
    }
