"""Who may rank on the leaderboard, and how they are scored. The rules are written here once and the ranking job
and the read endpoints use these functions, so nothing about fairness is decided anywhere else.

The rules (also in the README):

* Only live results count, never backtests. A strategy's `strategy_return` and `asset_return` start at 1 on the day
  it begins running (`started_at`) and a draft starts when it is activated, so they already cover only that time.
* The score is the return over buy-and-hold: `strategy_return / asset_return`. Above 1 means the strategy beat
  simply holding the asset over the same period; 1.10 is 10% better.
* A strategy ranks only if it is active, its owner is on the board, it has run for at least 14 days, and it has
  made at least 3 completed trades (a buy followed by a sell).
* Strategies are ranked within a group, so a one-minute crypto strategy is not ranked against a daily stock one:
  `intraday` (candles shorter than a day) and `daily_or_longer`.
* Each person appears once per group, with their best strategy, so one account cannot fill a board.
* Equal scores share a rank (1, 2, 2, 4), compared to four decimal places.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

MIN_LIVE_DAYS = 14
MIN_COMPLETED_TRADES = 3
SCORE_PLACES = Decimal("0.0001")

INTRADAY = "intraday"
DAILY_OR_LONGER = "daily_or_longer"
GROUPS = (INTRADAY, DAILY_OR_LONGER)
SECONDS_PER_DAY = 86400


def group_for(interval):
    """The group a candle interval ranks in, or None for an interval this server does not know."""
    info = settings.INTERVAL_MAP.get(interval)
    if info is None:
        return None
    return DAILY_OR_LONGER if info["seconds"] >= SECONDS_PER_DAY else INTRADAY


def completed_trades(strategy):
    """Buys that have been closed by a sell, counted from when the strategy began running."""
    trades = strategy.trades.order_by("date", "pk")
    if strategy.started_at is not None:
        trades = trades.filter(date__gte=strategy.started_at)
    holding = False
    count = 0
    for trade in trades:
        if trade.is_buy:
            holding = True
        elif holding:
            holding = False
            count += 1
    return count


def ineligible_reason(strategy, now=None):
    """Why `strategy` cannot rank right now, as a short code, or None if it can."""
    now = now or timezone.now()
    if not strategy.active:
        return "not_active"
    if strategy.user.leaderboard_name is None:
        return "owner_not_on_board"
    if group_for(strategy.interval) is None:
        return "unknown_interval"
    if strategy.started_at is None:
        return "start_unknown"  # strategies from before this was recorded; their returns cannot be dated
    if now - strategy.started_at < timedelta(days=MIN_LIVE_DAYS):
        return "too_new"
    if strategy.asset_return is None or strategy.asset_return <= 0 or strategy.strategy_return is None:
        return "no_valid_return"
    if completed_trades(strategy) < MIN_COMPLETED_TRADES:
        return "too_few_trades"
    return None


def score(strategy):
    """Return over buy-and-hold, rounded to four places. Call only for an eligible strategy."""
    return (Decimal(strategy.strategy_return) / Decimal(strategy.asset_return)).quantize(SCORE_PLACES)


def rank_scores(rows):
    """Competition ranking. `rows` is a list of `(key, score)`; returns `[(key, score, rank)]` best first, where
    equal scores share a rank and the next rank skips the places taken (1, 2, 2, 4). Ties keep their input order."""
    ordered = sorted(rows, key=lambda row: row[1], reverse=True)
    ranked = []
    for position, (key, value) in enumerate(ordered, start=1):
        rank = ranked[-1][2] if ranked and ranked[-1][1] == value else position
        ranked.append((key, value, rank))
    return ranked


def pick_best(candidates):
    """One candidate per user per group. A candidate is a tuple that starts `(strategy, score, ...)` and is already
    known to be eligible. The highest score wins, then the older strategy (lower id) on a tie.
    Returns `{group: [candidate, ...]}`, with both groups always present."""
    best = {}
    for candidate in candidates:
        strategy, value = candidate[0], candidate[1]
        key = (group_for(strategy.interval), strategy.user_id)
        current = best.get(key)
        if current is None or (value, -strategy.pk) > (current[1], -current[0].pk):
            best[key] = candidate
    grouped = {group: [] for group in GROUPS}
    for (group, _), candidate in best.items():
        grouped[group].append(candidate)
    return grouped


def best_per_user(strategies):
    """`pick_best` for strategies scored on their current returns. Returns `{group: [(strategy, score)]}` for the
    strategies that are eligible."""
    now = timezone.now()
    eligible = [(s, score(s)) for s in strategies if ineligible_reason(s, now) is None]
    return pick_best(eligible)
