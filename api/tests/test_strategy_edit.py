"""Editing a tracked strategy: what may change, what is kept, what the live job does next, and what it means for the
leaderboard and for a share link."""
from datetime import timedelta
from decimal import Decimal

import pandas as pd
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from api import leaderboard_job as job, leaderboard_rules as rules
from api.indicators.signal import Signal
from api.models import Strategy, Trade
from api.tests.test_api_trading import BUY_SMA, SELL_SMA
from api.tests.test_leaderboard_job import D, MONDAY, NEXT_MONDAY, all_time_board, live_strategy, member, set_returns
from api.tests.test_shared_strategies import ShareTestCase
from api.tests.test_strategy import make_strategy, make_user

RSI_BUY = {"name": "RSI", "above_target": False, "buy_signal": True, "target_value": 30, "window": 14}
RSI_SELL = {"name": "RSI", "above_target": True, "buy_signal": False, "target_value": 70, "window": 14}
NEVER_SELL = {"name": "SMA", "above_target": True, "buy_signal": False, "target_value": 1000.0, "window": 2}


def signal(data):
    return Signal(**data)


class EditApiTestCase(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)
        self.client.post("/api/strategy/", {"title": "Momentum", "symbol": "AAPL", "interval": "1day",
                                            "signals": [BUY_SMA, SELL_SMA]}, format="json")
        self.strategy = Strategy.objects.get()
        self.url = f"/api/strategy/{self.strategy.pk}/"

    def patch(self, body, url=None):
        return self.client.patch(url or self.url, body, format="json")

    def reload(self):
        return Strategy.objects.get(pk=self.strategy.pk)


class WhatMayChangeTests(EditApiTestCase):
    def test_signals_can_be_replaced_and_the_response_is_the_strategy(self):
        response = self.patch({"signals": [RSI_BUY, RSI_SELL]})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        strategy = self.reload()
        self.assertEqual([s["name"] for s in strategy.buy_signals], ["RSI"])
        self.assertEqual([s["name"] for s in strategy.sell_signals], ["RSI"])
        self.assertEqual(response.data["id"], strategy.pk)
        self.assertEqual([s["name"] for s in response.data["buy_signals"]], ["RSI"])

    def test_exit_rules_can_be_added_changed_and_cleared(self):
        self.assertEqual(self.patch({"exit_rules": {"stop_loss": 5}}).status_code, status.HTTP_200_OK)
        self.assertEqual(self.reload().exit_rules, {"stop_loss": 5.0})
        self.patch({"exit_rules": {"stop_loss": 3, "take_profit": 9}})
        self.assertEqual(self.reload().exit_rules, {"stop_loss": 3.0, "take_profit": 9.0})
        self.patch({"exit_rules": {}})
        self.assertEqual(self.reload().exit_rules, {})
        self.assertEqual(len(self.reload().edit_log), 3)

    def test_a_title_alone_is_applied_quietly(self):
        self.assertEqual(self.patch({"title": "Renamed"}).status_code, status.HTTP_200_OK)
        strategy = self.reload()
        self.assertEqual(strategy.title, "Renamed")
        self.assertEqual(strategy.edit_log, [])
        self.assertIsNone(strategy.rules_changed_at)

    def test_the_symbol_and_the_interval_cannot_be_changed(self):
        for field, value in (("symbol", "BTC/USD"), ("interval", "1min")):
            response = self.patch({field: value, "title": "x"})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, field)
            self.assertIn(field, response.data)
        strategy = self.reload()
        self.assertEqual((strategy.symbol, strategy.interval, strategy.title), ("AAPL", "1day", "Momentum"))

    def test_bad_edits_are_refused_and_change_nothing(self):
        before = self.reload()
        bad = [
            {},
            {"signals": [SELL_SMA]},  # no buy signal
            {"signals": [BUY_SMA, {**SELL_SMA, "version": 1}]},  # mixed versions
            {"signals": [{**BUY_SMA, "name": "NOPE"}, SELL_SMA]},
            {"signals": [{**BUY_SMA, "window": 9999}, SELL_SMA]},
            {"exit_rules": {"stop_loss": 100}},
            {"exit_rules": {"take_profit": -5}},
            {"exit_rules": {"bogus": 5}},
            {"title": ""},
            {"title": "x" * 51},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, status.HTTP_400_BAD_REQUEST)
        after = self.reload()
        self.assertEqual((after.buy_signals, after.sell_signals, after.exit_rules, after.title, after.edit_log),
                         (before.buy_signals, before.sell_signals, before.exit_rules, before.title, []))

    def test_someone_elses_strategy_is_a_404_and_is_not_touched(self):
        self.client.force_authenticate(make_user(username="bob", email="bob@example.com"))
        self.assertEqual(self.patch({"title": "mine now"}).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.patch({"signals": [RSI_BUY]}).status_code, status.HTTP_404_NOT_FOUND)  # even invalid data
        self.assertEqual(self.patch({"title": "x"}, url="/api/strategy/999999/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.reload().title, "Momentum")

    def test_signing_in_is_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.patch({"title": "x"}).status_code, status.HTTP_401_UNAUTHORIZED)


class UnknownExitRulesAreRefusedEverywhereTests(EditApiTestCase):
    def test_a_typo_does_not_clear_the_rules_on_an_edit_or_vanish_on_create_or_backtest(self):
        self.patch({"exit_rules": {"stop_loss": 5}})
        for typo in ({"stop_los": 5}, {"stop_loss": 5, "bogus": 1}):
            response = self.patch({"exit_rules": typo})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, typo)
            self.assertIn("exit_rules", response.data)
        self.assertEqual(self.reload().exit_rules, {"stop_loss": 5.0})  # still there
        created = self.client.post("/api/strategy/", {"title": "T", "symbol": "AAPL", "interval": "1day",
                                                      "signals": [BUY_SMA, SELL_SMA], "exit_rules": {"stop_los": 5}}, format="json")
        self.assertEqual(created.status_code, status.HTTP_400_BAD_REQUEST)
        backtest = self.client.post("/api/backtest/", {"symbol": "AAPL", "interval": "1day", "start_date": "2024-01-01T00:00:00Z",
                                                       "end_date": "2024-02-01T00:00:00Z", "signals": [BUY_SMA, SELL_SMA],
                                                       "exit_rules": {"take_profits": 5}}, format="json")
        self.assertEqual(backtest.status_code, status.HTTP_400_BAD_REQUEST)

    def test_off_values_for_known_rules_are_still_fine(self):
        self.assertEqual(self.patch({"exit_rules": {"stop_loss": None, "take_profit": 0}}).status_code, status.HTTP_200_OK)


