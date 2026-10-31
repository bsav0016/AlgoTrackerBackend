from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from api import leaderboard, leaderboard_job as job
from api.models import CustomUser, LeaderboardEntry, LeaderboardRun, LeaderboardSnapshot, Strategy, Trade
from api.tests.test_strategy import make_strategy, make_user

MONDAY = datetime(2026, 9, 7, 0, 30, tzinfo=timezone.utc)  # half past midnight, UTC
NEXT_MONDAY = MONDAY + timedelta(days=7)
D = Decimal


def member(name, display=None):
    user = make_user(username=name, email=f"{name}@example.com")
    user.leaderboard_name = display or name.title()
    user.save()
    return user


def live_strategy(user, strategy_return=1, asset_return=1, interval="1day", active=True, started=None):
    """A strategy that has run for 30 days before MONDAY and made three completed trades."""
    started = started or MONDAY - timedelta(days=30)
    strategy = make_strategy(user, started_at=started, strategy_return=D(strategy_return),
                             asset_return=D(asset_return), active=active)
    if interval != "1day":
        Strategy.objects.filter(pk=strategy.pk).update(interval=interval)
    day = started + timedelta(days=1)
    for _ in range(3):
        Trade.objects.create(strategy=strategy, price=10, is_buy=True, date=day)
        Trade.objects.create(strategy=strategy, price=11, is_buy=False, date=day + timedelta(hours=1))
        day += timedelta(days=2)
    return Strategy.objects.get(pk=strategy.pk)


def set_returns(strategy, strategy_return, asset_return):
    Strategy.objects.filter(pk=strategy.pk).update(strategy_return=D(strategy_return), asset_return=D(asset_return))


def week_board(group="daily_or_longer", week=MONDAY.date()):
    return list(LeaderboardEntry.objects.filter(kind="week", period_start=week, group=group).order_by("rank", "user__username"))


def all_time_board(group="daily_or_longer"):
    return list(LeaderboardEntry.objects.filter(kind="all_time", group=group).order_by("rank", "user__username"))


class WeekStartTests(TestCase):
    def test_the_fixture_date_is_a_monday(self):
        self.assertEqual(MONDAY.weekday(), 0)

    def test_the_week_starts_on_monday_utc(self):
        self.assertEqual(job.week_start(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)), MONDAY.date())
        self.assertEqual(job.week_start(datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc)), MONDAY.date())
        self.assertEqual(job.week_start(datetime(2026, 9, 6, 23, 59, tzinfo=timezone.utc)), (MONDAY - timedelta(days=7)).date())

    def test_a_time_in_another_zone_is_read_in_utc(self):
        eastern = timezone(timedelta(hours=-5))
        self.assertEqual(job.week_start(datetime(2026, 9, 6, 20, 0, tzinfo=eastern)), MONDAY.date())  # 01:00 Monday UTC


class SnapshotTests(TestCase):
    def test_active_strategies_of_members_are_recorded_and_nobody_else(self):
        mine = live_strategy(member("amy"))
        live_strategy(member("bob"), active=False)
        outsider = make_user(username="cat", email="cat@example.com")
        live_strategy(outsider)
        job.run(MONDAY)
        snapshots = LeaderboardSnapshot.objects.all()
        self.assertEqual([s.strategy_id for s in snapshots], [mine.pk])
        self.assertEqual(snapshots[0].period_start, MONDAY.date())

    def test_running_again_records_nothing_new(self):
        live_strategy(member("amy"))
        job.run(MONDAY)
        job.run(MONDAY + timedelta(hours=5))
        self.assertEqual(LeaderboardSnapshot.objects.count(), 1)

    def test_the_snapshot_keeps_the_returns_at_that_moment(self):
        strategy = live_strategy(member("amy"), "1.2", "1.1")
        job.run(MONDAY)
        set_returns(strategy, "1.5", "1.2")
        job.run(MONDAY + timedelta(hours=3))
        snapshot = LeaderboardSnapshot.objects.get()
        self.assertEqual((snapshot.strategy_return, snapshot.asset_return), (D("1.2"), D("1.1")))


