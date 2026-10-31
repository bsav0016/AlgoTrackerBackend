from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from api import leaderboard_rules as rules
from api.models import Strategy, Trade
from api.tests.test_strategy import make_strategy, make_user

NOW = timezone.now()


def add_round_trips(strategy, count, start=None):
    """`count` buys, each closed by a sell, one day apart from `start`."""
    day = start or (NOW - timedelta(days=13))
    for _ in range(count):
        Trade.objects.create(strategy=strategy, price=10, is_buy=True, date=day)
        Trade.objects.create(strategy=strategy, price=11, is_buy=False, date=day + timedelta(hours=1))
        day += timedelta(days=1)


class BoardTestCase(TestCase):
    def setUp(self):
        self.user = make_user(username="u", email="u@example.com")
        self.user.leaderboard_name = "Quiet Trader"
        self.user.save()

    def ranked_strategy(self, **kwargs):
        """A strategy that meets every rule, unless `kwargs` break one."""
        defaults = dict(started_at=NOW - timedelta(days=20), strategy_return=Decimal("1.2"), asset_return=Decimal("1.1"))
        interval = kwargs.pop("interval", None)  # make_strategy fixes the interval, so it is changed afterwards
        strategy = make_strategy(self.user, **{**defaults, **kwargs})
        if interval:
            Strategy.objects.filter(pk=strategy.pk).update(interval=interval)
        add_round_trips(strategy, 3)
        return Strategy.objects.get(pk=strategy.pk)


class EligibilityTests(BoardTestCase):
    def test_a_strategy_that_meets_every_rule_ranks(self):
        self.assertIsNone(rules.ineligible_reason(self.ranked_strategy(), NOW))

    def test_a_draft_does_not_rank(self):
        self.assertEqual(rules.ineligible_reason(self.ranked_strategy(active=False), NOW), "not_active")

    def test_an_owner_who_is_not_on_the_board_does_not_rank(self):
        strategy = self.ranked_strategy()
        self.user.leaderboard_name = None
        self.user.save()
        strategy = Strategy.objects.get(pk=strategy.pk)
        self.assertEqual(rules.ineligible_reason(strategy, NOW), "owner_not_on_board")

    def test_fourteen_days_are_needed(self):
        young = self.ranked_strategy(started_at=NOW - timedelta(days=13, hours=23))
        self.assertEqual(rules.ineligible_reason(young, NOW), "too_new")
        exact = self.ranked_strategy(started_at=NOW - timedelta(days=14))
        self.assertIsNone(rules.ineligible_reason(exact, NOW))

    def test_a_strategy_with_no_start_date_does_not_rank(self):
        self.assertEqual(rules.ineligible_reason(self.ranked_strategy(started_at=None), NOW), "start_unknown")

    def test_three_completed_trades_are_needed(self):
        strategy = make_strategy(self.user, started_at=NOW - timedelta(days=20))
        add_round_trips(strategy, 2)
        self.assertEqual(rules.ineligible_reason(strategy, NOW), "too_few_trades")
        add_round_trips(strategy, 1, start=NOW - timedelta(days=5))
        self.assertIsNone(rules.ineligible_reason(strategy, NOW))

    def test_an_open_buy_is_not_a_completed_trade(self):
        strategy = make_strategy(self.user, started_at=NOW - timedelta(days=20))
        add_round_trips(strategy, 2)
        Trade.objects.create(strategy=strategy, price=10, is_buy=True, date=NOW - timedelta(days=2))
        self.assertEqual(rules.completed_trades(strategy), 2)

    def test_a_stray_sell_with_nothing_held_is_not_counted(self):
        strategy = make_strategy(self.user, started_at=NOW - timedelta(days=20))
        Trade.objects.create(strategy=strategy, price=10, is_buy=False, date=NOW - timedelta(days=10))
        add_round_trips(strategy, 2)
        self.assertEqual(rules.completed_trades(strategy), 2)

    def test_trades_from_before_the_start_do_not_count(self):
        strategy = make_strategy(self.user, started_at=NOW - timedelta(days=15))
        add_round_trips(strategy, 3, start=NOW - timedelta(days=40))
        self.assertEqual(rules.completed_trades(strategy), 0)

    def test_an_unknown_interval_does_not_rank(self):
        self.assertEqual(rules.ineligible_reason(self.ranked_strategy(interval="3sec"), NOW), "unknown_interval")

    def test_a_missing_or_zero_asset_return_does_not_rank_or_crash(self):
        self.assertEqual(rules.ineligible_reason(self.ranked_strategy(asset_return=Decimal("0")), NOW), "no_valid_return")


