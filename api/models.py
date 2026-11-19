from django.db import models, transaction
from django.db.models.functions import Lower
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin, Permission, Group
from django.utils.translation import gettext_lazy as _
from api.indicators.signal import Signal, LEGACY_VERSION
from typing import List
from api.exit_rules import BUY, LABELS, SELL, PositionState, advance
from api.services.signal_service import SignalService
from api.services.backtest_service import BacktestService
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
import json
import logging
import secrets

logger = logging.getLogger(__name__)

MONTHLY_CREDITS = settings.MONTHLY_CREDITS
INTERVAL_MAP = settings.INTERVAL_MAP


def get_thirty_days_away():
    return timezone.now() + timedelta(days=30)


class CustomUserManager(BaseUserManager):
    def create_user(self, username, email, password=None, **extra_fields):
        if not email:
            raise ValueError(_("The Email field must be set"))
        if not username:
            raise ValueError(_("The Username field must be set"))

        email = self.normalize_email(email)
        user = self.model(username=username, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(username, email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    username = models.CharField(max_length=150, unique=True, null=False, blank=False)
    first_name = models.CharField(max_length=100, null=False, blank=False)
    last_name = models.CharField(max_length=100, null=False, blank=False)
    email = models.EmailField(unique=True, null=False, blank=False)
    account_created = models.DateTimeField(default=timezone.now, null=False, blank=False)
    account_credits = models.IntegerField(default=0, null=False, blank=False)
    monthly_credits = models.IntegerField(default=MONTHLY_CREDITS, null=False, blank=False)
    reset_monthly_credits = models.DateTimeField(default=get_thirty_days_away, null=False, blank=False)
    # The name shown on the public leaderboard. Empty means the user is not on it (the default); see api/leaderboard.py.
    leaderboard_name = models.CharField(max_length=20, null=True, blank=True, default=None)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = CustomUserManager()

    class Meta:
        constraints = [
            # Two people cannot hold the same board name, however it is capitalised.
            models.UniqueConstraint(Lower('leaderboard_name'), name='unique_leaderboard_name_ci'),
        ]

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email', 'first_name', 'last_name']

    groups = models.ManyToManyField(
        Group,
        related_name='customuser_set',
        blank=True
    )
    user_permissions = models.ManyToManyField(
        Permission,
        related_name='customuser_set',
        blank=True
    )

    def check_monthly_credits(self):
        """Reset monthly funds if the reset date has passed."""
        from api import credits
        if credits.reset_monthly(self.pk, MONTHLY_CREDITS):
            self.refresh_from_db(fields=["monthly_credits", "reset_monthly_credits"])

    def __str__(self):
        return self.username


class PushDevice(models.Model):
    """One phone's Expo push token. A token belongs to exactly one account: registering it again from another
    account moves it, so a phone that signs into a second account stops notifying the first."""
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="push_devices")
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=20, blank=True, default="")
    active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def register(cls, user, token, platform=""):
        """Bind `token` to `user` (moving it from any other account) and mark it active. Returns (device, created)."""
        device, created = cls.objects.update_or_create(
            token=token, defaults={"user": user, "platform": platform, "active": True, "last_seen_at": timezone.now()})
        return device, created

    def __str__(self):
        return f"{self.user_id}:{self.token}"


@receiver(post_save, sender=CustomUser)
def start_credit_ledger(sender, instance, created, raw=False, **kwargs):
    """Every new user gets opening ledger rows for the balances they start with (not when loading fixtures)."""
    if created and not raw:
        from api import credits
        credits.record_opening_balance(instance)


