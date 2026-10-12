"""Credit purchases. Stripe API calls are mocked; webhook signatures are verified for real."""
import hashlib
import hmac
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import stripe
from django.conf import settings
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import CustomUser, PaymentIntent, RevenueCatConfirmation, RevenueCatCustomer


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
        self.assertEqual((saved.user, saved.payment_intent_id, saved.amount), (self.user, "pi_1", 5))
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
        PaymentIntent.objects.create(user=self.user, payment_intent_id="pi_1", amount=5)

    def post(self, body, headers):
        return self.client.post(self.url, body, content_type="application/json", **headers)

    def test_successful_payment_credits_the_user_once(self):
        body, headers = signed_webhook(succeeded_event("pi_1"))
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_200_OK)
        self.assertEqual(credits_of(self.user), 5)
        self.assertFalse(PaymentIntent.objects.exists())

        # Stripe retries deliveries: the same event again must not credit twice.
        self.assertEqual(self.post(body, headers).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(credits_of(self.user), 5)

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


class RevenueCatTests(APITestCase):
    confirm_url = "/api/revenuecat-confirmation/"
    customer_url = "/api/revenuecat-customer/"

    def setUp(self):
        self.user = make_user()

    def register_customer(self, customer_id="rc_customer"):
        self.client.force_authenticate(self.user)
        response = self.client.post(self.customer_url, {"customer_id": customer_id}, format="json")
        self.client.force_authenticate(None)
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

    @unittest.expectedFailure
    def test_redelivered_webhook_does_not_credit_twice(self):
        # KNOWN GAP: processed transactions are deleted, so RevenueCat retrying the same
        # transaction credits again. Fixing needs a record of processed transaction ids (a migration).
        self.register_customer()
        for _ in range(2):
            self.client.post(self.confirm_url, revenuecat_event(), format="json")
        self.assertEqual(credits_of(self.user), 1000)