class NothingChangedTests(EditApiTestCase):
    def test_sending_the_same_rules_changes_nothing_and_costs_nothing(self):
        for body in ({"signals": [BUY_SMA, SELL_SMA]}, {"exit_rules": {}}, {"title": "Momentum"}):
            self.assertEqual(self.patch(body).status_code, status.HTTP_200_OK, body)
        strategy = self.reload()
        self.assertEqual(strategy.edit_log, [])
        self.assertIsNone(strategy.rules_changed_at)

    def test_reordering_signals_is_not_a_change(self):
        two_buys = [BUY_SMA, {**BUY_SMA, "window": 5}, SELL_SMA]
        self.patch({"signals": two_buys})
        self.assertEqual(len(self.reload().edit_log), 1)
        self.patch({"signals": [SELL_SMA, {**BUY_SMA, "window": 5}, BUY_SMA]})
        self.assertEqual(len(self.reload().edit_log), 1)  # same rules in another order

    def test_a_strategy_saved_before_signals_had_versions_is_the_same_as_one_sent_at_version_one(self):
        Strategy.objects.filter(pk=self.strategy.pk).update(
            buy_signals=[{k: v for k, v in s.items() if k != "version"} for s in self.strategy.buy_signals],
            sell_signals=[{k: v for k, v in s.items() if k != "version"} for s in self.strategy.sell_signals])
        self.patch({"signals": [{**BUY_SMA, "version": 1}, {**SELL_SMA, "version": 1}]})
        self.assertEqual(self.reload().edit_log, [])


class WhatIsKeptTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.strategy = make_strategy(self.user, position=2, strategy_return=D("1.25"), asset_return=D("1.10"),
                                      entry_price=100.0, peak_price=110.0)
        Trade.objects.create(strategy=self.strategy, price=100, is_buy=True)

    def test_history_position_and_returns_survive_an_edit(self):
        self.strategy.apply_edit(signals=[signal(RSI_BUY), signal(RSI_SELL)], exit_rules={"stop_loss": 4.0})
        strategy = Strategy.objects.get(pk=self.strategy.pk)
        self.assertEqual((strategy.position, strategy.strategy_return, strategy.asset_return), (2, D("1.25"), D("1.10")))
        self.assertEqual((strategy.entry_price, strategy.peak_price), (100.0, 110.0))
        self.assertEqual(strategy.trades.count(), 1)

    def test_the_record_says_when_and_what_changed(self):
        now = timezone.now()
        self.strategy.apply_edit(exit_rules={"stop_loss": 4.0}, signals=[signal(RSI_BUY), signal(RSI_SELL)], now=now)
        strategy = Strategy.objects.get(pk=self.strategy.pk)
        (entry,) = strategy.edit_log
        self.assertEqual(entry["date"], now.isoformat())
        self.assertEqual(set(entry["changes"]), {"buy_signals", "sell_signals", "exit_rules"})
        self.assertEqual(entry["changes"]["exit_rules"], {"from": {}, "to": {"stop_loss": 4.0}})
        self.assertEqual(entry["changes"]["buy_signals"]["to"][0]["name"], "RSI")
        self.assertEqual(entry["changes"]["buy_signals"]["from"][0]["name"], "SMA")
        self.assertEqual(strategy.rules_changed_at, now)

    def test_only_what_changed_is_recorded(self):
        self.strategy.apply_edit(exit_rules={"take_profit": 8.0})
        (entry,) = Strategy.objects.get(pk=self.strategy.pk).edit_log
        self.assertEqual(set(entry["changes"]), {"exit_rules"})

    def test_the_record_keeps_the_last_twenty(self):
        for i in range(25):
            self.strategy.apply_edit(exit_rules={"stop_loss": 1.0 + i})
        strategy = Strategy.objects.get(pk=self.strategy.pk)
        self.assertEqual(len(strategy.edit_log), Strategy.MAX_EDIT_LOG)
        self.assertEqual(strategy.edit_log[-1]["changes"]["exit_rules"]["to"], {"stop_loss": 25.0})
        self.assertEqual(strategy.edit_log[0]["changes"]["exit_rules"]["to"], {"stop_loss": 6.0})

    def test_a_draft_is_edited_without_a_record_or_a_restart(self):
        draft = make_strategy(self.user, active=False)
        draft.apply_edit(signals=[signal(RSI_BUY), signal(RSI_SELL)], exit_rules={"stop_loss": 2.0})
        draft = Strategy.objects.get(pk=draft.pk)
        self.assertEqual([s["name"] for s in draft.buy_signals], ["RSI"])
        self.assertEqual((draft.edit_log, draft.rules_changed_at), ([], None))

    def test_starting_a_draft_clears_any_edit_state(self):
        draft = make_strategy(self.user, active=False)
        Strategy.objects.filter(pk=draft.pk).update(
            rules_changed_at=timezone.now(), baseline_strategy_return=D("1.3"), baseline_asset_return=D("1.2"),
            edit_log=[{"date": "x", "changes": {}}])
        draft = Strategy.objects.get(pk=draft.pk)
        draft.activate()
        draft = Strategy.objects.get(pk=draft.pk)
        self.assertEqual((draft.rules_changed_at, draft.baseline_strategy_return, draft.baseline_asset_return, draft.edit_log),
                         (None, D("1"), D("1"), []))

    def test_a_clone_starts_without_the_edit_state(self):
        self.strategy.apply_edit(exit_rules={"stop_loss": 4.0})
        copy = Strategy.objects.get(pk=self.strategy.pk).clone_for(make_user(username="bob", email="bob@example.com"))
        self.assertEqual((copy.rules_changed_at, copy.edit_log), (None, []))
        self.assertEqual(copy.exit_rules, {"stop_loss": 4.0})  # but the new rules come with it


