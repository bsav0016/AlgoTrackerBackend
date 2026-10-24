import importlib

from django.apps import apps
from django.test import TestCase

from api.models import CustomUser, PaymentIntent


class PendingPaymentMigrationTests(TestCase):
    def test_pending_payments_stored_in_dollars_become_cents(self):
        user = CustomUser.objects.create_user(username="u", email="u@example.com", password="x",
                                              first_name="U", last_name="U")
        PaymentIntent.objects.create(user=user, payment_intent_id="pi_old", amount_cents=5)  # $5 as the old code saved it
        module = importlib.import_module("api.migrations.0021_paymentintent_amount_cents")
        module.dollars_to_cents(apps, None)
        self.assertEqual(PaymentIntent.objects.get().amount_cents, 500)
        module.cents_to_dollars(apps, None)
        self.assertEqual(PaymentIntent.objects.get().amount_cents, 5)
