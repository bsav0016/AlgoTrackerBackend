"""Credit purchases. Stripe API calls are mocked; webhook signatures are verified for real."""
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest import mock

import stripe
from django.conf import settings
from rest_framework import status
from rest_framework.test import APITestCase

from api.tests.helpers import ledger
from api.models import CreditTransaction, CustomUser, PaymentIntent, RevenueCatConfirmation, RevenueCatCustomer


def make_user(username="alice"):
    return CustomUser.objects.create_user(
        username=username, email=f"{username}@example.com", password="x", first_name="A", last_name="B")


def credits_of(user):
    user.refresh_from_db()
    return user.account_credits


class StripePaymentIntentTests(APITestCase):
    url = "/api/payment-intent/"

    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)
        base = "api.views.stripe"
        patches = {
            "customer": mock.patch(f"{base}.Customer.create", return_value={"id": "cus_1"}),
            "key": mock.patch(f"{base}.EphemeralKey.create", return_value=SimpleNamespace(secret="ek_1")),
            "intent": mock.patch(f"{base}.PaymentIntent.create",
                                 return_value=SimpleNamespace(id="pi_1", client_secret="pi_1_secret")),
        }
        self.mocks = {name: patcher.start() for name, patcher in patches.items()}
        for patcher in patches.values():
            self.addCleanup(patcher.stop)

    def test_creates_a_payment_intent_and_remembers_it_for_the_webhook(self):
        response = self.client.post(self.url, {"amount": 500}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data, {"payment_intent": "pi_1_secret", "ephemeral_key": "ek_1",
                                         "customer": "cus_1"})
        self.mocks["intent"].assert_called_once_with(amount=500, currency="usd", customer="cus_1")
        saved = PaymentIntent.objects.get()
        self.assertEqual((saved.user, saved.payment_intent_id, saved.amount_cents), (self.user, "pi_1", 500))
        self.assertEqual(credits_of(self.user), 0)  # credits only arrive via the confirmed webhook

    def test_amounts_below_the_minimum_or_malformed_are_rejected_without_calling_stripe(self):
        for amount in (299, 0, -500, "abc", None):
            with self.subTest(amount=amount):
                response = self.client.post(self.url, {"amount": amount}, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.mocks["intent"].assert_not_called()
        self.assertFalse(PaymentIntent.objects.exists())

    def test_minimum_amount_is_accepted(self):
        self.assertEqual(self.client.post(self.url, {"amount": 300}, format="json").status_code, 201)

    def test_stripe_errors_become_402_and_nothing_is_saved(self):
        self.mocks["intent"].side_effect = stripe.StripeError("card network down")
        response = self.client.post(self.url, {"amount": 500}, format="json")
        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertFalse(PaymentIntent.objects.exists())


def signed_webhook(event, secret=None, timestamp=None):
    """Returns (body, headers) signed exactly as Stripe signs webhooks."""
    body = json.dumps(event)
    timestamp = timestamp or int(time.time())
    secret = secret or settings.STRIPE_WEBHOOK_SECRET
    signature = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return body, {"HTTP_STRIPE_SIGNATURE": f"t={timestamp},v1={signature}"}


def succeeded_event(intent_id):
    return {"id": "evt_1", "object": "event", "type": "payment_intent.succeeded",
            "data": {"object": {"id": intent_id, "object": "payment_intent"}}}


class StripeWebhookTests(APITestCase):
    url = "/api/payment-confirmation/"

    def setUp(self):
        self.user = make_user()
        PaymentIntent.objects.create(user=self.user, payment_intent_id="pi_1", amount_cents=500)

    def post(self, body, headers):
        return self.client.post(self.url, body, content_type="application/json", **headers)

    def test_successful_payment_credits_the_user_once(self):
        body, headers = signed_webhook(succeeded_event("pi_1"))
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 500)  # $5.00 at 100 credits a dollar
        self.assertFalse(PaymentIntent.objects.exists())

        row = ledger().get()
        self.assertEqual((row.idempotency_key, row.kind, row.amount, row.bucket),
                         ("stripe:pi_1", "purchase", 500, "account"))
        self.assertEqual(row.meta["cents"], 500)

        # Stripe retries deliveries: the same event again must not credit twice, and is acknowledged.
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 500)
        self.assertEqual(ledger().count(), 1)

    def test_an_amount_that_is_not_whole_dollars_is_credited_in_full(self):
        PaymentIntent.objects.create(user=self.user, payment_intent_id="pi_odd", amount_cents=350)
        body, headers = signed_webhook(succeeded_event("pi_odd"))
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 350)  # $3.50, not 3

    def test_the_rate_matches_the_revenuecat_products(self):
        from api.credits import credits_for_cents
        self.assertEqual(credits_for_cents(1000), 1000)    # $10 -> 1000, like 10AlgoTrackerDeposit
        self.assertEqual(credits_for_cents(10000), 10000)  # $100 -> 10000, like 100AlgoTrackerDeposit

    def test_a_payment_intent_that_was_never_created_is_still_404(self):
        body, headers = signed_webhook(succeeded_event("pi_other"))
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(ledger().exists())

    def test_bad_signature_is_rejected_and_nothing_is_credited(self):
        body, headers = signed_webhook(succeeded_event("pi_1"), secret="whsec_attacker")
        response = self.post(body, headers)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(credits_of(self.user), 0)
        self.assertTrue(PaymentIntent.objects.exists())

    def test_tampered_body_is_rejected(self):
        body, headers = signed_webhook(succeeded_event("pi_1"))
        self.assertGreaterEqual(self.post(body.replace("pi_1", "pi_2"), headers).status_code, 400)
        self.assertEqual(credits_of(self.user), 0)

    def test_unknown_payment_intent_is_404(self):
        body, headers = signed_webhook(succeeded_event("pi_unknown"))
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_404_NOT_FOUND)

    def test_other_event_types_are_acknowledged_without_crediting(self):
        event = {**succeeded_event("pi_1"), "type": "payment_intent.created"}
        body, headers = signed_webhook(event)
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 0)


