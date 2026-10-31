from datetime import timedelta

from django.core.cache import caches
from django.core.cache.backends.base import BaseCache
from django.core.cache.backends.locmem import LocMemCache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from api import leaderboard, leaderboard_job as job
from api.models import CustomUser, LeaderboardEntry
from api.tests.test_leaderboard_job import MONDAY, NEXT_MONDAY, live_strategy, member, set_returns
from api.tests.test_strategy import make_user

URL = "/api/leaderboard/"
LOCMEM = {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "leaderboard-test"}
# Same as test_settings, so the rate limits (whose cache is chosen when the module loads) share one store with it.
THROTTLE = {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
ENTRY_FIELDS = {"rank", "display_name", "score", "strategy_return", "asset_return", "is_me"}


class BrokenCache(BaseCache):
    """A cache whose every call fails, like an unreachable Redis."""

    def _fail(self, *args, **kwargs):
        raise ConnectionError("cache is down")

    add = get = set = delete = incr = has_key = touch = clear = _fail


@override_settings(CACHES={"default": LOCMEM, "throttle": THROTTLE})
class BoardAPITestCase(APITestCase):
    def setUp(self):
        LocMemCache("leaderboard-test", {}).clear()
        caches["throttle"].clear()
        self.addCleanup(caches["throttle"].clear)
        self.amy = member("amy_login", "Amy Top")
        self.bob = member("bob_login", "Bob Mid")
        self.cat = member("cat_login", "Cat Low")
        self.amy_strategy = live_strategy(self.amy, "1.5", "1.0")
        self.bob_strategy = live_strategy(self.bob, "1.2", "1.0")
        self.cat_strategy = live_strategy(self.cat, "1.1", "1.0")
        job.run(MONDAY)
        self.client.force_authenticate(self.bob)

    def get(self, **params):
        return self.client.get(URL, params)


class ReadTests(BoardAPITestCase):
    def test_signing_in_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.get().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_default_is_the_all_time_board_for_daily_strategies(self):
        data = self.get().data
        self.assertEqual((data["kind"], data["group"], data["period_start"]), ("all_time", "daily_or_longer", None))
        self.assertEqual([(e["rank"], e["display_name"]) for e in data["entries"]],
                         [(1, "Amy Top"), (2, "Bob Mid"), (3, "Cat Low")])
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["entries"][0]["score"], 1.5)

    def test_entries_show_only_the_listed_fields(self):
        for entry in self.get().data["entries"]:
            self.assertEqual(set(entry), ENTRY_FIELDS)

    def test_nothing_that_identifies_a_person_or_their_rules_is_in_the_response(self):
        body = self.get().content.decode().lower()
        for secret in ("amy_login", "bob_login", "cat_login", "@example.com", "buy_signals", "sell_signals", "rsi", "sma"):
            self.assertNotIn(secret, body)
        self.assertNotIn('"user"', body)
        self.assertNotIn('"strategy"', body)

    def test_the_caller_is_marked_and_has_a_place(self):
        data = self.get().data
        self.assertEqual([e["is_me"] for e in data["entries"]], [False, True, False])
        self.assertTrue(data["on_board"])
        self.assertEqual(data["me"]["rank"], 2)
        self.assertEqual(data["me"]["score"], 1.2)

    def test_the_callers_place_is_shown_even_below_the_top(self):
        self.client.force_authenticate(self.cat)
        data = self.get(limit=1).data
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["me"]["rank"], 3)
        self.assertFalse(data["entries"][0]["is_me"])

    def test_someone_not_on_the_board_sees_it_but_has_no_place(self):
        outsider = make_user(username="dan_login", email="dan@example.com")
        self.client.force_authenticate(outsider)
        data = self.get().data
        self.assertEqual(len(data["entries"]), 3)
        self.assertFalse(data["on_board"])
        self.assertIsNone(data["me"])
        self.assertFalse(any(e["is_me"] for e in data["entries"]))

    def test_groups_are_separate(self):
        live_strategy(member("eve_login", "Eve Fast"), "1.3", "1.0", interval="1min")
        job.run(MONDAY + timedelta(hours=1))
        fast = self.get(group="intraday").data
        self.assertEqual([e["display_name"] for e in fast["entries"]], ["Eve Fast"])
        self.assertEqual(len(self.get(group="daily_or_longer").data["entries"]), 3)

    def test_ties_show_a_shared_rank(self):
        set_returns(self.cat_strategy, "1.2", "1.0")
        job.run(MONDAY + timedelta(hours=1))
        ranks = [(e["display_name"], e["rank"]) for e in self.get().data["entries"]]
        self.assertEqual(ranks, [("Amy Top", 1), ("Bob Mid", 2), ("Cat Low", 2)])

    def test_a_left_user_whose_row_somehow_survived_is_still_hidden(self):
        CustomUser.objects.filter(pk=self.amy.pk).update(leaderboard_name=None)
        self.assertEqual([e["display_name"] for e in self.get().data["entries"]], ["Bob Mid", "Cat Low"])