class ScoreAndGroupTests(BoardTestCase):
    def test_the_score_is_return_over_buy_and_hold(self):
        strategy = self.ranked_strategy(strategy_return=Decimal("1.32"), asset_return=Decimal("1.20"))
        self.assertEqual(rules.score(strategy), Decimal("1.1000"))

    def test_a_strategy_that_lost_less_than_the_asset_scores_above_one(self):
        strategy = self.ranked_strategy(strategy_return=Decimal("0.90"), asset_return=Decimal("0.80"))
        self.assertGreater(rules.score(strategy), 1)

    def test_scores_are_rounded_to_four_places(self):
        strategy = self.ranked_strategy(strategy_return=Decimal("1.00000"), asset_return=Decimal("3.00000"))
        self.assertEqual(rules.score(strategy), Decimal("0.3333"))

    def test_intervals_split_into_intraday_and_daily_or_longer(self):
        self.assertEqual(rules.group_for("1min"), rules.INTRADAY)
        self.assertEqual(rules.group_for("1day"), rules.DAILY_OR_LONGER)
        self.assertIsNone(rules.group_for("nope"))

    @override_settings(INTERVAL_MAP={"1h": {"seconds": 3600}, "1week": {"seconds": 604800}})
    def test_the_split_follows_the_configured_candle_length(self):
        self.assertEqual(rules.group_for("1h"), rules.INTRADAY)
        self.assertEqual(rules.group_for("1week"), rules.DAILY_OR_LONGER)


class RankingTests(BoardTestCase):
    def test_equal_scores_share_a_rank_and_the_next_rank_is_skipped(self):
        ranked = rules.rank_scores([("a", Decimal("1.2")), ("b", Decimal("1.5")), ("c", Decimal("1.2")),
                                    ("d", Decimal("1.0"))])
        self.assertEqual([(key, rank) for key, _, rank in ranked], [("b", 1), ("a", 2), ("c", 2), ("d", 4)])

    def test_an_empty_board_ranks_nothing(self):
        self.assertEqual(rules.rank_scores([]), [])

    def test_one_best_strategy_per_person_per_group(self):
        weak = self.ranked_strategy(strategy_return=Decimal("1.1"), asset_return=Decimal("1.1"))
        strong = self.ranked_strategy(strategy_return=Decimal("1.5"), asset_return=Decimal("1.1"))
        intraday = self.ranked_strategy(interval="1min")
        grouped = rules.best_per_user([weak, strong, intraday])
        self.assertEqual([s.pk for s, _ in grouped[rules.DAILY_OR_LONGER]], [strong.pk])
        self.assertEqual([s.pk for s, _ in grouped[rules.INTRADAY]], [intraday.pk])

    def test_a_tie_between_a_persons_strategies_keeps_the_older_one(self):
        first = self.ranked_strategy()
        second = self.ranked_strategy()
        grouped = rules.best_per_user([second, first])
        self.assertEqual([s.pk for s, _ in grouped[rules.DAILY_OR_LONGER]], [first.pk])

    def test_ineligible_strategies_are_left_out(self):
        good = self.ranked_strategy()
        draft = self.ranked_strategy(active=False)
        grouped = rules.best_per_user([good, draft])
        self.assertEqual([s.pk for s, _ in grouped[rules.DAILY_OR_LONGER]], [good.pk])

    def test_different_people_each_get_a_place(self):
        other = make_user(username="o", email="o@example.com")
        other.leaderboard_name = "Other Person"
        other.save()
        mine = self.ranked_strategy()
        theirs = make_strategy(other, started_at=NOW - timedelta(days=20),
                               strategy_return=Decimal("1.3"), asset_return=Decimal("1.1"))
        add_round_trips(theirs, 3)
        grouped = rules.best_per_user([mine, Strategy.objects.get(pk=theirs.pk)])
        self.assertEqual(len(grouped[rules.DAILY_OR_LONGER]), 2)


class StartTimeTests(BoardTestCase):
    def test_a_new_strategy_starts_now(self):
        strategy = make_strategy(self.user)
        self.assertLess(abs(timezone.now() - strategy.started_at), timedelta(seconds=5))

    def test_a_cloned_draft_has_no_start_until_it_is_activated(self):
        draft = self.ranked_strategy().clone_for(self.user)
        self.assertIsNone(draft.started_at)
        draft.activate()
        draft.refresh_from_db()
        self.assertLess(abs(timezone.now() - draft.started_at), timedelta(seconds=5))
        self.assertEqual((draft.strategy_return, draft.asset_return), (1, 1))
