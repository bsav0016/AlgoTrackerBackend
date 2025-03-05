from django.db import models
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin, Permission, Group
from django.utils.translation import gettext_lazy as _
from api.indicators.signal import Signal
from typing import List


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
    account_created = models.DateTimeField(auto_now_add=True)
    account_funds = models.DecimalField(default=0, max_digits=10, decimal_places=2)
    free_backtests = models.PositiveSmallIntegerField(default=5)
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

    def __str__(self):
        return self.username


class Strategy(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="strategies")
    symbol = models.CharField(max_length=20)
    interval = models.CharField(max_length=10)
    buy_signals = models.JSONField(default=list)
    sell_signals = models.JSONField(default=list)
    estimated_return = models.DecimalField(default=1, max_digits=12, decimal_places=2, null=True, blank=True)
    prices = models.JSONField(default=list)

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

        self.save()


class StockValue(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="stock_values")
    date = models.DateTimeField(auto_now_add=True)
    price = models.DecimalField(max_digits=20, decimal_places=2)

    def __str__(self):
        return f"{self.strategy.symbol} - {self.date} - {self.price}"


class Trade(models.Model):
    stock_value = models.ForeignKey(StockValue, on_delete=models.CASCADE)
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="trades")
    is_buy = models.BooleanField()


#TODO: Verify if we need this
class Backtest(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="backtests")
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    stock_values = models.ManyToManyField(StockValue)


#TODO: Verify if we need this
class IndicatorValue(models.Model):
    indicator = models.CharField(max_length=50)
    date = models.DateTimeField()
    value = models.DecimalField(max_digits=10, decimal_places=2)
