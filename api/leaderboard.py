"""Joining and leaving the public leaderboard.

A user is on the board only while `CustomUser.leaderboard_name` is set. It is a name made for the board, never the
username, first or last name or email, and it starts off empty, so nobody is public until they choose to be.
"""
import re

from django.db import IntegrityError, transaction

from api import leaderboard_read
from api.models import CustomUser

MIN_LENGTH = 3
MAX_LENGTH = 20
# ASCII letters, digits, spaces, "_" and "-", starting with a letter or digit. Limiting it to ASCII keeps out
# look-alike letters and invisible characters, and "@", "." and "/" keep out email addresses and web links.
ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")
# Names that could pass for the app or its staff, compared with spaces, "_" and "-" removed.
RESERVED = {"admin", "administrator", "moderator", "support", "staff", "official", "algotracker", "system", "you"}


class InvalidDisplayName(ValueError):
    """The name cannot be used. The message is safe to show to the user."""


def clean_display_name(raw, user):
    """The name to store for `user`, or raises `InvalidDisplayName`. Runs before anything is written."""
    if not isinstance(raw, str):
        raise InvalidDisplayName("Enter a display name")
    name = " ".join(raw.split())
    if not MIN_LENGTH <= len(name) <= MAX_LENGTH:
        raise InvalidDisplayName(f"Use {MIN_LENGTH} to {MAX_LENGTH} characters")
    if not ALLOWED.match(name):
        raise InvalidDisplayName("Use only letters, numbers, spaces, hyphens and underscores")
    squashed = re.sub(r"[ _-]", "", name).lower()
    if squashed in RESERVED:
        raise InvalidDisplayName("That name is not available")
    # Someone else's username would let one person pass for another.
    if CustomUser.objects.filter(username__iexact=name).exclude(pk=user.pk).exists():
        raise InvalidDisplayName("That name is not available")
    if CustomUser.objects.filter(leaderboard_name__iexact=name).exclude(pk=user.pk).exists():
        raise InvalidDisplayName("That name is taken")
    return name


def join(user, raw_name):
    """Put `user` on the board under `raw_name`, or change their name. Returns the stored name."""
    name = clean_display_name(raw_name, user)
    try:
        with transaction.atomic():
            CustomUser.objects.filter(pk=user.pk).update(leaderboard_name=name)
    except IntegrityError:  # someone took the name between the check and the write
        raise InvalidDisplayName("That name is taken")
    user.leaderboard_name = name
    leaderboard_read.bump_version()
    return name


def leave(user):
    """Take `user` off the board and delete everything it holds about them: entries and snapshots."""
    from api import leaderboard_job
    with transaction.atomic():
        CustomUser.objects.filter(pk=user.pk).update(leaderboard_name=None)
        leaderboard_job.remove_user(user.pk)
    user.leaderboard_name = None
    leaderboard_read.bump_version()
