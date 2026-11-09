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
