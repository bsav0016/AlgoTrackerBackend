import math
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.conf import settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from api.serializers import (UserRegistrationSerializer, LoginSerializer, CustomUserSerializer,
                             BacktestRequestSerializer, StrategyRequestSerializer)
from api.models import Strategy, CustomUser, PaymentIntent, RevenueCatDeposit, RevenueCatConfirmation
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

stripe.api_key = settings.STRIPE_SECRET_KEY
INTERVAL_MAP = settings.INTERVAL_MAP
BACKTEST_BASE_COST = settings.BACKTEST_BASE_COST
SYMBOLS = settings.SYMBOLS
EMAIL_HOST = settings.EMAIL_HOST
STRIPE_WEBHOOK_SECRET = settings.STRIPE_WEBHOOK_SECRET
BACKTEST_ITERATIONS = settings.BACKTEST_ITERATIONS
PROMO_CODES = settings.PROMO_CODES
PROMO_CODE_AVAILABILITY = settings.PROMO_CODE_AVAILABILITY


class UserRegistrationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny, ]

    def post(self, request, format=None):
        try:
            data = request.data
            promo_code = data.pop("promo_code", "")
            serializer = UserRegistrationSerializer(data=data)
            if not serializer.is_valid():
                return Response({"detail": serializer.errors}, status=status.HTTP_409_CONFLICT)

            if promo_code not in ["", " ", "N/A"] \
                    and promo_code not in list(PROMO_CODES.keys()):
                raise ValidationError("Invalid promo code")

            user = serializer.save()
            tokens = AuthService.create_tokens(user=user)

            if promo_code in list(PROMO_CODES.keys()):
                promo_code_value = int(PROMO_CODES[promo_code])
                user.add_funds(promo_code_value)

            user_data = CustomUserSerializer(user).data
            return Response({
                **user_data,
                "refresh_token": tokens['refresh'],
                "access_token": tokens['access'],
            },
                status=status.HTTP_201_CREATED
            )
        except ValidationError as e:
            return Response({"detail": e}, status=status.HTTP_409_CONFLICT)
        except Exception as e:
            print(e)
            return Response({"detail": "Error registering user"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PromoCodeAvailabilityView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny,]

    def get(self, request):
        return Response({"availability": PROMO_CODE_AVAILABILITY}, status=status.HTTP_200_OK)

class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny, ]

    def post(self, request, format=None):
        serializer = LoginSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.validate(data=request.data)
            user.check_monthly_credits()
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


