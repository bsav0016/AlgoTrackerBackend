from dataclasses import dataclass
from typing import Optional

# Indicator definitions. 1 is the original RSI, ADX and MACD value, kept so strategies saved before the standard
# definitions keep behaving as they did; 2 is the standard (Wilder) RSI and ADX and a sign-correct MACD value.
# Signals stored without a version are version 1; new signals default to the current one.
LEGACY_VERSION = 1
CURRENT_VERSION = 2


@dataclass
class Signal:
    name: str
    above_target: bool
    buy_signal: bool
    target_value: float
    window: Optional[int] = None
    fast_window: Optional[int] = None
    slow_window: Optional[int] = None
    signal_window: Optional[int] = None
    version: int = CURRENT_VERSION
    # A rule normally compares its indicator with `target_value`. With `compare_to` it compares it with a second
    # indicator instead, for example {"name": "SMA_PRICE", "window": 50}, and `target_value` is ignored. The keys are
    # the indicator fields of a Signal (name, window, fast_window, slow_window, signal_window). Signals stored before
    # this existed have none, so they behave exactly as before.
    compare_to: Optional[dict] = None


COMPARE_TO_KEYS = ("name", "window", "fast_window", "slow_window", "signal_window")


def comparison_signal(signal: Signal) -> Optional[Signal]:
    """The indicator a comparison rule is compared with, as a Signal that can be calculated like any other (None for a
    rule that compares with a typed number)."""
    if not signal.compare_to:
        return None
    fields = {key: signal.compare_to.get(key) for key in COMPARE_TO_KEYS}
    return Signal(above_target=signal.above_target, buy_signal=signal.buy_signal, target_value=0,
                  version=signal.version, **fields)


def indicator_signals(signals):
    """Every indicator the rules need calculated: each rule's own, and the one it is compared with."""
    needed = []
    for signal in signals:
        needed.append(signal)
        other = comparison_signal(signal)
        if other is not None:
            needed.append(other)
    return needed
