"""Reading the leaderboard: the public part of a board (cached) and the caller's own place on it.

What a board shows about a person is only their board name, rank, score and the period's returns, never an id,
username, email or the strategy's rules (rules stay private unless the owner shares the strategy).

The cached part is the same for everybody, so it holds no user ids. It is cached under a version token that changes
whenever anyone joins, renames or leaves and whenever the job writes, so a person who leaves the board disappears
from the next request instead of lingering until a timer runs out.
"""
import logging
import uuid
from datetime import date

from django.core.cache import caches

from api import leaderboard_rules as rules
from api.models import LeaderboardEntry, LeaderboardRun

logger = logging.getLogger(__name__)

VERSION_KEY = "leaderboard:version"
CACHE_SECONDS = 300
DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _cache():
    return caches["default"]


def bump_version():
    """Make every cached board stale. Never raises: with no cache the boards are simply built each time."""
    try:
        _cache().set(VERSION_KEY, uuid.uuid4().hex, None)
    except Exception:
        logger.exception("Could not clear the cached leaderboards")


def _version():
    try:
        version = _cache().get(VERSION_KEY)
        if version is None:
            version = uuid.uuid4().hex
            _cache().add(VERSION_KEY, version, None)
            version = _cache().get(VERSION_KEY) or version
        return version
    except Exception:
        return None  # no cache: do not cache


def latest_week():
    """The Monday of the most recently published week, or None before the first one."""
    return (LeaderboardRun.objects.filter(kind=LeaderboardEntry.WEEK).order_by("-period_start")
            .values_list("period_start", flat=True).first())


def published_weeks(limit=12):
    return list(LeaderboardRun.objects.filter(kind=LeaderboardEntry.WEEK).order_by("-period_start")
                .values_list("period_start", flat=True)[:limit])


def _entries(kind, period_start, group):
    return LeaderboardEntry.objects.filter(
        kind=kind, period_start=period_start, group=group, user__leaderboard_name__isnull=False)


def _build(kind, period_start, group, limit):
    queryset = _entries(kind, period_start, group)
    rows = queryset.select_related("user").order_by("rank", "pk")[:limit]
    return {
        "kind": kind,
        "group": group,
        "period_start": period_start.isoformat() if period_start else None,
        "total": queryset.count(),
        "entries": [
            {"rank": e.rank, "display_name": e.user.leaderboard_name, "score": float(e.score),
             "strategy_return": float(e.strategy_return), "asset_return": float(e.asset_return)}
            for e in rows
        ],
    }


def board(kind, period_start, group, limit):
    """The public part of one board, from the cache when it is current."""
    version = _version()
    key = f"leaderboard:{version}:{kind}:{period_start}:{group}:{limit}"
    if version is not None:
        try:
            cached = _cache().get(key)
            if cached is not None:
                return cached
        except Exception:
            logger.exception("Could not read the cached leaderboard")
    data = _build(kind, period_start, group, limit)
    if version is not None:
        try:
            _cache().set(key, data, CACHE_SECONDS)
        except Exception:
            logger.exception("Could not cache the leaderboard")
    return data


def my_place(user, kind, period_start, group):
    """The caller's entry on this board, or None."""
    entry = _entries(kind, period_start, group).filter(user=user).first()
    if entry is None:
        return None
    return {"rank": entry.rank, "score": float(entry.score), "strategy_return": float(entry.strategy_return),
            "asset_return": float(entry.asset_return)}


def with_viewer(data, user):
    """Adds what depends on who is asking, without touching the cached copy: which row is theirs, and their place."""
    mine = (user.leaderboard_name or "").lower()
    entries = [{**e, "is_me": bool(mine) and e["display_name"].lower() == mine} for e in data["entries"]]
    period = date.fromisoformat(data["period_start"]) if data["period_start"] else None
    return {**data, "entries": entries, "on_board": user.leaderboard_name is not None,
            "me": my_place(user, data["kind"], period, data["group"]) if user.leaderboard_name else None}


GROUPS = rules.GROUPS
