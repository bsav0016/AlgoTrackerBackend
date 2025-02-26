from dataclasses import dataclass
from typing import Optional


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