def revenuecat_event(transaction_id="tx1", product_id="10AlgoTrackerDeposit", customer="rc_customer"):
    return {"event": {"product_id": product_id, "transaction_id": transaction_id, "app_user_id": customer}}


WEBHOOK_AUTH = "test-revenuecat-webhook-secret"  # matches REVENUECAT_WEBHOOK_AUTH in test_settings


class RevenueCatTests(APITestCase):
    confirm_url = "/api/revenuecat-confirmation/"
    customer_url = "/api/revenuecat-customer/"

    def setUp(self):
        self.user = make_user()
        self.client.credentials(HTTP_AUTHORIZATION=WEBHOOK_AUTH)  # what RevenueCat sends

    def register_customer(self, customer_id="rc_customer"):
        self.client.force_authenticate(self.user)
        response = self.client.post(self.customer_url, {"customer_id": customer_id}, format="json")
        self.client.force_authenticate(None)  # also clears credentials, so set the webhook header again
        self.client.credentials(HTTP_AUTHORIZATION=WEBHOOK_AUTH)
        return response

    def test_purchase_after_the_customer_is_registered_credits_immediately(self):
        self.register_customer()
        response = self.client.post(self.confirm_url, revenuecat_event(), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 1000)
        self.assertFalse(RevenueCatConfirmation.objects.exists())

    def test_purchase_before_the_customer_is_registered_is_credited_on_registration(self):
        response = self.client.post(self.confirm_url, revenuecat_event(), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 0)
        self.assertEqual(RevenueCatConfirmation.objects.count(), 1)

        registered = self.register_customer()
        self.assertEqual(registered.status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 1000)
        self.assertFalse(RevenueCatConfirmation.objects.exists())

    def test_registering_without_a_pending_purchase_creates_the_customer_only(self):
        response = self.register_customer()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(RevenueCatCustomer.objects.get().user, self.user)
        self.assertEqual(credits_of(self.user), 0)

    def test_each_product_grants_its_credit_amount(self):
        self.register_customer()
        products = {"FundsDeposit": 500, "10AlgoTrackerDeposit": 1000, "20AlgoTrackerDeposit": 2000,
                    "50AlgoTrackerDeposit": 5000, "100AlgoTrackerDeposit": 10000}
        expected = 0
        for index, (product, credits) in enumerate(products.items()):
            with self.subTest(product=product):
                expected += credits
                response = self.client.post(
                    self.confirm_url, revenuecat_event(transaction_id=f"tx{index}", product_id=product),
                    format="json")
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(credits_of(self.user), expected)

    def test_unknown_product_is_rejected_and_not_credited(self):
        self.register_customer()
        response = self.client.post(self.confirm_url, revenuecat_event(product_id="Mystery"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(credits_of(self.user), 0)

    def test_redelivered_webhook_does_not_credit_twice(self):
        self.register_customer()
        for _ in range(3):
            response = self.client.post(self.confirm_url, revenuecat_event(), format="json")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 1000)
        row = ledger().get()
        self.assertEqual((row.idempotency_key, row.kind, row.amount), ("revenuecat:tx1", "purchase", 1000))

    def test_redelivery_while_the_purchase_is_still_waiting_for_the_customer_keeps_one_pending_row(self):
        for _ in range(2):
            self.client.post(self.confirm_url, revenuecat_event(), format="json")
        self.assertEqual(RevenueCatConfirmation.objects.count(), 1)
        self.assertEqual(credits_of(self.user), 0)
        self.assertFalse(ledger().exists())

    def test_redelivery_after_a_pending_purchase_was_claimed_does_not_credit_again(self):
        self.client.post(self.confirm_url, revenuecat_event(), format="json")
        self.register_customer()
        self.assertEqual(credits_of(self.user), 1000)
        self.client.post(self.confirm_url, revenuecat_event(), format="json")
        self.register_customer()
        self.assertEqual(credits_of(self.user), 1000)
        self.assertEqual(ledger().count(), 1)
        self.assertFalse(RevenueCatConfirmation.objects.exists())

    def test_two_purchases_waiting_for_registration_are_each_credited_once(self):
        self.client.post(self.confirm_url, revenuecat_event(transaction_id="a"), format="json")
        self.client.post(self.confirm_url, revenuecat_event(transaction_id="b", product_id="FundsDeposit"),
                         format="json")
        self.register_customer()
        self.register_customer()
        self.assertEqual(credits_of(self.user), 1500)
        self.assertEqual(sorted(ledger().values_list("idempotency_key", flat=True)),
                         ["revenuecat:a", "revenuecat:b"])

    def test_a_customer_id_registered_to_another_account_cannot_be_claimed(self):
        self.register_customer("rc_customer")
        RevenueCatConfirmation.objects.create(transaction_id="parked", transaction_credits=1000,
                                              customer_id="rc_customer")
        intruder = make_user("mallory")
        self.client.force_authenticate(intruder)
        response = self.client.post(self.customer_url, {"customer_id": "rc_customer"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(credits_of(intruder), 0)
        self.assertTrue(RevenueCatConfirmation.objects.filter(transaction_id="parked").exists())

    def test_registering_without_a_customer_id_is_a_400(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(self.customer_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RevenueCatWebhookAuthTests(APITestCase):
    """Anyone who can reach the server can POST to the webhook URL, so the shared secret is the
    only thing standing between a forged "purchase" and free credits."""
    confirm_url = "/api/revenuecat-confirmation/"
    customer_url = "/api/revenuecat-customer/"

    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)
        self.client.post(self.customer_url, {"customer_id": "rc_customer"}, format="json")
        self.client.force_authenticate(None)

    def forge(self, **extra):
        return self.client.post(self.confirm_url, revenuecat_event(product_id="100AlgoTrackerDeposit"),
                                format="json", **extra)

    def assert_nothing_happened(self, response):
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(credits_of(self.user), 0)
        self.assertFalse(RevenueCatConfirmation.objects.exists())

    def test_request_without_the_secret_is_rejected(self):
        self.assert_nothing_happened(self.forge())

    def test_wrong_secret_is_rejected(self):
        for value in ["wrong", WEBHOOK_AUTH + "x", WEBHOOK_AUTH[:-1], WEBHOOK_AUTH.upper(), " " + WEBHOOK_AUTH, ""]:
            with self.subTest(value=value):
                self.assert_nothing_happened(self.forge(HTTP_AUTHORIZATION=value))

    def test_bearer_prefix_is_not_accepted_unless_it_is_part_of_the_configured_value(self):
        self.assert_nothing_happened(self.forge(HTTP_AUTHORIZATION="Bearer " + WEBHOOK_AUTH))

    def test_a_logged_in_users_jwt_does_not_authorize_the_webhook(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(self.user).access_token)
        self.assert_nothing_happened(self.forge(HTTP_AUTHORIZATION=f"Bearer {token}"))

    def test_correct_secret_credits_the_account(self):
        response = self.forge(HTTP_AUTHORIZATION=WEBHOOK_AUTH)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 10000)

    def test_fails_closed_when_no_secret_is_configured(self):
        with self.settings(REVENUECAT_WEBHOOK_AUTH=""):
            for header in [{}, {"HTTP_AUTHORIZATION": ""}]:
                with self.subTest(header=header):
                    self.assert_nothing_happened(self.forge(**header))

    def test_malformed_events_from_a_trusted_sender_are_400_not_500(self):
        self.client.credentials(HTTP_AUTHORIZATION=WEBHOOK_AUTH)
        for payload in [{}, {"event": None}, {"event": "x"}, {"event": {}}, {"event": {"product_id": "FundsDeposit"}}]:
            with self.subTest(payload=payload):
                response = self.client.post(self.confirm_url, payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(credits_of(self.user), 0)