class Strategy(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="strategies")
    title = models.CharField(default='(Strategy Title)', max_length=50)
    symbol = models.CharField(max_length=20)
    interval = models.CharField(max_length=10)
    buy_signals = models.JSONField(default=list)
    sell_signals = models.JSONField(default=list)
    position = models.SmallIntegerField(default=1) #0: sell, 1: hold (sold), 2: hold (bought), 3: buy
    strategy_return = models.DecimalField(default=1, max_digits=10, decimal_places=5)
    asset_return = models.DecimalField(default=1, max_digits=10, decimal_places=5)
    charge_user_date = models.DateTimeField(default=get_thirty_days_away)
    active = models.BooleanField(default=True)  # False for a draft (a copy of a shared strategy) that is not tracked or billed yet
    cloned_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="clones")
    last_processed_ts = models.BigIntegerField(null=True, blank=True)  # timestamp of the newest candle already evaluated
    # When live tracking began: set on creation and again on activation, the moment `strategy_return` and
    # `asset_return` start from 1. Empty for strategies from before it was recorded, and for drafts.
    started_at = models.DateTimeField(null=True, blank=True, default=timezone.now)
    # Optional exits that depend on the entry price, as {"stop_loss": 5, "take_profit": 10, "trailing_stop": 4}
    # (percent); see exit_rules.py. Empty means the strategy only trades on its signals.
    exit_rules = models.JSONField(default=dict, blank=True)
    entry_price = models.FloatField(null=True, blank=True)  # the close it bought at, while holding
    peak_price = models.FloatField(null=True, blank=True)  # the highest close since then (a trailing stop trails it)
    exit_blocked = models.BooleanField(default=False)  # a rule sold while the buy signals held: wait for them to clear
    # Editing the rules of a tracked strategy (see apply_edit). The leaderboard counts only what a strategy earned since
    # its rules last changed, so it cannot be tuned after seeing how it did: the clock restarts, and the returns at that
    # moment become the baseline the board measures from. `strategy_return` and `asset_return` themselves keep the whole
    # history, which is what the strategy's own screen shows.
    rules_changed_at = models.DateTimeField(null=True, blank=True)
    baseline_strategy_return = models.DecimalField(default=1, max_digits=10, decimal_places=5)
    baseline_asset_return = models.DecimalField(default=1, max_digits=10, decimal_places=5)
    edit_log = models.JSONField(default=list, blank=True)  # the last MAX_EDIT_LOG rule edits: {date, changes}

    def add_signals(self, signals: List[Signal]):
        updated_buy_signals = []
        updated_sell_signals = []

        for signal in signals:
            signal_data = {
                "name": signal.name,
                "above_target": signal.above_target,
                "buy_signal": signal.buy_signal,
                "target_value": signal.target_value,
                "window": signal.window,
                "fast_window": signal.fast_window,
                "slow_window": signal.slow_window,
                "signal_window": signal.signal_window,
                "version": signal.version,
            }

            if signal.buy_signal:
                updated_buy_signals.append(signal_data)
            else:
                updated_sell_signals.append(signal_data)

        self.buy_signals = updated_buy_signals
        self.sell_signals = updated_sell_signals
        self.save(update_fields=["buy_signals", "sell_signals"])

    MAX_EDIT_LOG = 20

    @staticmethod
    def signal_dicts(signals: List[Signal]):
        """The stored form of a list of signals: (buy signals, sell signals), one dict each."""
        buy, sell = [], []
        for signal in signals:
            data = {
                "name": signal.name, "above_target": signal.above_target, "buy_signal": signal.buy_signal,
                "target_value": signal.target_value, "window": signal.window, "fast_window": signal.fast_window,
                "slow_window": signal.slow_window, "signal_window": signal.signal_window, "version": signal.version,
            }
            (buy if signal.buy_signal else sell).append(data)
        return buy, sell

    @staticmethod
    def _same_signals(stored, new):
        """Whether two lists of signals do the same thing: order does not matter (they are all required at once), and a
        signal stored before versions existed is version 1."""
        key = lambda items: sorted(json.dumps({**item, "version": item.get("version", LEGACY_VERSION)}, sort_keys=True)
                                   for item in items)
        return key(stored) == key(new)

    def apply_edit(self, title=None, signals=None, exit_rules=None, now=None):
        """Change this strategy's title, signals or exit rules. The caller holds the row lock (select_for_update).

        Position, entry price, returns and trades are kept, and the live job reads the new rules from the next candle, so
        a position bought under the old rules is judged by the new ones. The symbol and the interval are not editable:
        a different market is a different strategy, and the interval sets the monthly charge.

        A change to the *rules* (signals or exit rules) on a tracked strategy is recorded, restarts its leaderboard clock
        and sets its leaderboard baseline to the returns it has now. A new title is not a rules change and is applied
        quietly, and so is any change to a draft, which is not tracked. Returns whether anything changed."""
        now = now or timezone.now()
        update = []
        changes = {}

        if title is not None and title != self.title:
            self.title = title
            update.append("title")

        if signals is not None:
            buy, sell = self.signal_dicts(signals)
            if not self._same_signals(self.buy_signals, buy):
                changes["buy_signals"] = {"from": self.buy_signals, "to": buy}
                self.buy_signals = buy
                update.append("buy_signals")
            if not self._same_signals(self.sell_signals, sell):
                changes["sell_signals"] = {"from": self.sell_signals, "to": sell}
                self.sell_signals = sell
                update.append("sell_signals")

        if exit_rules is not None and exit_rules != (self.exit_rules or {}):
            changes["exit_rules"] = {"from": self.exit_rules or {}, "to": exit_rules}
            self.exit_rules = exit_rules
            update.append("exit_rules")

        if changes and self.active:
            self.rules_changed_at = now
            self.baseline_strategy_return = self.strategy_return
            self.baseline_asset_return = self.asset_return
            self.edit_log = ([*self.edit_log, {"date": now.isoformat(), "changes": changes}])[-self.MAX_EDIT_LOG:]
            update += ["rules_changed_at", "baseline_strategy_return", "baseline_asset_return", "edit_log"]

        if update:
            self.save(update_fields=update)
        return bool(update)

    def combine_signals(self):
        """Method to combine buy and sell signals, convert to Signal dataclass, and store them."""
        buy_signal_objects = [
            Signal(
                name=signal['name'],
                above_target=signal['above_target'],
                buy_signal=True,
                target_value=signal['target_value'],
                window=signal.get('window'),
                fast_window=signal.get('fast_window'),
                slow_window=signal.get('slow_window'),
                signal_window=signal.get('signal_window'),
                version=signal.get('version', LEGACY_VERSION),  # saved before versions existed: the original definitions
            )
            for signal in self.buy_signals
        ]

        sell_signal_objects = [
            Signal(
                name=signal['name'],
                above_target=signal['above_target'],
                buy_signal=False,
                target_value=signal['target_value'],
                window=signal.get('window'),
                fast_window=signal.get('fast_window'),
                slow_window=signal.get('slow_window'),
                signal_window=signal.get('signal_window'),
                version=signal.get('version', LEGACY_VERSION),  # saved before versions existed: the original definitions
            )
            for signal in self.sell_signals
        ]

        combined_signals = buy_signal_objects + sell_signal_objects
        return combined_signals

    def check_signals(self, prices, ts=None):
        """Evaluate the newest candle in `prices` (chronological rows) and update position and returns.

        `ts` is the timestamp of that candle; it is stored in `last_processed_ts` so a repeat or late job for
        the same candle can be skipped. Needs at least two candles (the return is the change from the previous one).
        """
        if len(prices) < 2:
            return
        signals = self.combine_signals()
        indicator_data = SignalService.calculate_indicators(prices, signals)
        indicator_data = BacktestService.find_buys(indicator_data, signals)
        indicator_data = BacktestService.find_sells(indicator_data, signals)

        current_close = indicator_data.iloc[-1]
        prev_close = indicator_data.iloc[-2]
        asset_change = Decimal(current_close["close"] / prev_close["close"]) if prev_close["close"] else Decimal(1)

        if self.position >= 2:
            self.strategy_return = self.strategy_return * asset_change
        self.asset_return = self.asset_return * asset_change

        before = PositionState(owned=self.position >= 2, entry_price=self.entry_price, peak_price=self.peak_price,
                               blocked=self.exit_blocked)
        after, action = advance(before, bool(current_close["buy"]), bool(current_close["sell"]),
                                float(current_close["close"]), self.exit_rules)
        self.entry_price, self.peak_price, self.exit_blocked = after.entry_price, after.peak_price, after.blocked
        if action == BUY:
            self.position = 3
            trade = Trade.objects.create(strategy=self, price=current_close["close"], is_buy=True)
            self.send_notifications(trade, f"{self.title} triggered a buy signal!")
        elif action is not None:  # a sell signal or an exit rule
            self.position = 0
            reason = "" if action == SELL else action
            trade = Trade.objects.create(strategy=self, price=current_close["close"], is_buy=False, exit_reason=reason)
            what = "triggered a sell signal" if action == SELL else f"hit its {LABELS[action]} and sold"
            self.send_notifications(trade, f"{self.title} {what}!")
        else:
            self.position = 2 if after.owned else 1

        if timezone.now() > self.charge_user_date:
            self.charge_user(INTERVAL_MAP[self.interval]['monthly_charge'])
            if self.pk is None:  # the user could not pay and the strategy was deleted; do not save it back
                return
        if ts is not None:
            self.last_processed_ts = ts
        self.save(update_fields=["position", "strategy_return", "asset_return", "charge_user_date", "last_processed_ts",
                                 "entry_price", "peak_price", "exit_blocked"])
        logger.info("Finished processing data")

    def clone_for(self, user):
        """A draft copy for `user`: the same rules, symbol, interval and title, not tracked or billed until activated."""
        return Strategy.objects.create(
            user=user, title=self.title, symbol=self.symbol, interval=self.interval,
            buy_signals=self.buy_signals, sell_signals=self.sell_signals, exit_rules=self.exit_rules, active=False,
            cloned_from=self,
            started_at=None)

    def activate(self):
        """Start tracking a draft now: fresh returns and position, and the first monthly charge 30 days away."""
        self.active = True
        self.position = 1
        self.entry_price = self.peak_price = None
        self.exit_blocked = False
        self.strategy_return = 1
        self.asset_return = 1
        self.rules_changed_at = None
        self.baseline_strategy_return = self.baseline_asset_return = 1
        self.edit_log = []
        self.last_processed_ts = None
        self.charge_user_date = get_thirty_days_away()
        self.started_at = timezone.now()
        self.save(update_fields=["active", "position", "strategy_return", "asset_return", "last_processed_ts",
                                 "charge_user_date", "started_at", "entry_price", "peak_price", "exit_blocked",
                                 "rules_changed_at", "baseline_strategy_return", "baseline_asset_return", "edit_log"])

    def send_notifications(self, trade, message):
        """Record that `trade` owes the user a push, in the same transaction as the trade.

        The Notification and one PushDelivery per active device are written now, so a worker that dies before
        sending loses nothing: the rows are still there to send. The send job is queued once the transaction
        commits; if the transaction rolls back the rows go with it and nothing is sent.
        """
        from api.tasks import send_notification_task
        notification = Notification.objects.create(
            user_id=self.user_id, strategy=self, trade=trade, message=message,
            kind=Notification.BUY if trade.is_buy else Notification.SELL)
        devices = list(self.user.push_devices.filter(active=True))
        PushDelivery.objects.bulk_create([PushDelivery(notification=notification, device=d) for d in devices])
        if devices:
            transaction.on_commit(lambda: send_notification_task.delay(notification.pk))

    def charge_user(self, amount):
        """Charge one month of this subscription. If the user cannot pay, the strategy is deleted and nothing is charged.

        The ledger key names this strategy and its billing date, so running the same job again does not charge twice.
        """
        from api import credits
        key = f"subscription:{self.pk}:{int(self.charge_user_date.timestamp())}"
        try:
            credits.spend(self.user_id, amount, key, "subscription",
                          meta={"strategy": self.pk, "symbol": self.symbol, "interval": self.interval})
        except credits.InsufficientCredits:
            self.delete()
            return
        self.charge_user_date = timezone.now() + timedelta(days=30)