class DeleteAccountView(APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        user = request.user
        data = request.data
        password = data.get("password", "")

        if not user.is_superuser:
            if not user.check_password(password):
                return Response({"detail": "Invalid password"}, status=status.HTTP_403_FORBIDDEN)

        try:
            tokens = OutstandingToken.objects.filter(user=user)
            for token in tokens:
                BlacklistedToken.objects.get_or_create(token=token)

            user.delete()
            return Response({"detail": "Account successfully deleted"}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"detail": f"Error deleting account: {str(e)}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UserDetailsView(APIView):
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def get(self, request):
        user = request.user
        user.check_monthly_credits()
        user_data = CustomUserSerializer(user).data

        return Response({**user_data}, status=status.HTTP_200_OK)


class DeviceIDView(APIView):
    """API to manage device IDs for push notifications."""
    authentication_classes = [JWTAuthentication, ]
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
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        intervals = [{"interval": key, "monthly_charge": value["monthly_charge"]} for key, value in INTERVAL_MAP.items()]
        return Response({
            "symbols": list(SYMBOLS.keys()),
            "intervals": intervals,
            "backtest_base_cost": BACKTEST_BASE_COST,
            "backtest_iterations": BACKTEST_ITERATIONS
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

                result, attempts = BacktestService.conduct_backtest(symbol, interval, start_date, end_date, signals)
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

                img_base64, img_base64_no_signals = BacktestService.generate_backtest_chart(result)
                percent_return, percent_return_adj = BacktestService.calculate_percent_return(result)
                sharpe_ratio, sharpe_ratio_adj = BacktestService.calculate_sharpe_ratio(result)
                sortino_ratio, sortino_ratio_adj = BacktestService.calculate_sortino_ratio(result)

                duration_minutes = (result.iloc[-1]["datetime"] - result.iloc[0]["datetime"]).total_seconds() / 60
                minutes_per_year = 525600
                annualize_factor = minutes_per_year / duration_minutes

                seconds_per_year = minutes_per_year * 60
                seconds_per_year_adj = seconds_per_year * SYMBOLS[symbol] / (365 * 24)
                periods_per_year = seconds_per_year_adj / INTERVAL_MAP[interval]["seconds"]
                sqrt_n = math.sqrt(periods_per_year)

                percent_return = percent_return * annualize_factor
                percent_return_adj = percent_return_adj * annualize_factor
                sharpe_ratio = sharpe_ratio * sqrt_n
                sharpe_ratio_adj = sharpe_ratio_adj * sqrt_n
                sortino_ratio = sortino_ratio * sqrt_n
                sortino_ratio_adj = sortino_ratio_adj * sqrt_n

                user = request.user
                amount = BACKTEST_BASE_COST * attempts
                sufficient_funds, account_credits, monthly_credits = BacktestService.charge_user(user, amount)

                if not sufficient_funds:
                    return Response({"detail": f"Requires ${amount}"}, status=status.HTTP_402_PAYMENT_REQUIRED)

                response_data = {
                    "result": processed_result,
                    "image": img_base64,
                    "image_no_signals": img_base64_no_signals,
                    "percent_return": percent_return,
                    "percent_return_adj": percent_return_adj,
                    "sharpe_ratio": sharpe_ratio,
                    "sharpe_ratio_adj": sharpe_ratio_adj,
                    "sortino_ratio": sortino_ratio,
                    "sortino_ratio_adj": sortino_ratio_adj,
                    "user_account_credits": account_credits,
                    "user_monthly_credits": monthly_credits
                }

                return Response(response_data, status=status.HTTP_200_OK)
            except Exception as e:
                print(f"Error: {e}")
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
                strategy.save()
                strategy.add_signals(signals)

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
            from_email=EMAIL_HOST,
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
            if amount < 300:
                raise
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
                "payment_intent": payment_intent.client_secret,
                "ephemeral_key": ephemeral_key.secret,
                "customer": customer["id"]
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
        payload = request.body
        sig_header = request.headers['STRIPE_SIGNATURE']

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            return Response({"detail": f"Value error: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except stripe.error.SignatureVerificationError as e:
            return Response({"detail": f"Stripe error: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if event['type'] == 'payment_intent.succeeded':
            payment_intent = event['data']['object']
            payment_intent_id = payment_intent['id']

            try:
                saved_payment_intent = PaymentIntent.objects.get(payment_intent_id=payment_intent_id)
                user = saved_payment_intent.user
                amount = saved_payment_intent.amount

                user.add_funds(amount)
                user.save()
                saved_payment_intent.delete()
                print("Success")

                return Response({"message": "Payment successful, funds added."}, status=status.HTTP_200_OK)
            except ObjectDoesNotExist:
                return Response({"detail": "PaymentIntent not found."}, status=status.HTTP_404_NOT_FOUND)
        else:
            return Response({"message": "Other event type"}, status=status.HTTP_200_OK)


class RevenueCatDepositView(APIView):
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        user = request.user
        data = request.data
        transaction_id = data.get("transaction_id")

        try:
            deposit = RevenueCatDeposit.objects.get(transaction_id=transaction_id)

        except ObjectDoesNotExist:
            deposit = RevenueCatDeposit(
                user=user,
                transaction_id=transaction_id
            )
            deposit.save()

        if deposit.added:
            return Response({"detail": "Transaction already processed"}, status=status.HTTP_409_CONFLICT)

        try:
            confirmation = RevenueCatConfirmation.objects.get(transaction_id=transaction_id)
            if confirmation.added:
                deposit.set_added()
                return Response({"detail": "Transaction already processed"}, status=status.HTTP_409_CONFLICT)

            user.add_funds(confirmation.transaction_credits)
            deposit.set_added()
            confirmation.set_added()
            return Response({"message": "Deposit completed successfully"}, status=status.HTTP_200_OK)

        except ObjectDoesNotExist:
            return Response({"message": "Deposit created but not completed"}, status=status.HTTP_201_CREATED)

        except Exception as e:
            print(e)
            return Response({"detail": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RevenueCatConfirmationView(APIView):
    def post(self, request):
        data = request.data
        event = data.get("event")
        product_id = event["product_id"]
        transaction_id = event["transaction_id"]

        if product_id == "FundsDeposit":
            transaction_credits = 500
        elif product_id == "10AlgoTrackerDeposit":
            transaction_credits = 1000
        elif product_id == "100AlgoTrackerDeposit":
            transaction_credits = 10000
        elif product_id == "20AlgoTrackerDeposit":
            transaction_credits = 2000
        elif product_id == "50AlgoTrackerDeposit":
            transaction_credits = 5000
        else:
            return Response({"detail": "Did not recognize product ID"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            confirmation = RevenueCatConfirmation.objects.get(transaction_id=transaction_id)

        except ObjectDoesNotExist:
            confirmation = RevenueCatConfirmation(
                transaction_id=transaction_id,
                transaction_credits=transaction_credits
            )
            confirmation.save()

        if confirmation.added:
            return Response({"detail": "Transaction already processed"}, status=status.HTTP_409_CONFLICT)

        try:
            deposit = RevenueCatDeposit.objects.get(transaction_id=transaction_id)
            if deposit.added:
                confirmation.set_added()
                return Response({"detail": "Transaction already processed"}, status=status.HTTP_409_CONFLICT)

            deposit.user.add_funds(confirmation.transaction_credits)
            deposit.set_added()
            confirmation.set_added()
            return Response({"message": "Deposit completed successfully"}, status=status.HTTP_200_OK)

        except ObjectDoesNotExist:
            return Response({"message": "Confirmation created but not completed"}, status=status.HTTP_200_OK)

        except Exception as e:
            print(e)
            return Response({"detail": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