class TheLiveJobUsesTheNewRulesTests(TestCase):
    """The strategy is held under the old rules; the next candle is judged by the new ones."""

    def test_a_stop_loss_added_while_holding_applies_to_the_next_candle_and_the_real_entry_price(self):
        user = make_user()
        strategy = make_strategy(user, position=2, entry_price=100.0, peak_price=100.0)
        falling_6 = pd.DataFrame({"close": [100.0, 100.0, 94.0]})

        strategy.apply_edit(signals=[signal(BUY_SMA), signal(NEVER_SELL)], exit_rules={"stop_loss": 5.0})
        strategy = Strategy.objects.get(pk=strategy.pk)
        strategy.check_signals(falling_6)

        strategy = Strategy.objects.get(pk=strategy.pk)
        self.assertEqual(strategy.position, 0)
        sale = Trade.objects.get()
        self.assertFalse(sale.is_buy)
        self.assertEqual(sale.exit_reason, "stop_loss")

    def test_without_the_edit_the_old_signal_sells_with_no_exit_reason(self):
        user = make_user()
        strategy = make_strategy(user, position=2, entry_price=100.0, peak_price=100.0)
        strategy.check_signals(pd.DataFrame({"close": [100.0, 100.0, 94.0]}))
        self.assertEqual(Trade.objects.get().exit_reason, "")

    def test_a_position_stays_open_through_an_edit_that_changes_nothing_about_it(self):
        user = make_user()
        strategy = make_strategy(user, position=2, entry_price=100.0, peak_price=100.0)
        strategy.apply_edit(exit_rules={"take_profit": 50.0})
        Strategy.objects.get(pk=strategy.pk).check_signals(pd.DataFrame({"close": [10.0, 11.0, 12.0]}))
        self.assertEqual(Strategy.objects.get(pk=strategy.pk).position, 2)
        self.assertEqual(Trade.objects.count(), 0)


class LeaderboardAfterAnEditTests(TestCase):
    """An edit starts the strategy over for the board, and the board counts only what it earned since."""

    def edited(self, days_before_monday=3, strategy_return="1.5", asset_return="1.2"):
        """A strategy that ranked before: 30 days live, 3 trades, 1.5 against 1.2. Its rules were edited recently."""
        user = member("ed")
        strategy = live_strategy(user, strategy_return=strategy_return, asset_return=asset_return)
        self.assertIsNone(rules.ineligible_reason(strategy, MONDAY))
        strategy.apply_edit(exit_rules={"stop_loss": 5.0}, now=MONDAY - timedelta(days=days_before_monday))
        return Strategy.objects.get(pk=strategy.pk)

    def test_the_edit_stops_it_ranking_until_it_has_14_days_and_3_trades_again(self):
        strategy = self.edited()
        self.assertEqual(rules.ineligible_reason(strategy, MONDAY), "too_new")
        later = strategy.rules_changed_at + timedelta(days=15)
        self.assertEqual(rules.ineligible_reason(strategy, later), "too_few_trades")  # the old trades do not count
        day = strategy.rules_changed_at + timedelta(hours=6)
        for _ in range(3):
            Trade.objects.create(strategy=strategy, price=10, is_buy=True, date=day)
            Trade.objects.create(strategy=strategy, price=11, is_buy=False, date=day + timedelta(hours=1))
            day += timedelta(days=2)
        self.assertEqual(rules.completed_trades(strategy), 3)
        self.assertIsNone(rules.ineligible_reason(strategy, later))

    def test_the_clock_starts_at_the_edit_not_at_the_start(self):
        strategy = self.edited()
        self.assertEqual(rules.clock_start(strategy), strategy.rules_changed_at)
        self.assertEqual(rules.clock_start(live_strategy(member("plain"))), MONDAY - timedelta(days=30))

    def test_the_score_counts_only_what_it_earned_since_the_edit(self):
        strategy = self.edited()
        self.assertEqual((strategy.baseline_strategy_return, strategy.baseline_asset_return), (D("1.5"), D("1.2")))
        set_returns(strategy, "1.65", "1.2")  # +10% on the strategy since the edit, nothing on the asset
        strategy = Strategy.objects.get(pk=strategy.pk)
        self.assertEqual(rules.score(strategy), D("1.1000"))  # not 1.65 / 1.2 = 1.375, which would credit the old rules
        since_strategy, since_asset = rules.board_returns(strategy)
        self.assertEqual((since_strategy.quantize(D("0.0001")), since_asset), (D("1.1000"), D("1")))

    def test_the_all_time_board_shows_the_returns_since_the_edit(self):
        strategy = self.edited(days_before_monday=40)  # long enough ago
        day = strategy.rules_changed_at + timedelta(hours=6)
        for _ in range(3):
            Trade.objects.create(strategy=strategy, price=10, is_buy=True, date=day)
            Trade.objects.create(strategy=strategy, price=11, is_buy=False, date=day + timedelta(hours=1))
            day += timedelta(days=2)
        set_returns(strategy, "1.65", "1.2")
        job.run(MONDAY)
        (entry,) = all_time_board()
        self.assertEqual(entry.score, D("1.1000"))
        self.assertEqual(entry.strategy_return.quantize(D("0.01")), D("1.10"))
        self.assertEqual(entry.asset_return, D("1"))

    def test_a_strategy_edited_during_a_week_has_no_result_for_that_week(self):
        user = member("mid")
        strategy = live_strategy(user, strategy_return="1.5", asset_return="1.2")
        job.run(MONDAY)  # snapshots the start of the week
        job.take_snapshots(NEXT_MONDAY.date())
        strategy.apply_edit(exit_rules={"stop_loss": 5.0}, now=MONDAY + timedelta(days=2))
        # Long after, so the 14-day minimum is no excuse: the week itself must be skipped.
        published = job.publish_week(MONDAY.date(), NEXT_MONDAY.date(), MONDAY + timedelta(days=90))
        self.assertEqual(published, 0)

    def test_the_week_is_published_normally_for_a_strategy_that_was_not_edited_that_week(self):
        user = member("calm")
        live_strategy(user, strategy_return="1.5", asset_return="1.2")
        job.run(MONDAY)
        job.take_snapshots(NEXT_MONDAY.date())
        self.assertEqual(job.publish_week(MONDAY.date(), NEXT_MONDAY.date(), MONDAY + timedelta(days=90)), 1)

    def test_a_strategy_that_was_never_edited_is_scored_exactly_as_before(self):
        strategy = live_strategy(member("plain"), strategy_return="1.5", asset_return="1.2")
        self.assertEqual(rules.score(strategy), D("1.2500"))
        self.assertEqual(rules.board_returns(strategy), (D("1.5"), D("1.2")))

    def test_a_missing_or_zero_baseline_means_no_valid_return(self):
        strategy = live_strategy(member("zero"))
        Strategy.objects.filter(pk=strategy.pk).update(baseline_strategy_return=D("0"))
        strategy = Strategy.objects.get(pk=strategy.pk)
        self.assertEqual(rules.board_returns(strategy), (None, None))
        self.assertEqual(rules.ineligible_reason(strategy, MONDAY), "no_valid_return")


