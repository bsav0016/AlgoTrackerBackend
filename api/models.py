from django.db import models
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin, Permission, Group
from django.utils.translation import gettext_lazy as _


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

    def __str__(self):
        return self.username


class Signal(models.Model):
    indicator = models.CharField(max_length=50)
    target_value = models.DecimalField(max_digits=10, decimal_places=2)
    above_target = models.BooleanField()

    def __str__(self):
        return f"{self.indicator} {'>' if self.above_target else '<'} {self.target_value}"


class Strategy(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="strategies")
    symbol = models.CharField(max_length=20)
    candle_length = models.CharField(max_length=10)
    buy_signals = models.ManyToManyField(Signal, related_name="buy_strategies", blank=True)
    sell_signals = models.ManyToManyField(Signal, related_name="sell_strategies", blank=True)
    initial_investment = models.DecimalField(max_digits=30, decimal_places=2)
    estimated_return = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)


class StockValue(models.Model):
    symbol = models.CharField(max_length=20)
    date = models.DateTimeField()
    price = models.DecimalField(max_digits=10, decimal_places=2)


class Trade(models.Model):
    stock_value = models.ForeignKey(StockValue, on_delete=models.CASCADE)
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="trades")
    is_buy = models.BooleanField()


class Backtest(models.Model):
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="backtests")
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    stock_values = models.ManyToManyField(StockValue)


class IndicatorValue(models.Model):
    indicator = models.CharField(max_length=50)
    date = models.DateTimeField()
    value = models.DecimalField(max_digits=10, decimal_places=2)
