from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth import authenticate
from api.exit_rules import LIMITS, STOP_LOSS, TAKE_PROFIT, TRAILING_STOP
from api.indicators.signal import CURRENT_VERSION, LEGACY_VERSION
from api.models import CreditTransaction, CustomUser, Notification, Strategy, Trade
from api.services.card_service import equity_points
from django.conf import settings
from django.db.models import prefetch_related_objects
import math

PROMO_CODES = settings.PROMO_CODES


class TradeSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=20, decimal_places=2, coerce_to_string=False)

    class Meta:
        model = Trade
        fields = ['id', 'date', 'price', 'is_buy', 'exit_reason']


class ComparisonSerializer(serializers.Serializer):
    """The second indicator of a rule that compares two indicators with each other."""
    name = serializers.CharField(max_length=100)
    window = serializers.IntegerField()


class SignalSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    above_target = serializers.BooleanField()
    buy_signal = serializers.BooleanField()
    # Required unless the rule has `compare_to`; then it is ignored and stored as 0.
    target_value = serializers.FloatField(required=False)
    # Compare the indicator with a second one instead of a number: {"name": "SMA_PRICE", "window": 50}.
    compare_to = ComparisonSerializer(required=False, allow_null=True)
    window = serializers.IntegerField(required=False, allow_null=True)
    fast_window = serializers.IntegerField(required=False, allow_null=True)
    slow_window = serializers.IntegerField(required=False, allow_null=True)
    signal_window = serializers.IntegerField(required=False, allow_null=True)
    # Which definition of the indicator to use. Left out means the current one.
    version = serializers.ChoiceField(choices=(LEGACY_VERSION, CURRENT_VERSION), required=False, default=CURRENT_VERSION)

    SIGNAL_WINDOWS = {
        "RSI": ("window",), "SMA": ("window",), "EMA": ("window",), "SO": ("window",), "BBP": ("window",),
        "ADX": ("window",), "MACD": ("fast_window", "slow_window", "signal_window"),
        "SMA_PRICE": ("window",), "EMA_PRICE": ("window",),
    }
    # Which indicators a rule may be compared with, by the indicator it starts from. Only indicators on the same scale
    # can be compared: a price average with a price average. (An oscillator from 0 to 100 against a price means
    # nothing.) The price averages exist only for this; they cannot be compared with a typed number, because a price
    # level differs for every market.
    COMPARISONS = {"SMA_PRICE": ("SMA_PRICE", "EMA_PRICE"), "EMA_PRICE": ("SMA_PRICE", "EMA_PRICE")}
    MAX_WINDOW = 500  # live strategies see at most the last 500 candles

    def validate(self, attrs):
        required = self.SIGNAL_WINDOWS.get(attrs["name"])
        if required is None:
            raise serializers.ValidationError({"name": f"Unknown indicator: {attrs['name']}"})
        for field in required:
            value = attrs.get(field)
            if value is None or not 1 <= value <= self.MAX_WINDOW:
                raise serializers.ValidationError(
                    {field: f"{attrs['name']} needs {field} between 1 and {self.MAX_WINDOW}"})
        compare = attrs.pop("compare_to", None)
        if compare is None:
            if attrs["name"] in self.COMPARISONS:
                raise serializers.ValidationError(
                    {"compare_to": f"{attrs['name']} is compared with another average: send compare_to"})
            if "target_value" not in attrs:
                raise serializers.ValidationError({"target_value": "This field is required."})
            if not math.isfinite(attrs["target_value"]):
                raise serializers.ValidationError({"target_value": "Target value must be a finite number"})
            return attrs
        allowed = self.COMPARISONS.get(attrs["name"])
        if allowed is None:
            raise serializers.ValidationError({"compare_to": f"{attrs['name']} cannot be compared with another indicator"})
        if compare["name"] not in allowed:
            raise serializers.ValidationError(
                {"compare_to": f"{attrs['name']} can only be compared with {' or '.join(allowed)}"})
        if not 1 <= compare["window"] <= self.MAX_WINDOW:
            raise serializers.ValidationError(
                {"compare_to": f"{compare['name']} needs window between 1 and {self.MAX_WINDOW}"})
        if compare["name"] == attrs["name"] and compare["window"] == attrs["window"]:
            raise serializers.ValidationError({"compare_to": "An average cannot be compared with itself; pick a different one"})
        attrs["compare_to"] = {"name": compare["name"], "window": compare["window"]}
        attrs["target_value"] = 0.0  # unused for a comparison; stored so older readers still find the field
        return attrs


