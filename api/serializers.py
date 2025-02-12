from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth import authenticate
from .models import CustomUser, Strategy, Signal, Trade, StockValue


class SignalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Signal
        fields = ['id', 'indicator', 'target_value', 'above_target']


class TradeSerializer(serializers.ModelSerializer):
    stock_value = serializers.SerializerMethodField()

    class Meta:
        model = Trade
        fields = ['id', 'stock_value', 'is_buy']

    def get_stock_value(self, obj):
        return {
            "symbol": obj.stock_value.symbol,
            "date": obj.stock_value.date,
            "price": obj.stock_value.price
        }


class StrategySerializer(serializers.ModelSerializer):
    """Serializer for Strategies, including buy/sell signals and trades"""
    buy_signals = SignalSerializer(many=True, read_only=True)
    sell_signals = SignalSerializer(many=True, read_only=True)
    trades = TradeSerializer(many=True, read_only=True)

    class Meta:
        model = Strategy
        fields = [
            'id', 'symbol', 'candle_length', 'buy_signals', 'sell_signals',
            'initial_investment', 'estimated_return', 'trades'
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
            'id', 'username', 'first_name', 'last_name', 'email', 'account_funds',
            'free_backtests', 'strategies'
        ]
        read_only_fields = ['id', 'account_created', 'is_staff']