def new_share_id():
    return secrets.token_urlsafe(16)


class StrategyShare(models.Model):
    """A public link to one strategy. The id is random (128 bits), so a link cannot be guessed; deleting the row
    revokes it, and sharing again makes a new id. A strategy has at most one live share."""
    strategy = models.OneToOneField(Strategy, on_delete=models.CASCADE, related_name="share")
    share_id = models.CharField(max_length=32, unique=True, default=new_share_id, editable=False)
    created_at = models.DateTimeField(default=timezone.now)


class LeaderboardSnapshot(models.Model):
    """A strategy's cumulative returns at the start of a week (Monday, UTC). A week's result is the change between
    two snapshots. Only strategies of people on the leaderboard are recorded, and leaving deletes them."""
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="leaderboard_snapshots")
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="leaderboard_snapshots")
    period_start = models.DateField()
    strategy_return = models.DecimalField(max_digits=10, decimal_places=5)
    asset_return = models.DecimalField(max_digits=10, decimal_places=5)
    taken_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["strategy", "period_start"], name="one_snapshot_per_week")]


class LeaderboardEntry(models.Model):
    """One person's place on one board. `kind` is a finished `week` (starting `period_start`) or the current
    `all_time` board (no `period_start`). The name shown is read live from the user, so a rename shows at once."""
    WEEK = "week"
    ALL_TIME = "all_time"
    KINDS = [(WEEK, "Week"), (ALL_TIME, "All time")]

    kind = models.CharField(max_length=10, choices=KINDS)
    period_start = models.DateField(null=True, blank=True)
    group = models.CharField(max_length=20)
    rank = models.PositiveIntegerField()
    score = models.DecimalField(max_digits=12, decimal_places=4)  # return over buy-and-hold: 1.10 is 10% better
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="leaderboard_entries")
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="leaderboard_entries")
    strategy_return = models.DecimalField(max_digits=14, decimal_places=5)  # over the board's period
    asset_return = models.DecimalField(max_digits=14, decimal_places=5)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "period_start", "group", "user"], name="one_entry_per_user_per_board"),
            # NULLs never clash in a unique constraint, so the all-time board needs its own.
            models.UniqueConstraint(fields=["kind", "group", "user"], condition=models.Q(period_start__isnull=True),
                                    name="one_all_time_entry_per_user_per_group"),
        ]
        indexes = [models.Index(fields=["kind", "period_start", "group", "rank"])]