class ExitRulesSerializer(serializers.Serializer):
    """Percentages; a rule that is left out, null or 0 is off. See exit_rules.py."""
    stop_loss = serializers.FloatField(required=False, allow_null=True, min_value=0, max_value=LIMITS[STOP_LOSS][1])
    trailing_stop = serializers.FloatField(required=False, allow_null=True, min_value=0,
                                           max_value=LIMITS[TRAILING_STOP][1])
    take_profit = serializers.FloatField(required=False, allow_null=True, min_value=0, max_value=LIMITS[TAKE_PROFIT][1])

    def to_internal_value(self, data):
        # A name this does not know is refused, not dropped: dropped, a typo would read as "no rules", and on an edit it
        # would clear the exit rules the strategy has.
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({unknown[0]: "Not an exit rule. Use stop_loss, trailing_stop or take_profit."})
        return super().to_internal_value(data)

    def validate(self, attrs):
        rules = {}
        for name, value in attrs.items():
            if not value:
                continue
            if value < LIMITS[name][0]:
                raise serializers.ValidationError({name: f"{name} must be at least {LIMITS[name][0]} percent"})
            rules[name] = value
        return rules


def with_signal_versions(data):
    """Strategies saved before versions existed use the original definitions; say so rather than leave it blank."""
    for key in ("buy_signals", "sell_signals"):
        data[key] = [signal if "version" in signal else {**signal, "version": LEGACY_VERSION} for signal in data[key]]
    return data


def one_version(signals):
    if len({signal["version"] for signal in signals}) > 1:
        raise serializers.ValidationError("All signals in a strategy must use the same indicator version")
    return signals


class StrategySerializer(serializers.ModelSerializer):
    """Serializer for Strategies, including buy/sell signals and trades"""
    buy_signals = serializers.JSONField()
    sell_signals = serializers.JSONField()
    exit_rules = serializers.JSONField(read_only=True)
    # When the rules were last edited, and the last few edits (what changed, from and to). Empty for a strategy never edited.
    edits = serializers.JSONField(source='edit_log', read_only=True)
    trades = TradeSerializer(many=True, read_only=True)
    equity_curve = serializers.SerializerMethodField()

    def get_equity_curve(self, strategy):
        """The strategy's value over time, from the same function the share card draws from (`equity_points`), so the
        screen and the card cannot disagree. Points are `{"date", "multiple"}`, the multiple starting at 1.0 on the
        first buy; empty until there is a finished round trip or an open position to draw."""
        trades = sorted(strategy.trades.all(), key=lambda trade: (trade.date, trade.pk))  # uses a prefetch if there is one
        return [{"date": moment, "multiple": multiple}
                for moment, multiple in equity_points(trades, strategy.strategy_return)]

    def to_representation(self, instance):
        # The trades are used twice (the list and the curve); load them once. Does nothing if already prefetched.
        prefetch_related_objects([instance], "trades")
        return with_signal_versions(super().to_representation(instance))

    class Meta:
        model = Strategy
        fields = [
            'id', 'title', 'symbol', 'interval', 'buy_signals', 'sell_signals', 'exit_rules', 'strategy_return',
            'asset_return', 'position', 'charge_user_date', 'active', 'last_processed_ts', 'rules_changed_at', 'edits', 'trades',
            'equity_curve'
        ]
        read_only_fields = ['last_processed_ts']  # the live job sets it: the open time (Unix seconds) of the newest candle checked


class SharedStrategySerializer(serializers.ModelSerializer):
    """What anyone holding a share link may see. An allow-list on purpose: nothing about the owner (id, name,
    email, credits, devices), the billing date or the strategy's internal state is ever added here."""

    class Meta:
        model = Strategy
        fields = ['title', 'symbol', 'interval', 'buy_signals', 'sell_signals', 'exit_rules', 'strategy_return',
                  'asset_return', 'rules_changed_at']
        read_only_fields = fields

    def to_representation(self, instance):
        return with_signal_versions(super().to_representation(instance))


class UserRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ['username', 'first_name', 'last_name', 'email', 'password']
        extra_kwargs = {'password': {'write_only': True}}

    def validate_username(self, value):
        if CustomUser.objects.filter(username=value).exists():
            raise serializers.ValidationError("Username already taken")
        return value

    def validate_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email already taken")
        return value

    def create(self, validated_data):
        user = CustomUser.objects.create_user(
            username=validated_data['username'],
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            email=validated_data['email'],
            password=validated_data['password']
        )
        return user


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField()

    def validate(self, data):
        username = data.get("username")
        password = data.get("password")
        user = authenticate(username=username, password=password)
        if user is None:
            raise AuthenticationFailed("Invalid credentials.")
        if not user.is_active:
            raise AuthenticationFailed("User is inactive.")
        return user

    def get_user_strategies(self, user):
        user_strategies = Strategy.objects.filter(user=user)
        strategy_data = [StrategySerializer(strategy).data for strategy in user_strategies]
        return strategy_data


class CreditTransactionSerializer(serializers.ModelSerializer):
    """One line of a user's credit history. `meta` and the internal event key are not exposed."""
    label = serializers.SerializerMethodField()

    class Meta:
        model = CreditTransaction
        fields = ['id', 'created_at', 'bucket', 'amount', 'balance_after', 'kind', 'label']
        read_only_fields = fields

    def get_label(self, row):
        meta = row.meta or {}
        refund = "refund_of" in meta
        if row.kind == "purchase":
            return "Credit purchase"
        if row.kind == "backtest":
            what = f"Backtest {meta['symbol']} {meta['interval']}".strip() if "symbol" in meta else "Backtest"
            return f"{what} (refund)" if refund else what
        if row.kind == "subscription":
            return f"Strategy subscription {meta['symbol']} {meta['interval']}" if "symbol" in meta \
                else "Strategy subscription"
        if row.kind == "monthly_grant":
            return "Monthly credits reset"
        if row.kind == "promo":
            return "Promo code"
        return "Opening balance" if meta.get("reason") == "opening balance" else "Adjustment"


class CustomUserSerializer(serializers.ModelSerializer):
    """Serializer for Users, including their strategies"""
    strategies = StrategySerializer(many=True, read_only=True)

    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'first_name', 'last_name', 'email', 'account_credits', 'account_created',
            'monthly_credits', 'reset_monthly_credits', 'leaderboard_name', 'strategies'
        ]
        read_only_fields = ['id', 'account_created', 'is_staff']


class BacktestRequestSerializer(serializers.Serializer):
    symbol = serializers.CharField(max_length=50)
    interval = serializers.CharField(max_length=10)
    start_date = serializers.DateTimeField()
    end_date = serializers.DateTimeField()
    signals = SignalSerializer(many=True)
    exit_rules = ExitRulesSerializer(required=False, default=dict)
    next_candle_execution = serializers.BooleanField(required=False, default=False)

    def validate_signals(self, signals):
        return one_version(signals)


class StrategyEditSerializer(serializers.Serializer):
    """What may be changed on a strategy that exists: its title, signals and exit rules, checked as on create. The symbol and
    the candle interval are not editable (a different market is a different strategy, and the interval sets the charge).
    Every field is optional; an empty edit is refused."""
    title = serializers.CharField(max_length=50, required=False)
    signals = SignalSerializer(many=True, required=False)
    exit_rules = ExitRulesSerializer(required=False)

    def validate(self, attrs):
        for field in ("symbol", "interval"):
            if field in self.initial_data:
                raise serializers.ValidationError({field: f"The {field} of a strategy cannot be changed. Start a new strategy instead."})
        if not attrs:
            raise serializers.ValidationError("Send at least one of title, signals or exit_rules to change.")
        return attrs

    def validate_signals(self, signals):
        if not any(signal["buy_signal"] for signal in signals):
            raise serializers.ValidationError("A strategy needs at least one buy signal")
        return one_version(signals)


class StrategyRequestSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=50)
    symbol = serializers.CharField(max_length=50)
    interval = serializers.CharField(max_length=10)
    signals = SignalSerializer(many=True)
    exit_rules = ExitRulesSerializer(required=False, default=dict)

    def validate_signals(self, signals):
        if not any(signal["buy_signal"] for signal in signals):
            raise serializers.ValidationError("A strategy needs at least one buy signal")
        return one_version(signals)


class NotificationSerializer(serializers.ModelSerializer):
    """One signal the user was notified about. `strategy_id` and `trade_id` are what a tap on the push opens."""
    trade_id = serializers.IntegerField(read_only=True)
    price = serializers.DecimalField(source='trade.price', max_digits=20, decimal_places=2, read_only=True)

    class Meta:
        model = Notification
        fields = ['id', 'created_at', 'kind', 'message', 'strategy_id', 'trade_id', 'price']
        read_only_fields = fields
