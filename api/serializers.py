from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth import authenticate
from api.models import CustomUser, Strategy, Trade
from django.conf import settings

PROMO_CODES = settings.PROMO_CODES


class TradeSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=20, decimal_places=2, coerce_to_string=False)

    class Meta:
        model = Trade
        fields = ['id', 'date', 'price', 'is_buy']


class SignalSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    above_target = serializers.BooleanField()
    buy_signal = serializers.BooleanField()
    target_value = serializers.FloatField()
    window = serializers.IntegerField(required=False, allow_null=True)
    fast_window = serializers.IntegerField(required=False, allow_null=True)
    slow_window = serializers.IntegerField(required=False, allow_null=True)
    signal_window = serializers.IntegerField(required=False, allow_null=True)


class StrategySerializer(serializers.ModelSerializer):
    """Serializer for Strategies, including buy/sell signals and trades"""
    buy_signals = serializers.JSONField()
    sell_signals = serializers.JSONField()
    trades = TradeSerializer(many=True, read_only=True)

    class Meta:
        model = Strategy
        fields = [
            'id', 'title', 'symbol', 'interval', 'buy_signals', 'sell_signals', 'strategy_return', 'asset_return',
            'position', 'charge_user_date', 'active', 'trades'
        ]


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


class CustomUserSerializer(serializers.ModelSerializer):
    """Serializer for Users, including their strategies"""
    strategies = StrategySerializer(many=True, read_only=True)

    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'first_name', 'last_name', 'email', 'account_funds', 'account_created',
            'monthly_funds', 'reset_monthly_funds', 'strategies'
        ]
        read_only_fields = ['id', 'account_created', 'is_staff']


class BacktestRequestSerializer(serializers.Serializer):
    symbol = serializers.CharField(max_length=50)
    interval = serializers.CharField(max_length=10)
    start_date = serializers.DateTimeField()
    end_date = serializers.DateTimeField()
    signals = SignalSerializer(many=True)


class StrategyRequestSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=50)
    symbol = serializers.CharField(max_length=50)
    interval = serializers.CharField(max_length=10)
    signals = SignalSerializer(many=True)
