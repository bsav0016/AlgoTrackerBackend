import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0029_leaderboard_name'),
    ]

    operations = [
        # Strategies that already exist get no start date: when they began cannot be known, and a made-up date
        # would let their earlier returns count as if they were new. They do not rank on the leaderboard.
        migrations.AddField(
            model_name='strategy',
            name='started_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        # From here on, new strategies start now.
        migrations.AlterField(
            model_name='strategy',
            name='started_at',
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, null=True),
        ),
    ]
