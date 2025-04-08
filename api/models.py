from django.db import models
from django.conf import settings
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin, Permission, Group
from django.utils.translation import gettext_lazy as _
from django.db.models import F
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
    username = models.CharField(max_length=150, unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    account_created = models.DateTimeField(default=timezone.now)
    account_credits = models.IntegerField(default=0, null=False)
    monthly_credits = models.IntegerField(default=MONTHLY_CREDITS, null=False)
    reset_monthly_credits = models.DateTimeField(default=get_thirty_days_away, null=False)
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

    def add_device_id(self, device_id: str):
        """Add a new device ID to the user's list."""
        if not isinstance(self.device_ids, list):
            self.device_ids = []
        if device_id not in self.device_ids:
            self.device_ids.append(device_id)
            self.save(update_fields=["device_ids"])

    def remove_device_id(self, device_id: str):
        """Remove a device ID from the user's list."""
        if device_id in self.device_ids:
            self.device_ids.remove(device_id)
            self.save(update_fields=["device_ids"])

    def check_monthly_credits(self):
        """Reset monthly funds if the reset date has passed."""
        if timezone.now() > self.reset_monthly_credits:
            self.monthly_credits = F("monthly_credits") + MONTHLY_CREDITS  # Atomic update
            self.reset_monthly_credits = timezone.now() + timedelta(days=30)
            self.save(update_fields=["monthly_credits", "reset_monthly_credits"])

    def add_funds(self, amount):
        if amount > 0:
            credit_amount = int(amount)
            self.account_credits += credit_amount
            self.save(update_fields=["account_credits"])

    def __str__(self):
        return self.username


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

    def check_signals(self, prices):
        signals = self.combine_signals()
        indicator_data = SignalService.calculate_indicators(prices, signals)
        indicator_data = BacktestService.find_buys(indicator_data, signals)
        indicator_data = BacktestService.find_sells(indicator_data, signals)

        current_close = indicator_data.iloc[-1]
        prev_close = indicator_data.iloc[-2]
        asset_change = Decimal(current_close["close"] / prev_close["close"])
        if asset_change is None:
            asset_change = Decimal(1)

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

        self.strategy_return = self.strategy_return * current_close["close"] / indicator_data.iloc[-2]["close"]
        if timezone.now() > self.charge_user_date:
            self.charge_user(INTERVAL_MAP[self.interval]['monthly_charge'])
        self.save()
        logger.info("Finished processing data")

    def send_notifications(self, message):
        user: "CustomUser" = self.user
        for device_id in user.device_ids:
            NotificationService.send_push_message(device_id, message)

    def charge_user(self, amount):
        self.charge_user_date = timezone.now() + timedelta(days=30)
        user: "CustomUser" = self.user
        funds_diff = user.monthly_credits - int(amount)
        if funds_diff > 0:
            user.monthly_credits = funds_diff
        else:
            user.monthly_credits = 0
            user.account_credits -= funds_diff

        if user.account_credits < 0:
            user.account_credits = 0
            self.delete()

        user.save(update_fields=["monthly_credits", "account_credits"])



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


class PaymentIntent(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="payment_intents")
    payment_intent_id = models.CharField(max_length=255)
    amount = models.IntegerField(default=0)