class LeaderboardRun(models.Model):
    """Marks a finished week as published, so the job writes each week's board exactly once."""
    kind = models.CharField(max_length=10, default=LeaderboardEntry.WEEK)
    period_start = models.DateField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "period_start"], name="one_run_per_board")]


class StockValue(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="stock_values")
    date = models.DateTimeField(default=timezone.now)
    price = models.DecimalField(max_digits=20, decimal_places=2)

    def __str__(self):
        return f"{self.strategy.symbol} - {self.date} - {self.price}"


class Trade(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="trades")
    date = models.DateTimeField(default=timezone.now)
    price = models.DecimalField(default=0, max_digits=20, decimal_places=2)
    is_buy = models.BooleanField()
    exit_reason = models.CharField(max_length=20, blank=True, default="")  # the exit rule that made this sell, if one did


class Notification(models.Model):
    """A signal the user is owed a push for. Written in the transaction that records the trade."""
    BUY = "buy"
    SELL = "sell"
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="notifications")
    strategy = models.ForeignKey(Strategy, on_delete=models.SET_NULL, null=True, related_name="notifications")
    trade = models.OneToOneField(Trade, on_delete=models.CASCADE, related_name="notification")
    kind = models.CharField(max_length=10, choices=[(BUY, "Buy"), (SELL, "Sell")])
    message = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now)


