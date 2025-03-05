from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ObjectDoesNotExist
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from api.serializers import (UserRegistrationSerializer, LoginSerializer, CustomUserSerializer, BacktestRequestSerializer,
                          StrategyRequestSerializer)
from api.models import Strategy
from api.services.auth_service import AuthService
from api.services.backtest_service import BacktestService
from api.services.strategy_service import StrategyService
from api.indicators.signal import Signal
from rest_framework_simplejwt.authentication import JWTAuthentication


class UserRegistrationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny, ]

    def post(self, request, format=None):
        try:
            serializer = UserRegistrationSerializer(data=request.data)
            if serializer.is_valid():
                user = serializer.save()
                tokens = AuthService.create_tokens(user=user)

                user_data = CustomUserSerializer(user).data
                return Response({
                    **user_data,
                    "refresh_token": tokens['refresh'],
                    "access_token": tokens['access'],
                },
                    status=status.HTTP_201_CREATED
                )
            return Response({"detail": serializer.errors}, status=status.HTTP_409_CONFLICT)
        except Exception as e:
            print(e)
            return Response({"detail": "Error registering user"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny, ]

    def post(self, request, format=None):
        serializer = LoginSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.validate(data=request.data)
            tokens = AuthService.create_tokens(user=user)
            user_data = CustomUserSerializer(user).data

            return Response({
                **user_data,
                "refresh_token": tokens['refresh'],
                "access_token": tokens['access'],
            },
                status=status.HTTP_200_OK
            )
        return Response({"detail": "Invalid credentials."}, status=status.HTTP_400_BAD_REQUEST)


class LogoutView(APIView):
    def post(self, request, *args, **kwargs):
        try:
            if self.request.data.get('all'):
                token: OutstandingToken
                for token in OutstandingToken.objects.filter(user=request.user):
                    _, _ = BlacklistedToken.objects.get_or_create(token=token)
                return Response({"message": "All refresh tokens blacklisted"})
            refresh_token = request.data.get('refresh')
            outstanding_token = OutstandingToken.objects.get(token=refresh_token)
            BlacklistedToken.objects.get_or_create(token=outstanding_token)
            return Response({"message": "Logout completed"}, status=status.HTTP_200_OK)

        except ObjectDoesNotExist:
            return Response({"detail": "Token does not exist"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"detail": "Error: " + str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DeviceIDView(APIView):
    """API to manage device IDs for push notifications."""
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        """Add a new device ID to the user's list."""
        device_id = request.data.get("device_id")

        if not device_id:
            return Response({"error": "Device ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        if device_id not in user.device_ids:
            user.add_device_id(device_id)
            return Response({"message": "Device ID added successfully"}, status=status.HTTP_200_OK)
        else:
            return Response({"message": "Device ID already exists"}, status=status.HTTP_200_OK)

    def delete(self, request):
        """Remove a device ID from the user's list."""
        device_id = request.data.get("device_id")

        if not device_id:
            return Response({"error": "Device ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        if device_id in user.device_ids:
            user.remove_device_id(device_id)
            return Response({"message": "Device ID removed successfully"}, status=status.HTTP_200_OK)
        else:
            return Response({"error": "Device ID not found"}, status=status.HTTP_404_NOT_FOUND)


class BacktestView(APIView):
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        serializer = BacktestRequestSerializer(data=request.data)
        if serializer.is_valid():
            try:
                symbol = serializer.validated_data['symbol']
                interval = serializer.validated_data['interval']
                start_date = serializer.validated_data['start_date']
                end_date = serializer.validated_data['end_date']
                signals = serializer.validated_data['signals']

                signals = [Signal(**signal) for signal in signals]

                result = BacktestService.conduct_backtest(symbol, interval, start_date, end_date, signals)
                raw_result = result.to_dict(orient='records')

                processed_result = []
                for entry in raw_result:
                    indicators = {}
                    core_fields = ["datetime", "close", "buy", "sell", "owned", "return"]

                    for key, value in entry.items():
                        if key not in core_fields:
                            indicators[key] = value

                    transformed_entry = {k: entry[k] for k in core_fields}
                    transformed_entry["indicators"] = indicators

                    processed_result.append(transformed_entry)

                response_data = {"result": processed_result}

                return Response(response_data, status=status.HTTP_200_OK)
            except Exception as e:
                print(e)
                return Response({"detail": "Internal Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class StrategyView(APIView):
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        serializer = StrategyRequestSerializer(data=request.data)
        if serializer.is_valid():
            try:
                symbol = serializer.validated_data['symbol']
                interval = serializer.validated_data['interval']
                signals = serializer.validated_data['signals']

                signals = [Signal(**signal) for signal in signals]

                result = StrategyService().start_strategy(
                    user=request.user,
                    symbol=symbol,
                    interval=interval,
                    signals=signals
                )
                if result:
                    response_data = {"message": "Successfully subscribed strategy"}

                    return Response(response_data, status=status.HTTP_200_OK)
                else:
                    return Response({"detail": "Unsuccessful"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            except Exception as e:
                print(e)
                return Response({"detail": "Internal Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        try:
            strategy = Strategy.objects.get(pk=pk)
            if strategy.user != request.user:
                return Response({"detail": "You do not have permission to delete this strategy."},
                                status=status.HTTP_403_FORBIDDEN)
            strategy.analyzing_flag = False
            strategy.save()
            strategy.delete()
            return Response({"message": "Strategy deleted successfully"}, status=status.HTTP_200_OK)

        except Strategy.DoesNotExist:
            return Response({"detail": "Strategy not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            print(e)
            return Response({"detail": "Internal error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
