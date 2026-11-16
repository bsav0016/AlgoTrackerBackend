"""Exit rules: leave a position because of how the price has moved since entry, not because an indicator crossed.

One function, `advance`, decides what happens on each candle. The backtest and the live job both call it, so they
cannot drift apart. Rules are judged on each candle's close (a close-only rule never reacts to a spike that
retraced inside the candle), and the exit happens at that close, the same price a signal exit would use.

Rules are percentages: {"stop_loss": 5} sells when the close is 5% below the entry price, {"take_profit": 10}
when it is 10% above, {"trailing_stop": 4} when it is 4% below the highest close since entry. A rule that is
left out, null or zero is off. If more than one fires on the same candle, they are reported in that order.
"""
from dataclasses import dataclass, replace
from typing import Optional

STOP_LOSS = "stop_loss"
TAKE_PROFIT = "take_profit"
TRAILING_STOP = "trailing_stop"
RULE_NAMES = (STOP_LOSS, TRAILING_STOP, TAKE_PROFIT)  # the order they win when several fire at once
LIMITS = {STOP_LOSS: (0.01, 99.99), TRAILING_STOP: (0.01, 99.99), TAKE_PROFIT: (0.01, 1000.0)}
LABELS = {STOP_LOSS: "stop-loss", TAKE_PROFIT: "take-profit", TRAILING_STOP: "trailing stop"}

BUY = "buy"
SELL = "sell"  # a sell signal, as opposed to one of the exit rules above


def clean_rules(raw) -> dict:
    """The rules that are on, as {name: percent}. Unknown names and out-of-range values raise ValueError."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("exit_rules must be an object")
    unknown = set(raw) - set(RULE_NAMES)
    if unknown:
        raise ValueError(f"Unknown exit rule: {sorted(unknown)[0]}")
    rules = {}
    for name, value in raw.items():
        if value is None or value == 0:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        low, high = LIMITS[name]
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high} percent")
        rules[name] = float(value)
    return rules


def describe_rules(rules) -> list:
    """Short phrases for cards and share pages, e.g. ["Stop-loss 5%", "Take-profit 10%"]."""
    rules = rules or {}
    return [f"{LABELS[name].capitalize()} {rules[name]:g}%" for name in (STOP_LOSS, TRAILING_STOP, TAKE_PROFIT)
            if rules.get(name)]


@dataclass(frozen=True)
class PositionState:
    owned: bool = False
    entry_price: Optional[float] = None
    peak_price: Optional[float] = None  # highest close since entry, what a trailing stop trails
    # After a rule sells while the buy signals still hold, wait for them to stop holding before buying again.
    # Otherwise the strategy would buy straight back in on the next candle, at a worse price.
    blocked: bool = False


def _level(price: float, percent: float, direction: int) -> float:
    """The price `percent` above (direction 1) or below (-1) `price`. Rounded so a close that lands exactly on the
    level counts: 100 * 1.10 is 110.00000000000001 in floating point, which would miss a close of exactly 110."""
    return round(price * (1 + direction * percent / 100), 8)


def exit_reason(rules, state: PositionState, close: float) -> Optional[str]:
    if not rules or not state.owned or state.entry_price is None:
        return None
    if rules.get(STOP_LOSS) and close <= _level(state.entry_price, rules[STOP_LOSS], -1):
        return STOP_LOSS
    if rules.get(TRAILING_STOP) and state.peak_price is not None \
            and close <= _level(state.peak_price, rules[TRAILING_STOP], -1):
        return TRAILING_STOP
    if rules.get(TAKE_PROFIT) and close >= _level(state.entry_price, rules[TAKE_PROFIT], 1):
        return TAKE_PROFIT
    return None


def advance(state: PositionState, buy: bool, sell: bool, close: float, rules=None):
    """The state after one candle and what happened on it: None, BUY, SELL, or the name of the rule that fired.

    A candle that is both a buy and a sell signal counts as neither, as it always has."""
    if state.owned:
        entry = state.entry_price if state.entry_price is not None else close
        peak = max(state.peak_price if state.peak_price is not None else close, close)
        state = replace(state, entry_price=entry, peak_price=peak)
        reason = exit_reason(rules, state, close)
        if reason:
            return PositionState(blocked=bool(buy)), reason
        if sell and not buy:
            return PositionState(), SELL
        return state, None
    if state.blocked:
        return (replace(state, blocked=False) if not buy else state), None
    if buy and not sell:
        return PositionState(owned=True, entry_price=close, peak_price=close), BUY
    return state, None