class PushDelivery(models.Model):
    """One notification going to one device. The job that sends it can run any number of times: only rows still
    `pending` and due are sent, and a row never leaves `sent` or a final state."""
    PENDING = "pending"
    SENT = "sent"            # Expo accepted it; the receipt has not confirmed delivery yet
    DELIVERED = "delivered"  # Expo's receipt says it reached the push service (FCM / APNs)
    FAILED = "failed"
    DEAD_TOKEN = "dead_token"
    STATUSES = [(PENDING, "Pending"), (SENT, "Sent"), (DELIVERED, "Delivered"), (FAILED, "Failed"),
                (DEAD_TOKEN, "Dead token")]

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="deliveries")
    device = models.ForeignKey(PushDevice, on_delete=models.CASCADE, related_name="deliveries")
    status = models.CharField(max_length=12, choices=STATUSES, default=PENDING)
    ticket_id = models.CharField(max_length=100, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=255, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notification", "device"], name="one_delivery_per_device")]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class SavedBacktest(models.Model):
    """A finished backtest, kept for its owner. See saved_backtests.py for what is stored and why."""
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="saved_backtests")
    created_at = models.DateTimeField(default=timezone.now)
    label = models.CharField(max_length=60, blank=True, default="")
    symbol = models.CharField(max_length=20)
    interval = models.CharField(max_length=10)
    requested_start = models.DateTimeField()
    requested_end = models.DateTimeField()
    covered_start = models.DateTimeField()  # what the market data actually covered (see DataWindow)
    covered_end = models.DateTimeField()
    buy_signals = models.JSONField(default=list)
    sell_signals = models.JSONField(default=list)
    exit_rules = models.JSONField(default=dict, blank=True)
    next_candle_execution = models.BooleanField(default=False)
    cost_percent = models.FloatField(default=0)
    summary = models.JSONField(default=dict)  # the response's numbers, without the rows
    series = models.JSONField(default=list)  # thinned rows: datetime, close, owned, return, exit_reason

    class Meta:
        indexes = [models.Index(fields=["user", "-id"])]