class WeeklyBoardTests(TestCase):
    def run_two_mondays(self, between=None):
        job.run(MONDAY)
        if between:
            between()
        return job.run(NEXT_MONDAY)

    def test_the_score_is_the_weeks_return_over_the_assets(self):
        amy = live_strategy(member("amy"), "1.2", "1.1")
        bob = live_strategy(member("bob"), "1.0", "1.0")

        def move():
            set_returns(amy, "1.32", "1.21")  # +10% and +10% over the week: no better than holding
            set_returns(bob, "1.2", "1.0")    # +20% against flat

        self.run_two_mondays(move)
        board = week_board()
        self.assertEqual([(e.user.username, e.rank) for e in board], [("bob", 1), ("amy", 2)])
        self.assertEqual(board[0].score, D("1.2000"))
        self.assertEqual(board[1].score, D("1.0000"))
        self.assertEqual(board[0].strategy_return, D("1.2"))

    def test_the_first_run_has_no_week_to_publish(self):
        live_strategy(member("amy"))
        snapshots, weekly, _ = job.run(MONDAY)
        self.assertEqual(snapshots, 1)
        self.assertEqual(weekly, 0)
        self.assertEqual(week_board(week=(MONDAY - timedelta(days=7)).date()), [])

    def test_a_finished_week_is_published_once_and_never_changes(self):
        amy = live_strategy(member("amy"))
        self.run_two_mondays(lambda: set_returns(amy, "1.3", "1.0"))
        before = [(e.pk, e.score) for e in week_board()]
        runs = LeaderboardRun.objects.count()
        set_returns(amy, "9", "1")
        _, weekly, _ = job.run(NEXT_MONDAY + timedelta(hours=2))
        self.assertIsNone(weekly)
        self.assertEqual([(e.pk, e.score) for e in week_board()], before)
        self.assertEqual(LeaderboardRun.objects.count(), runs)

    def test_a_strategy_that_joined_during_the_week_waits_for_the_next_one(self):
        amy = live_strategy(member("amy"))
        job.run(MONDAY)
        late = live_strategy(member("dee"))  # joined after the snapshot
        set_returns(amy, "1.1", "1.0")
        set_returns(late, "1.4", "1.0")
        job.run(NEXT_MONDAY)
        self.assertEqual([e.user.username for e in week_board()], ["amy"])
        self.assertEqual({e.user.username for e in all_time_board()}, {"amy", "dee"})

    def test_a_strategy_that_stopped_being_eligible_is_left_out(self):
        amy = live_strategy(member("amy"))
        bob = live_strategy(member("bob"))
        job.run(MONDAY)
        Strategy.objects.filter(pk=bob.pk).update(active=False)
        set_returns(amy, "1.1", "1.0")
        job.run(NEXT_MONDAY)
        self.assertEqual([e.user.username for e in week_board()], ["amy"])

    def test_ties_share_a_rank(self):
        strategies = [live_strategy(member(n)) for n in ("amy", "bob", "cat", "dan")]
        job.run(MONDAY)
        for strategy, value in zip(strategies, ("1.3", "1.1", "1.3", "1.0")):
            set_returns(strategy, value, "1.0")
        job.run(NEXT_MONDAY)
        self.assertEqual([(e.user.username, e.rank) for e in week_board()],
                         [("amy", 1), ("cat", 1), ("bob", 3), ("dan", 4)])

    def test_a_person_appears_once_per_group_with_their_best_strategy(self):
        owner = member("amy")
        weak = live_strategy(owner)
        strong = live_strategy(owner)
        job.run(MONDAY)
        set_returns(weak, "1.05", "1.0")
        set_returns(strong, "1.25", "1.0")
        job.run(NEXT_MONDAY)
        board = week_board()
        self.assertEqual([(e.strategy_id, e.score) for e in board], [(strong.pk, D("1.2500"))])

    def test_groups_are_ranked_apart(self):
        daily = live_strategy(member("amy"))
        fast = live_strategy(member("bob"), interval="1min")
        job.run(MONDAY)
        set_returns(daily, "1.1", "1.0")
        set_returns(fast, "1.9", "1.0")
        job.run(NEXT_MONDAY)
        self.assertEqual([(e.user.username, e.rank) for e in week_board("daily_or_longer")], [("amy", 1)])
        self.assertEqual([(e.user.username, e.rank) for e in week_board("intraday")], [("bob", 1)])

    def test_a_publish_that_lost_a_race_does_nothing(self):
        live_strategy(member("amy"))
        job.run(MONDAY)
        LeaderboardRun.objects.create(kind="week", period_start=MONDAY.date())
        self.assertIsNone(job.publish_week(MONDAY.date(), NEXT_MONDAY.date(), NEXT_MONDAY))
        self.assertEqual(LeaderboardEntry.objects.filter(kind="week").count(), 0)


