import math

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from api.serializers import (UserRegistrationSerializer, LoginSerializer, CustomUserSerializer,
                             BacktestRequestSerializer, StrategyRequestSerializer)
from api.models import Strategy, CustomUser, PaymentIntent
from api.services.auth_service import AuthService
from api.services.backtest_service import BacktestService
from api.indicators.signal import Signal
from rest_framework_simplejwt.authentication import JWTAuthentication
import random
import numpy as np
from django.core.mail import send_mail
from api.redis_manager import RedisManager
import stripe
from stripe import StripeError


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
            user.check_monthly_funds()
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


class AvailableSymbolsAndIntervalsView(APIView):
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        intervals = list(settings.INTERVAL_MAP.keys())
        return Response({
            "symbols": settings.SYMBOLS,
            "intervals": intervals
        }, status=status.HTTP_200_OK)


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
                            indicators[key] = 0 if (isinstance(value, float) and np.isnan(value)) else value

                    transformed_entry = {k: (0 if (isinstance(entry[k], float) and np.isnan(entry[k])) else entry[k])
                                         for k in core_fields}
                    transformed_entry["indicators"] = indicators

                    processed_result.append(transformed_entry)

                img_base64 = BacktestService.generate_backtest_chart(result)
                sharpe_ratio = BacktestService.calculate_sharpe_ratio(result)
                sortino_ratio = BacktestService.calculate_sortino_ratio(result)

                hours = 365 * 24 #TODO: This needs to be 252*6.5 if not traded 24/7
                sqrt_n = math.sqrt(hours * 3600 / settings.INTERVAL_MAP[interval])

                sharpe_ratio = sharpe_ratio * sqrt_n
                sortino_ratio = sortino_ratio * sqrt_n

                response_data = {
                    "result": processed_result,
                    "image": img_base64,
                    "sharpe_ratio": sharpe_ratio,
                    "sortino_ratio": sortino_ratio
                }

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
                title = serializer.validated_data['title']
                symbol = serializer.validated_data['symbol']
                interval = serializer.validated_data['interval']
                signals = serializer.validated_data['signals']

                signals = [Signal(**signal) for signal in signals]
                strategy = Strategy(
                    user=request.user,
                    title=title,
                    symbol=symbol,
                    interval=interval
                )
                strategy.add_signals(signals)
                strategy.save()

                return Response({"message": "Successfully subscribed strategy"}, status=status.HTTP_200_OK)

            except Exception as e:
                print("Strategy view internal error: ", e)
                return Response({"detail": "Internal Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        try:
            strategy = Strategy.objects.get(pk=pk)
            if strategy.user != request.user:
                return Response({"detail": "You do not have permission to delete this strategy."},
                                status=status.HTTP_403_FORBIDDEN)
            strategy.delete()
            return Response({"message": "Strategy deleted successfully"}, status=status.HTTP_200_OK)

        except ObjectDoesNotExist:
            return Response({"detail": "Strategy not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            print(e)
            return Response({"detail": "Internal error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PasswordResetRequestView(APIView):
    """Generate OTP and send it via email for password reset"""
    permission_classes = [AllowAny, ]

    def post(self, request):
        email = request.data.get("email")
        print(email)
        try:
            CustomUser.objects.get(email=email)
        except ObjectDoesNotExist:
            return Response({"detail": "User with email not found"}, status=status.HTTP_404_NOT_FOUND)

        otp = random.randint(100000, 999999)

        RedisManager.delete_otp(email)
        RedisManager.store_otp(email, otp)

        send_mail(
            subject="Your AlgoTracker Password Reset Code",
            message=f"Your AlgoTracker password reset code is: {otp}\n" +
                    "This code will expire in 10 minutes.\n\n" +
                    "Sincerely,\n" +
                    "AlgoTracker Support",
            from_email=settings.EMAIL_HOST,
            recipient_list=[email],
        )

        return Response({"message": "Password reset code sent"}, status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    """Verify OTP and reset password"""
    permission_classes = [AllowAny, ]

    def post(self, request):
        username = request.data.get("username")
        otp = request.data.get("otp")
        new_password = request.data.get("password")

        try:
            user = CustomUser.objects.get(username=username)
            stored_otp = RedisManager.get_otp(user.email)
            print(stored_otp)
            print(otp)

            if not stored_otp or str(stored_otp) != str(otp):
                return Response({"error": "Invalid or expired OTP"}, status=status.HTTP_417_EXPECTATION_FAILED)

            user.set_password(new_password)
            user.save()

            RedisManager.delete_otp(user.email)
            return Response({"message": "Password reset successful"}, status=status.HTTP_200_OK)
        except ObjectDoesNotExist:
            return Response({"detail": "User with email not found"}, status=status.HTTP_404_NOT_FOUND)


class StripePaymentView(APIView):
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        try:
            amount = request.data.get("amount")
            amount = int(amount)
            customer = stripe.Customer.create()
            ephemeral_key = stripe.EphemeralKey.create(
                customer=customer['id'],
                stripe_version='2022-11-15'
            )

            payment_intent = stripe.PaymentIntent.create(
                amount=amount,
                currency='usd',
                customer=customer['id']
            )

            dollar_amount = amount / 100.0
            saved_payment_intent = PaymentIntent(
                user=request.user,
                payment_intent_id=payment_intent.id,
                amount=dollar_amount
            )
            saved_payment_intent.save()

            return Response({
                "payment_intent": payment_intent,
                "ephemeral_key": ephemeral_key,
                "customer": customer
            }, status=status.HTTP_201_CREATED)

        except (TypeError, ValueError):
            return Response({"detail": "Amount not properly formatted"}, status=status.HTTP_400_BAD_REQUEST)

        except StripeError as e:
            return Response({"detail" : f"Stripe error: {str(e)}"}, status=status.HTTP_402_PAYMENT_REQUIRED)

        except Exception as e:
            return Response({"detail" : f"Internal error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StripePaymentConfirmationView(APIView):
    def post(self, request):
        event = None
        payload = request.data
        sig_header = request.headers['STRIPE_SIGNATURE']

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            raise e
        except stripe.error.SignatureVerificationError as e:
            raise e

        if event['type'] == 'payment_intent.succeeded':
            payment_intent = event['data']['object']
            payment_intent_id = payment_intent['id']

            try:
                saved_payment_intent = PaymentIntent.objects.get(payment_intent_id=payment_intent_id)
                user = saved_payment_intent.user
                amount = saved_payment_intent.amount

                user.add_funds(amount)
                saved_payment_intent.delete()

                return Response({"message": "Payment successful, funds added."}, status=status.HTTP_200_OK)
            except ObjectDoesNotExist:
                return Response({"detail": "PaymentIntent not found."}, status=status.HTTP_404_NOT_FOUND)

        else:
            return Response({"detail": f"Unhandled event type {event['type']}"}, status=status.HTTP_400_BAD_REQUEST)