#Not using it now but may use it later?
class Backtest(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="backtests")
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    stock_values = models.ManyToManyField(StockValue)


#Not using it now but may use it later?
class IndicatorValue(models.Model):
    indicator = models.CharField(max_length=50)
    date = models.DateTimeField()
    value = models.DecimalField(max_digits=10, decimal_places=2)


class CreditTransaction(models.Model):
    """One signed change to one of a user's two credit balances.

    Every balance change is written here by `api.credits.apply`, which also updates the cached totals on
    `CustomUser`. `idempotency_key` names the business event (a store transaction, a charge), so the same event
    can never be applied twice. Rows are never edited or deleted by the app.
    """
    ACCOUNT = "account"
    MONTHLY = "monthly"
    BUCKETS = [(ACCOUNT, "Account credits"), (MONTHLY, "Monthly credits")]

    KINDS = [
        ("purchase", "Purchase"),
        ("backtest", "Backtest"),
        ("subscription", "Strategy subscription"),
        ("monthly_grant", "Monthly grant"),
        ("promo", "Promo code"),
        ("adjustment", "Adjustment"),
    ]

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="credit_transactions")
    bucket = models.CharField(max_length=10, choices=BUCKETS)
    amount = models.IntegerField()  # signed: positive adds credits, negative spends them
    balance_after = models.IntegerField()  # that bucket's balance once this row applied
    kind = models.CharField(max_length=20, choices=KINDS)
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key", "bucket"], name="unique_credit_event_per_bucket"),
        ]
        indexes = [models.Index(fields=["user", "-id"], name="credit_tx_user_recent")]

    def __str__(self):
        return f"{self.user_id} {self.bucket} {self.amount:+d} ({self.kind})"


class PaymentIntent(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="payment_intents")
    payment_intent_id = models.CharField(max_length=255)
    amount_cents = models.IntegerField(default=0)  # what Stripe will charge, in US cents


class RevenueCatCustomer(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="revenue_cat_customer")
    customer_id = models.CharField(max_length=100, default="", unique=True, null=False, blank=False)


class RevenueCatConfirmation(models.Model):
    transaction_id = models.CharField(max_length=32, unique=True)
    transaction_credits = models.IntegerField(default=0, null=False, blank=False)
    customer_id = models.CharField(max_length=50, default="", null=False, blank=False)