class WhatTheAppAndTheShareLinkSeeTests(EditApiTestCase):
    def test_the_strategy_in_the_users_details_carries_when_and_what_changed(self):
        self.patch({"exit_rules": {"stop_loss": 5}})
        shown = self.client.get("/api/user-details/").data["strategies"][0]
        self.assertIsNotNone(shown["rules_changed_at"])
        self.assertEqual(len(shown["edits"]), 1)
        self.assertEqual(shown["edits"][0]["changes"]["exit_rules"], {"from": {}, "to": {"stop_loss": 5.0}})

    def test_a_strategy_never_edited_says_so(self):
        shown = self.client.get("/api/user-details/").data["strategies"][0]
        self.assertEqual((shown["rules_changed_at"], shown["edits"]), (None, []))

    def test_the_edit_costs_no_extra_query_per_strategy(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as one:
            self.client.get("/api/user-details/")
        for _ in range(2):
            other = make_strategy(self.user)
            other.apply_edit(exit_rules={"stop_loss": 3.0})
        with CaptureQueriesContext(connection) as three:
            self.client.get("/api/user-details/")
        self.assertEqual(len(three) - len(one), 2)  # one trades query each, as before; the edit record is on the row


class TheShareLinkShowsWhenTheRulesChangedTests(ShareTestCase):
    def test_the_public_view_shows_the_date_only_after_an_edit_and_nothing_else_about_it(self):
        link = self.share()
        self.assertIsNone(self.client.get(f"/api/shared/{link}/").data["rules_changed_at"])
        self.sign_in(self.owner)
        self.assertEqual(self.client.patch(f"/api/strategy/{self.strategy.pk}/", {"exit_rules": {"stop_loss": 5}},
                                           format="json").status_code, status.HTTP_200_OK)
        self.client.force_authenticate(None)
        shown = self.client.get(f"/api/shared/{link}/").data
        self.assertIsNotNone(shown["rules_changed_at"])
        self.assertEqual(shown["exit_rules"], {"stop_loss": 5.0})  # the link shows the strategy as it is now
        self.assertNotIn("edits", shown)  # what changed, from and to, is for the owner
