import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def copy_device_ids(apps, schema_editor):
    """Turn each user's `device_ids` list into PushDevice rows. A token listed on two accounts goes to the one
    that signed in most recently (never-signed-in accounts lose to any that did, then the newer account wins);
    each conflict is logged."""
    CustomUser = apps.get_model("api", "CustomUser")
    PushDevice = apps.get_model("api", "PushDevice")

    def recency(user):
        return (user.last_login is not None, user.last_login or user.account_created, user.pk)

    owners = {}
    for user in CustomUser.objects.all().iterator():
        if not isinstance(user.device_ids, list):
            continue
        for token in user.device_ids:
            if not isinstance(token, str) or not token:
                continue
            current = owners.get(token)
            if current is None:
                owners[token] = user
            else:
                winner = max(current, user, key=recency)
                logger.warning("Push token on users %s and %s; keeping user %s", current.pk, user.pk, winner.pk)
                owners[token] = winner
    PushDevice.objects.bulk_create([PushDevice(user=user, token=token) for token, user in owners.items()])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0022_pushdevice'),
    ]

    operations = [
        # Going back leaves the (never cleared) JSON lists in place, so nothing needs undoing.
        migrations.RunPython(copy_device_ids, migrations.RunPython.noop),
    ]
