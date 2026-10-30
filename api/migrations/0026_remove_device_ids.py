from django.db import migrations


class Migration(migrations.Migration):
    """Drops the old JSON list of push tokens. 0023 copied every token into PushDevice rows, and nothing reads or
    writes the list any more. Going back restores an empty list."""

    dependencies = [
        ('api', '0025_delivered_status'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='customuser',
            name='device_ids',
        ),
    ]
