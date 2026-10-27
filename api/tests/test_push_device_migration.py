import importlib
from datetime import timedelta

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from api.models import CustomUser, PushDevice


def make_user(name, **kwargs):
    return CustomUser.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", first_name="U", last_name="U", **kwargs)


class CopyDeviceIdsMigrationTests(TestCase):
    def copy(self):
        importlib.import_module("api.migrations.0023_copy_device_ids").copy_device_ids(apps, None)

    def owners(self):
        return {d.token: d.user.username for d in PushDevice.objects.select_related("user")}

    def test_each_list_becomes_rows(self):
        make_user("a", device_ids=["t1", "t2"])
        make_user("b", device_ids=["t3"])
        make_user("c")
        self.copy()
        self.assertEqual(self.owners(), {"t1": "a", "t2": "a", "t3": "b"})

    def test_a_token_on_two_accounts_goes_to_the_most_recent_sign_in(self):
        now = timezone.now()
        make_user("old", device_ids=["shared"], last_login=now - timedelta(days=5))
        make_user("recent", device_ids=["shared"], last_login=now)
        make_user("never", device_ids=["shared"])
        self.copy()
        self.assertEqual(self.owners(), {"shared": "recent"})

    def test_junk_entries_are_skipped(self):
        make_user("a", device_ids=["", None, 5, "ok", "ok"])
        self.copy()
        self.assertEqual(self.owners(), {"ok": "a"})