class AllTimeBoardTests(TestCase):
    def test_it_ranks_current_cumulative_results(self):
        live_strategy(member("amy"), "1.5", "1.0")
        live_strategy(member("bob"), "1.2", "1.0")
        job.run(MONDAY)
        self.assertEqual([(e.user.username, e.rank) for e in all_time_board()], [("amy", 1), ("bob", 2)])

    def test_each_run_replaces_the_board(self):
        amy = live_strategy(member("amy"), "1.5", "1.0")
        bob = live_strategy(member("bob"), "1.2", "1.0")
        job.run(MONDAY)
        set_returns(bob, "2.0", "1.0")
        Strategy.objects.filter(pk=amy.pk).update(active=False)
        job.run(MONDAY + timedelta(hours=1))
        self.assertEqual([e.user.username for e in all_time_board()], ["bob"])
        self.assertEqual(LeaderboardEntry.objects.filter(kind="all_time").count(), 1)

    def test_a_strategy_under_fourteen_days_old_is_not_ranked(self):
        live_strategy(member("amy"), started=MONDAY - timedelta(days=10))
        job.run(MONDAY)
        self.assertEqual(all_time_board(), [])

    def test_an_empty_board_is_fine(self):
        self.assertEqual(job.run(MONDAY), (0, 0, 0))


class LeavingTests(TestCase):
    def test_leaving_removes_the_users_entries_and_snapshots_everywhere(self):
        amy = member("amy")
        bob = member("bob")
        strategy = live_strategy(amy)
        live_strategy(bob)
        job.run(MONDAY)
        set_returns(strategy, "1.2", "1.0")
        job.run(NEXT_MONDAY)
        self.assertTrue(LeaderboardEntry.objects.filter(user=amy).exists())

        leaderboard.leave(amy)

        self.assertFalse(LeaderboardEntry.objects.filter(user=amy).exists())
        self.assertFalse(LeaderboardSnapshot.objects.filter(user=amy).exists())
        self.assertIsNone(CustomUser.objects.get(pk=amy.pk).leaderboard_name)
        self.assertTrue(LeaderboardEntry.objects.filter(user=bob).exists())
        self.assertTrue(LeaderboardSnapshot.objects.filter(user=bob).exists())

    def test_rejoining_starts_from_nothing(self):
        amy = member("amy")
        live_strategy(amy)
        job.run(MONDAY)
        leaderboard.leave(amy)
        leaderboard.join(amy, "Amy Again")
        job.run(NEXT_MONDAY)
        self.assertEqual(week_board(), [])  # no snapshot from the previous Monday
        self.assertEqual([e.user.username for e in all_time_board()], ["amy"])

    def test_leaving_through_the_api_clears_everything_too(self):
        amy = member("amy")
        live_strategy(amy)
        job.run(MONDAY)
        client = APIClient()
        client.force_authenticate(amy)
        self.assertEqual(client.delete("/api/leaderboard/profile/").status_code, 204)
        self.assertEqual(LeaderboardEntry.objects.count(), 0)
        self.assertEqual(LeaderboardSnapshot.objects.count(), 0)

    def test_deleting_a_strategy_removes_its_rows(self):
        strategy = live_strategy(member("amy"))
        job.run(MONDAY)
        strategy.delete()
        self.assertEqual(LeaderboardEntry.objects.count(), 0)
        self.assertEqual(LeaderboardSnapshot.objects.count(), 0)


class CommandTests(TestCase):
    def test_the_command_runs_one_pass_and_exits(self):
        live_strategy(member("amy"))
        call_command("leaderboard_rank", "--once", stdout=StringIO())
        self.assertEqual(LeaderboardSnapshot.objects.count(), 1)
