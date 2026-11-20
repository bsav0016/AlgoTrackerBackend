"""Plain-language numbers for a backtest, worked out from the rows it already produced (`close`, `return`, `held`).

Sharpe and Sortino are the headline numbers today, and they mean little to most people. These say the same story in
words anyone can read: how far the strategy fell at its worst, how many of its trades made money, and how much of the
time it was invested at all.

All percentages are after trading costs, the same as `period_return`. A figure that cannot be worked out (no closed
trade, too few candles) is None, never a made-up zero.
"""
import math

import pandas as pd

KEYS = (
    "max_drawdown", "asset_max_drawdown", "closed_trades", "winning_trades", "win_rate", "average_trade",
    "best_trade", "worst_trade", "time_invested", "open_trade",
)


def _finite(value):
    """A float, or None for NaN and infinity (JSON cannot carry them)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def max_drawdown(values) -> float:
    """The biggest fall from a high to a later low, as a percent (zero or negative). A series that never falls is 0."""
    peak, worst = None, 0.0
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak:
            worst = min(worst, value / peak - 1)
    return worst * 100


def closed_trades(held, value) -> list:
    """The percent gain of each trade that was closed, in order.

    A trade starts on the candle where the position goes from out to in and ends on the candle where it goes back
    out. Its gain is the strategy's value on the closing candle over its value on the candle before it opened, so
    the cost of getting in and out is included. A position still open on the last candle is not a closed trade."""
    gains, entry = [], None
    for i in range(1, len(held)):
        if held[i] and not held[i - 1]:
            entry = i
        elif not held[i] and held[i - 1] and entry is not None:
            before = value[entry - 1]
            gains.append((value[i] / before - 1) * 100 if before else 0.0)
            entry = None
    return gains


def summarize(result: pd.DataFrame) -> dict:
    """The plain-language numbers for a backtest result (the frame from `BacktestService.calculate_returns`)."""
    empty = {key: None for key in KEYS}
    if result is None or len(result) < 2 or not {"close", "return", "held"}.issubset(result.columns):
        return empty

    held = [bool(h) for h in result["held"]]
    value = [float(v) for v in result["return"]]
    gains = closed_trades(held, value)
    wins = [g for g in gains if g > 0]

    return {
        "max_drawdown": _finite(max_drawdown(value)),
        "asset_max_drawdown": _finite(max_drawdown([float(c) for c in result["close"]])),
        "closed_trades": len(gains),
        "winning_trades": len(wins),
        "win_rate": _finite(len(wins) / len(gains) * 100) if gains else None,
        "average_trade": _finite(sum(gains) / len(gains)) if gains else None,
        "best_trade": _finite(max(gains)) if gains else None,
        "worst_trade": _finite(min(gains)) if gains else None,
        "time_invested": _finite(sum(held[1:]) / (len(held) - 1) * 100),
        "open_trade": held[-1],
    }
