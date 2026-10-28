from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin, Permission, Group
from django.utils.translation import gettext_lazy as _
from api.indicators.signal import Signal
from typing import List
from api.services.signal_service import SignalService
from api.services.backtest_service import BacktestService
from api.services.notification_service import NotificationService
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
import logging

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
    device_ids = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = CustomUserManager()

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
    active = models.BooleanField(default=True)
    last_processed_ts = models.BigIntegerField(null=True, blank=True)  # timestamp of the newest candle already evaluated

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
            }

            if signal.buy_signal:
                updated_buy_signals.append(signal_data)
            else:
                updated_sell_signals.append(signal_data)

        self.buy_signals = updated_buy_signals
        self.sell_signals = updated_sell_signals
        self.save(update_fields=["buy_signals", "sell_signals"])

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
                signal_window=signal.get('signal_window')
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
                signal_window=signal.get('signal_window')
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

        if current_close["buy"] and not current_close["sell"] and self.position < 2:
            self.position = 3
            self.send_notifications(f"{self.title} triggered a buy signal!")
            trade = Trade(
                strategy=self,
                price=current_close["close"],
                is_buy=True,
            )
            trade.save()
        elif current_close["sell"] and not current_close["buy"] and self.position >= 2:
            self.position = 0
            self.send_notifications(f"{self.title} triggered a sell signal!")
            trade = Trade(
                strategy=self,
                price=current_close["close"],
                is_buy=False,
            )
            trade.save()
        elif self.position < 2:
            self.position = 1
        else:
            self.position = 2

        if timezone.now() > self.charge_user_date:
            self.charge_user(INTERVAL_MAP[self.interval]['monthly_charge'])
            if self.pk is None:  # the user could not pay and the strategy was deleted; do not save it back
                return
        if ts is not None:
            self.last_processed_ts = ts
        self.save(update_fields=["position", "strategy_return", "asset_return", "charge_user_date", "last_processed_ts"])
        logger.info("Finished processing data")

    def send_notifications(self, message):
        """Push `message` to the user's devices once the surrounding transaction commits.

        Sending straight away would hold the strategy's row lock during network calls, and a later failure
        would roll back the trade while the push had already gone out (a retry would then notify twice). If the
        transaction rolls back, nothing is sent. A failure for one device does not stop the others.
        """
        device_ids = list(self.user.push_devices.filter(active=True).values_list("token", flat=True))
        transaction.on_commit(lambda: self._push_to_devices(device_ids, message))

    @staticmethod
    def _push_to_devices(device_ids, message):
        for device_id in device_ids:
            try:
                NotificationService.send_push_message(device_id, message)
            except Exception:
                logger.exception("Push notification failed")

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
