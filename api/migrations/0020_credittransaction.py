from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def write_opening_balances(apps, schema_editor):
    """Give every existing balance a starting ledger row so the ledger sums to the cached totals."""
    CustomUser = apps.get_model("api", "CustomUser")
    CreditTransaction = apps.get_model("api", "CreditTransaction")
    rows = []
    for user in CustomUser.objects.all().iterator():
        for bucket, balance in (("account", user.account_credits), ("monthly", user.monthly_credits)):
            if balance:
                rows.append(CreditTransaction(
                    user=user, bucket=bucket, amount=balance, balance_after=balance, kind="adjustment",
                    idempotency_key=f"opening:{user.pk}", meta={"reason": "opening balance"}))
    CreditTransaction.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0019_strategy_last_processed_ts'),
    ]

    operations = [
        migrations.CreateModel(
            name='CreditTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bucket', models.CharField(choices=[('account', 'Account credits'), ('monthly', 'Monthly credits')], max_length=10)),
                ('amount', models.IntegerField()),
                ('balance_after', models.IntegerField()),
                ('kind', models.CharField(choices=[('purchase', 'Purchase'), ('backtest', 'Backtest'), ('subscription', 'Strategy subscription'), ('monthly_grant', 'Monthly grant'), ('promo', 'Promo code'), ('adjustment', 'Adjustment')], max_length=20)),
                ('idempotency_key', models.CharField(max_length=128)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('meta', models.JSONField(blank=True, default=dict)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='credit_transactions', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(
            model_name='credittransaction',
            index=models.Index(fields=['user', '-id'], name='credit_tx_user_recent'),
        ),
        migrations.AddConstraint(
            model_name='credittransaction',
            constraint=models.UniqueConstraint(fields=('idempotency_key', 'bucket'), name='unique_credit_event_per_bucket'),
        ),
        migrations.RunPython(write_opening_balances, migrations.RunPython.noop),
    ]
