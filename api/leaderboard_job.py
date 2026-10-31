"""The leaderboard ranking job: snapshots each week's starting point and writes the boards. Safe to run as often as
you like; every step does nothing if it has already been done.

How the boards are made:

* **Snapshots.** Every Monday 00:00 UTC (in practice, the first run after it) the job records the cumulative
  `strategy_return` and `asset_return` of every active strategy whose owner is on the board. Only people on the board
  are recorded, and leaving deletes their snapshots.
* **Weekly board.** Once a week has ended, its board is written one time: a strategy's weekly result is its
  return over that week divided by the asset's, taken from its snapshot at the start of the week and at the end. A
  strategy with no snapshot at the start (it, or its owner, joined during the week) has no weekly result and ranks
  from the next week. The week is then final.
* **All-time board.** Rewritten on every run from each strategy's current cumulative returns, which already
  cover only live running since `started_at`.

Eligibility, scoring, groups, one entry per person and ties all come from `api.leaderboard_rules`.
"""
import logging
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from api import leaderboard_read, leaderboard_rules as rules
from api.models import LeaderboardEntry, LeaderboardRun, LeaderboardSnapshot, Strategy

logger = logging.getLogger(__name__)


def week_start(moment):
    """The Monday (UTC) of the week `moment` falls in."""
    day = moment.astimezone(dt_timezone.utc).date()
    return day - timedelta(days=day.weekday())


def on_board_strategies():
    """Active strategies whose owner is on the board."""
    return Strategy.objects.filter(active=True, user__leaderboard_name__isnull=False).select_related("user")


def take_snapshots(boundary):
    """Record the starting point of the week beginning `boundary` for every strategy that has none yet."""
    have = set(LeaderboardSnapshot.objects.filter(period_start=boundary).values_list("strategy_id", flat=True))
    rows = [
        LeaderboardSnapshot(user_id=s.user_id, strategy=s, period_start=boundary,
                            strategy_return=s.strategy_return, asset_return=s.asset_return)
        for s in on_board_strategies() if s.pk not in have
    ]
    LeaderboardSnapshot.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def _write_board(kind, period_start, candidates):
    """Rank `candidates` (`[(strategy, score, strategy_return, asset_return)]`, already eligible) per group and
    store them. One entry per person per group, best first, ties sharing a rank."""
    entries = []
    for group, chosen in rules.pick_best(candidates).items():
        ranked = rules.rank_scores([(c, c[1]) for c in chosen])
        for (strategy, value, strategy_return, asset_return), _, rank in ranked:
            entries.append(LeaderboardEntry(
                kind=kind, period_start=period_start, group=group, rank=rank, score=value,
                user_id=strategy.user_id, strategy=strategy,
                strategy_return=strategy_return, asset_return=asset_return))
    LeaderboardEntry.objects.bulk_create(entries)
    return len(entries)


def publish_week(week, boundary, now):
    """Write the final board for the week that began on `week` and ended at `boundary`. Returns the number of
    entries, or None if that week was already published."""
    try:
        with transaction.atomic():
            LeaderboardRun.objects.create(kind=LeaderboardEntry.WEEK, period_start=week)
            started = {s.strategy_id: s for s in LeaderboardSnapshot.objects.filter(period_start=week)}
            candidates = []
            ended = LeaderboardSnapshot.objects.filter(period_start=boundary).select_related("strategy__user")
            for end in ended:
                begin = started.get(end.strategy_id)
                strategy = end.strategy
                if begin is None or begin.strategy_return <= 0 or begin.asset_return <= 0:
                    continue
                if rules.ineligible_reason(strategy, now) is not None:
                    continue
                strategy_return = Decimal(end.strategy_return) / Decimal(begin.strategy_return)
                asset_return = Decimal(end.asset_return) / Decimal(begin.asset_return)
                if asset_return <= 0:
                    continue
                value = (strategy_return / asset_return).quantize(rules.SCORE_PLACES)
                candidates.append((strategy, value, strategy_return, asset_return))
            return _write_board(LeaderboardEntry.WEEK, week, candidates)
    except IntegrityError:  # another run published it first
        return None


def refresh_all_time(now):
    """Replace the all-time board with one built from every strategy's current results."""
    candidates = []
    now = now or timezone.now()
    for strategy in on_board_strategies():
        if rules.ineligible_reason(strategy, now) is None:
            candidates.append((strategy, rules.score(strategy), strategy.strategy_return, strategy.asset_return))
    with transaction.atomic():
        LeaderboardEntry.objects.filter(kind=LeaderboardEntry.ALL_TIME).delete()
        return _write_board(LeaderboardEntry.ALL_TIME, None, candidates)


def run(now=None):
    """One pass. Returns `(snapshots taken, weekly entries written or None, all-time entries written)`."""
    now = now or timezone.now()
    boundary = week_start(now)
    snapshots = take_snapshots(boundary)
    weekly = publish_week(boundary - timedelta(days=7), boundary, now)
    all_time = refresh_all_time(now)
    leaderboard_read.bump_version()
    return snapshots, weekly, all_time


def remove_user(user_id):
    """Everything the board holds about a user: their entries and their snapshots."""
    LeaderboardEntry.objects.filter(user_id=user_id).delete()
    LeaderboardSnapshot.objects.filter(user_id=user_id).delete()

