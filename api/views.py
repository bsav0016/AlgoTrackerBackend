import hmac
import logging
import math
import time
import uuid
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.conf import settings
from django.db import connection, transaction
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from api.serializers import (UserRegistrationSerializer, LoginSerializer, CustomUserSerializer,
                             CreditTransactionSerializer, NotificationSerializer,
                             BacktestRequestSerializer, StrategyRequestSerializer)
from api import credits
from api.models import (Strategy, Notification, PushDevice, CustomUser, CreditTransaction, PaymentIntent, RevenueCatCustomer,
                        RevenueCatConfirmation)
from api.push_sweep import backlog as push_backlog
from api.services.auth_service import AuthService
from api.services.backtest_service import BacktestService
from api.indicators.signal import Signal
from rest_framework_simplejwt.authentication import JWTAuthentication
import secrets
import numpy as np
from django.core.mail import send_mail
from api.redis_manager import RedisManager
import stripe
from stripe import StripeError

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

MAX_OTP_ATTEMPTS = 5  # wrong guesses allowed per password-reset code
INTERVAL_MAP = settings.INTERVAL_MAP
BACKTEST_BASE_COST = settings.BACKTEST_BASE_COST
SYMBOLS = settings.SYMBOLS
EMAIL_HOST = settings.EMAIL_HOST
STRIPE_WEBHOOK_SECRET = settings.STRIPE_WEBHOOK_SECRET
BACKTEST_ITERATIONS = settings.BACKTEST_ITERATIONS
PROMO_CODES = settings.PROMO_CODES
PROMO_CODE_AVAILABILITY = settings.PROMO_CODE_AVAILABILITY


def unsupported_market_response(symbol, interval):
    """A 400 response when the symbol or interval isn't one this server is configured for, else None."""
    if symbol not in SYMBOLS or interval not in INTERVAL_MAP:
        return Response({"detail": "Unsupported symbol or interval"}, status=status.HTTP_400_BAD_REQUEST)
    return None


def finite_or_zero(value):
    """JSON can't carry NaN/inf, so undefined statistics are sent as 0 (as NaNs in the result rows are)."""
    return value if math.isfinite(value) else 0


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
                credits.apply(user.pk, {"account": promo_code_value}, f"promo:{user.pk}", "promo",
                              meta={"code": promo_code})
                user.refresh_from_db()

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
            logger.exception("Error registering user")
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


class CreditHistoryView(APIView):
    """The signed-in user's own credit history, newest first, a page at a time.

    `?limit=` (1 to 100, default 50) sizes the page; pass the previous response's `next_before` as `?before=`
    to get the next one. `next_before` is null on the last page.
    """
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]
    DEFAULT_LIMIT = 50
    MAX_LIMIT = 100

    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", self.DEFAULT_LIMIT))
            before = request.query_params.get("before")
            before = int(before) if before is not None else None
        except ValueError:
            return Response({"detail": "limit and before must be whole numbers"}, status=status.HTTP_400_BAD_REQUEST)
        limit = max(1, min(limit, self.MAX_LIMIT))

        rows = CreditTransaction.objects.filter(user=request.user).order_by("-id")
        if before is not None:
            rows = rows.filter(id__lt=before)
        page = list(rows[:limit + 1])
        has_more = len(page) > limit
        page = page[:limit]
        return Response({
            "results": CreditTransactionSerializer(page, many=True).data,
            "next_before": page[-1].id if has_more else None,
        }, status=status.HTTP_200_OK)


