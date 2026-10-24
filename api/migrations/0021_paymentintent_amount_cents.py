from django.db import migrations


def dollars_to_cents(apps, schema_editor):
    """Payments that were started but not yet confirmed stored dollars (truncated); keep them paying
    what they were going to: one dollar was one credit, so they are now worth 100 credits a dollar."""
    PaymentIntent = apps.get_model("api", "PaymentIntent")
    for row in PaymentIntent.objects.all():
        row.amount_cents = row.amount_cents * 100
        row.save(update_fields=["amount_cents"])


def cents_to_dollars(apps, schema_editor):
    PaymentIntent = apps.get_model("api", "PaymentIntent")
    for row in PaymentIntent.objects.all():
        row.amount_cents = row.amount_cents // 100
        row.save(update_fields=["amount_cents"])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0020_credittransaction'),
    ]

    operations = [
        migrations.RenameField(
            model_name='paymentintent',
            old_name='amount',
            new_name='amount_cents',
        ),
        migrations.RunPython(dollars_to_cents, cents_to_dollars),
    ]