class WeekTests(BoardAPITestCase):
    def setUp(self):
        super().setUp()
        for strategy, value in ((self.amy_strategy, "1.8"), (self.bob_strategy, "1.32"), (self.cat_strategy, "1.21")):
            set_returns(strategy, value, "1.0")
        job.run(NEXT_MONDAY)

    def test_the_latest_finished_week_is_the_default(self):
        data = self.get(kind="week").data
        self.assertEqual(data["period_start"], MONDAY.date().isoformat())
        self.assertEqual([(e["display_name"], e["rank"]) for e in data["entries"]],
                         [("Amy Top", 1), ("Bob Mid", 2), ("Cat Low", 2)])
        self.assertAlmostEqual(data["entries"][0]["score"], 1.2, places=3)  # 1.8 / 1.5 against a flat asset

    def test_the_published_weeks_are_listed_newest_first(self):
        weeks = self.get(kind="week").data["weeks"]
        self.assertEqual(weeks[0], MONDAY.date().isoformat())
        self.assertEqual(weeks, sorted(weeks, reverse=True))

    def test_a_week_can_be_picked_and_an_unpublished_one_is_empty(self):
        self.assertEqual(len(self.get(kind="week", week=MONDAY.date().isoformat()).data["entries"]), 3)
        empty = self.get(kind="week", week="2020-01-06").data
        self.assertEqual(empty["entries"], [])
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["me"])

    def test_before_any_week_is_published_the_board_is_empty(self):
        LeaderboardEntry.objects.filter(kind="week").delete()
        from api.models import LeaderboardRun
        LeaderboardRun.objects.all().delete()
        data = self.get(kind="week").data
        self.assertEqual((data["period_start"], data["entries"], data["weeks"]), (None, [], []))


class ValidationTests(BoardAPITestCase):
    def test_bad_parameters_are_refused(self):
        for params in ({"kind": "month"}, {"group": "crypto"}, {"limit": "many"}, {"kind": "week", "week": "soon"}):
            self.assertEqual(self.get(**params).status_code, status.HTTP_400_BAD_REQUEST, params)

    def test_the_limit_is_kept_between_one_and_a_hundred(self):
        self.assertEqual(self.get(limit=0).data["entries"].__len__(), 1)
        self.assertEqual(self.get(limit=-5).data["entries"].__len__(), 1)
        self.assertEqual(len(self.get(limit=1000).data["entries"]), 3)


class FreshnessTests(BoardAPITestCase):
    def test_the_board_is_cached_between_requests(self):
        self.get()
        LeaderboardEntry.objects.all().delete()  # behind the cache's back
        self.assertEqual(len(self.get().data["entries"]), 3)

    def test_the_cache_does_not_leak_one_viewers_place_to_another(self):
        self.get()
        self.client.force_authenticate(self.cat)
        data = self.get().data
        self.assertEqual([e["is_me"] for e in data["entries"]], [False, False, True])
        self.assertEqual(data["me"]["rank"], 3)

    def test_leaving_removes_the_person_from_the_very_next_request(self):
        self.get()
        bob_client = self.client_class()
        bob_client.force_authenticate(self.bob)
        self.assertEqual(bob_client.delete("/api/leaderboard/profile/").status_code, 204)
        self.client.force_authenticate(self.cat)
        names = [e["display_name"] for e in self.get().data["entries"]]
        self.assertEqual(names, ["Amy Top", "Cat Low"])

    def test_a_rename_shows_at_once(self):
        self.get()
        leaderboard.join(self.bob, "Bob Renamed")
        self.assertIn("Bob Renamed", [e["display_name"] for e in self.get().data["entries"]])

    def test_a_new_run_shows_at_once(self):
        self.get()
        set_returns(self.cat_strategy, "3.0", "1.0")
        job.run(MONDAY + timedelta(hours=2))
        self.assertEqual(self.get().data["entries"][0]["display_name"], "Cat Low")

    @override_settings(CACHES={"default": {"BACKEND": "api.tests.test_leaderboard_api.BrokenCache"}, "throttle": THROTTLE})
    def test_an_unreachable_cache_still_gives_a_board(self):
        data = self.get().data
        self.assertEqual(len(data["entries"]), 3)
        leaderboard.join(self.bob, "Bob Again")  # joining must not fail either
        self.assertEqual(self.get().data["me"]["rank"], 2)


class ThrottleTests(BoardAPITestCase):
    def test_reads_are_limited_per_address(self):
        with override_settings(AUTH_THROTTLE_RATES={"leaderboard_read_address": "2/min"}):
            statuses = [self.get().status_code for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 429, 429])