class NotificationHistoryView(APIView):
    """The signed-in user's own signal notifications, newest first, a page at a time (same paging as credit history).

    `?limit=` (1 to 100, default 50) sizes the page; pass the previous response's `next_before` as `?before=`.
    """
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]
    DEFAULT_LIMIT = 50
    MAX_LIMIT = 100

    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", self.DEFAULT_LIMIT))
            before = request.query_params.get("before")
            before = int(before) if before is not None else None
        except ValueError:
            return Response({"detail": "limit and before must be whole numbers"}, status=status.HTTP_400_BAD_REQUEST)
        limit = max(1, min(limit, self.MAX_LIMIT))

        rows = Notification.objects.filter(user=request.user).select_related("trade").order_by("-id")
        if before is not None:
            rows = rows.filter(id__lt=before)
        page = list(rows[:limit + 1])
        has_more = len(page) > limit
        page = page[:limit]
        return Response({
            "results": NotificationSerializer(page, many=True).data,
            "next_before": page[-1].id if has_more else None,
        }, status=status.HTTP_200_OK)


class DeviceIDView(APIView):
    """API to manage device IDs for push notifications."""
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        """Register a push token for the signed-in account. A token already held by another account moves here."""
        device_id = request.data.get("device_id")

        if not device_id:
            return Response({"error": "Device ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        _, created = PushDevice.register(request.user, device_id, request.data.get("platform") or "")
        message = "Device ID added successfully" if created else "Device ID already exists"
        return Response({"message": message}, status=status.HTTP_200_OK)

    def delete(self, request):
        """Remove one of the signed-in account's push tokens."""
        device_id = request.data.get("device_id")

        if not device_id:
            return Response({"error": "Device ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        deleted, _ = PushDevice.objects.filter(user=request.user, token=device_id).delete()
        if deleted:
            return Response({"message": "Device ID removed successfully"}, status=status.HTTP_200_OK)
        return Response({"error": "Device ID not found"}, status=status.HTTP_404_NOT_FOUND)


class HealthView(APIView):
    """Liveness for the container healthcheck and monitoring: is the database up, is Redis up, and how long
    since the websocket worker last received a price. A quiet feed (a closed market) does not fail the check."""
    authentication_classes = []
    permission_classes = [AllowAny, ]

    def get(self, request):
        database = redis = True
        try:
            connection.ensure_connection()
        except Exception:
            logger.exception("Health check: database unavailable")
            database = False
        seconds_since_last_tick = None
        try:
            seconds_since_last_tick = RedisManager.seconds_since_last_tick(int(time.time()))
        except Exception:
            logger.exception("Health check: Redis unavailable")
            redis = False
        healthy = database and redis
        # Informational: a backlog does not fail the check, it shows in monitoring.
        pushes = {"pending_deliveries": None, "failed_deliveries_24h": None, "oldest_pending_seconds": None}
        if database:
            try:
                pushes = push_backlog()
            except Exception:
                logger.exception("Health check: push backlog unavailable")
        return Response({"status": "ok" if healthy else "unavailable", "database": database, "redis": redis,
                         "seconds_since_last_tick": seconds_since_last_tick, **pushes},
                        status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE)


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

                unsupported = unsupported_market_response(symbol, interval)
                if unsupported:
                    return unsupported

                signals = [Signal(**signal) for signal in signals]

                # Reserve the cost of one data request before spending any market-data quota; settle the
                # extra requests once we know how many it took, and give everything back if the run fails.
                user = request.user
                spend_key = f"backtest:{uuid.uuid4().hex}"
                base_cost = int(BACKTEST_BASE_COST)
                try:
                    balances = credits.spend(user.pk, base_cost, f"{spend_key}:reserve", "backtest",
                                             meta={"symbol": symbol, "interval": interval})
                except credits.InsufficientCredits:
                    return Response({"detail": f"Requires ${base_cost}"}, status=status.HTTP_402_PAYMENT_REQUIRED)

                try:
                    return self.run_and_settle(
                        user, spend_key, base_cost, balances, symbol, interval, start_date, end_date, signals)
                except Exception:
                    for suffix in ("reserve", "settle"):
                        credits.refund(user.pk, f"{spend_key}:{suffix}", f"{spend_key}:{suffix}:refund", "backtest")
                    raise
            except Exception as e:
                logger.exception("Error building response")
                return Response({"detail": "Internal Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def run_and_settle(self, user, spend_key, base_cost, balances, symbol, interval, start_date, end_date, signals):
        result, attempts = BacktestService.conduct_backtest(symbol, interval, start_date, end_date, signals)
        extra_cost = base_cost * (attempts - 1)
        if extra_cost > 0:
            try:
                balances = credits.spend(user.pk, extra_cost, f"{spend_key}:settle", "backtest",
                                         meta={"symbol": symbol, "interval": interval, "attempts": attempts})
            except credits.InsufficientCredits:
                credits.refund(user.pk, f"{spend_key}:reserve", f"{spend_key}:reserve:refund", "backtest")
                return Response({"detail": f"Requires ${base_cost * attempts}"},
                                status=status.HTTP_402_PAYMENT_REQUIRED)

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

        percent_return = finite_or_zero(percent_return * annualize_factor)
        percent_return_adj = finite_or_zero(percent_return_adj * annualize_factor)
        sharpe_ratio = finite_or_zero(sharpe_ratio * sqrt_n)
        sharpe_ratio_adj = finite_or_zero(sharpe_ratio_adj * sqrt_n)
        sortino_ratio = finite_or_zero(sortino_ratio * sqrt_n)
        sortino_ratio_adj = finite_or_zero(sortino_ratio_adj * sqrt_n)

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
            "user_account_credits": balances.account,
            "user_monthly_credits": balances.monthly
        }

        return Response(response_data, status=status.HTTP_200_OK)


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

                unsupported = unsupported_market_response(symbol, interval)
                if unsupported:
                    return unsupported

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
                logger.exception("Strategy view internal error")
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
            logger.exception("Strategy lookup failed")
            return Response({"detail": "Internal error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PasswordResetRequestView(APIView):
    """Generate OTP and send it via email for password reset"""
    permission_classes = [AllowAny, ]

    def post(self, request):
        email = request.data.get("email")
        try:
            CustomUser.objects.get(email=email)
        except ObjectDoesNotExist:
            return Response({"detail": "User with email not found"}, status=status.HTTP_404_NOT_FOUND)

        otp = secrets.randbelow(900000) + 100000  # six digits

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

            if not stored_otp or str(stored_otp) != str(otp):
                # Only a live code can be guessed at; after too many wrong guesses it is thrown away, so the
                # six digits cannot be brute-forced inside the ten minutes the code lives for.
                if stored_otp and RedisManager.record_failed_otp_attempt(user.email) >= MAX_OTP_ATTEMPTS:
                    RedisManager.delete_otp(user.email)
                    return Response({"error": "Too many incorrect codes. Request a new code."},
                                    status=status.HTTP_429_TOO_MANY_REQUESTS)
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
                raise ValueError("Amount below minimum")
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

            saved_payment_intent = PaymentIntent(
                user=request.user,
                payment_intent_id=payment_intent.id,
                amount_cents=amount
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

            key = f"stripe:{payment_intent_id}"
            try:
                saved_payment_intent = PaymentIntent.objects.get(payment_intent_id=payment_intent_id)
            except ObjectDoesNotExist:
                # Stripe redelivers events. If this payment was already credited the ledger knows it.
                if CreditTransaction.objects.filter(idempotency_key=key).exists():
                    return Response({"message": "Payment already credited."}, status=status.HTTP_200_OK)
                return Response({"detail": "PaymentIntent not found."}, status=status.HTTP_404_NOT_FOUND)

            credits.apply(saved_payment_intent.user_id,
                          {"account": credits.credits_for_cents(saved_payment_intent.amount_cents)}, key,
                          "purchase", meta={"source": "stripe", "payment_intent": payment_intent_id,
                                            "cents": saved_payment_intent.amount_cents})
            saved_payment_intent.delete()
            return Response({"message": "Payment successful, funds added."}, status=status.HTTP_200_OK)
        else:
            return Response({"message": "Other event type"}, status=status.HTTP_200_OK)


def credit_revenuecat_purchase(user_id, confirmation):
    """Credit one store purchase. The key is the store transaction id, so a redelivery is a no-op."""
    return credits.apply(
        user_id, {"account": confirmation.transaction_credits}, f"revenuecat:{confirmation.transaction_id}",
        "purchase", meta={"source": "revenuecat", "customer_id": confirmation.customer_id})


class RevenueCatCustomerView(APIView):
    authentication_classes = [JWTAuthentication, ]
    permission_classes = [IsAuthenticated, ]

    def post(self, request):
        user = request.user
        data = request.data
        customer_id = data.get("customer_id")

        if not customer_id:
            return Response({"detail": "Missing customer_id"}, status=status.HTTP_400_BAD_REQUEST)

        customer, _ = RevenueCatCustomer.objects.get_or_create(
            customer_id=customer_id,
            defaults={'user': user}
        )
        if customer.user_id != user.pk:
            return Response({"detail": "This customer is registered to another account"},
                            status=status.HTTP_409_CONFLICT)

        confirmations = RevenueCatConfirmation.objects.filter(customer_id=customer_id)
        if not confirmations.exists():
            return Response({"message": "Customer created but transaction not completed"}, status=status.HTTP_201_CREATED)

        try:
            with transaction.atomic():
                for confirmation in list(confirmations):
                    credit_revenuecat_purchase(user.pk, confirmation)
                    confirmation.delete()
            return Response({"message": "Transaction completed successfully"}, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Payment confirmation failed")
            return Response({"detail": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# TODO(go-live): the production server is currently paused, so no webhook reaches this view. Before it
# goes live again, RevenueCat must be pointed at it with a matching secret:
#   1. Set REVENUECAT_WEBHOOK_AUTH in the server's .env to a long random value.
#   2. In RevenueCat (Integrations > Webhooks), set the webhook URL to this server's
#      /api/revenuecat-confirmation/ and the Authorization header value to the same string.
#      The webhook was last configured with an old Heroku URL that no longer responds.
#   3. Send a test event / make a sandbox purchase and confirm the credits arrive.
# Until the secret is set, every request is rejected on purpose.
def revenuecat_request_is_authorized(request):
    """True only if the Authorization header equals the configured webhook secret.

    Fails closed: with no secret configured nothing is authorized. The comparison is constant-time.
    """
    expected = settings.REVENUECAT_WEBHOOK_AUTH
    if not expected:
        return False
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class RevenueCatConfirmationView(APIView):
    # Called by RevenueCat's servers, not by a signed-in user, so JWT authentication does not apply;
    # the shared secret in the Authorization header is checked instead.
    authentication_classes = []
    permission_classes = [AllowAny, ]

    def post(self, request):
        if not revenuecat_request_is_authorized(request):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

        data = request.data
        event = data.get("event") if isinstance(data, dict) else None
        if not isinstance(event, dict):
            return Response({"detail": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)
        product_id = event.get("product_id")
        transaction_id = event.get("transaction_id")
        customer_id = event.get("app_user_id")

        if not all([product_id, transaction_id, customer_id]):
            return Response({"detail": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

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
            try:
                customer = RevenueCatCustomer.objects.get(customer_id=customer_id)
            except ObjectDoesNotExist:
                # The app has not registered this store customer yet: park the purchase until it does.
                RevenueCatConfirmation.objects.get_or_create(
                    transaction_id=transaction_id,
                    defaults={'transaction_credits': transaction_credits, 'customer_id': customer_id}
                )
                return Response({"message": "Confirmation created but not completed"}, status=status.HTTP_200_OK)

            with transaction.atomic():
                confirmation = RevenueCatConfirmation(
                    transaction_id=transaction_id, transaction_credits=transaction_credits,
                    customer_id=customer_id)
                credit_revenuecat_purchase(customer.user_id, confirmation)
                RevenueCatConfirmation.objects.filter(transaction_id=transaction_id).delete()
            return Response({"message": "Deposit completed successfully"}, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Failed to process RevenueCat confirmation")
            return Response({"detail": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
