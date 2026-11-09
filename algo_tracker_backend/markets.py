"""Market settings: when a symbol trades, which decides how many candles a year holds.

Sharpe and Sortino are scaled by the square root of the candles in a year, so each symbol needs to say how
often it trades. In the SYMBOLS setting the part after the colon is one of:

- a name: `always_open` (trades 24/7, like crypto) or `us_equity` (252 sessions of 6.5 hours);
- `<sessions>d<hours>h` for anything else, e.g. `252d6.5h`, `365d24h`;
- a bare number, the old format: trading hours per year (8760 for 24/7). Still read, with a warning.
"""
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60
CALENDAR_DAYS = 365


@dataclass(frozen=True)
class Market:
    name: str
    sessions_per_year: float
    session_hours: float
    legacy_hours_per_year: float | None = None

    def periods_per_year(self, interval_seconds: int) -> float:
        """Candles in a year for candles `interval_seconds` long."""
        if self.legacy_hours_per_year is not None:
            return self.legacy_hours_per_year * 3600 / interval_seconds
        if interval_seconds < DAY_SECONDS:
            return self.sessions_per_year * self.session_hours * 3600 / interval_seconds
        if interval_seconds == DAY_SECONDS:
            return self.sessions_per_year  # a daily candle is one session
        return CALENDAR_DAYS * DAY_SECONDS / interval_seconds  # weekly, monthly: calendar time


PRESETS = {
    "always_open": Market("always_open", 365, 24),
    "us_equity": Market("us_equity", 252, 6.5),
}
_EXPLICIT = re.compile(r"^(\d+(?:\.\d+)?)d(\d+(?:\.\d+)?)h$")


def parse_market(symbol: str, value: str) -> Market:
    value = value.strip()
    if value in PRESETS:
        return PRESETS[value]
    explicit = _EXPLICIT.match(value)
    if explicit:
        sessions, hours = float(explicit.group(1)), float(explicit.group(2))
        if not (0 < sessions <= 366 and 0 < hours <= 24):
            raise ValueError(f"SYMBOLS {symbol}: {value!r} needs 1-366 sessions and 0-24 hours")
        return Market(value, sessions, hours)
    try:
        hours_per_year = float(value)
    except ValueError:
        raise ValueError(
            f"SYMBOLS {symbol}: {value!r} is not a market. Use {', '.join(PRESETS)} or <sessions>d<hours>h such as 252d6.5h"
        ) from None
    if hours_per_year <= 0:
        raise ValueError(f"SYMBOLS {symbol}: {value!r} must be above 0")
    logger.warning(
        "SYMBOLS %s:%s uses the old trading-hours-per-year number. Use a market name such as %s instead; "
        "see .env.example.", symbol, value, " or ".join(PRESETS))
    return Market(f"{hours_per_year:g} hours a year", 0, 0, legacy_hours_per_year=hours_per_year)
