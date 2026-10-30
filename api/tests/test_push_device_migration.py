from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

BEFORE = [("api", "0022_pushdevice")]
COPIED = [("api", "0023_copy_device_ids")]


class CopyDeviceIdsMigrationTests(TransactionTestCase):
    """Runs the real 0023 migration between the historical models, since `device_ids` no longer exists on the
    current user model."""

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Leave the test database at the latest migration for whatever runs next.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def make_user(self, apps, name, **kwargs):
        return apps.get_model("api", "CustomUser").objects.create(
            username=name, email=f"{name}@example.com", first_name="U", last_name="U", **kwargs)

    def owners(self, apps):
        return {d.token: d.user.username for d in apps.get_model("api", "PushDevice").objects.select_related("user")}

    def test_each_list_becomes_rows(self):
        old = self.migrate(BEFORE)
        self.make_user(old, "a", device_ids=["t1", "t2"])
        self.make_user(old, "b", device_ids=["t3"])
        self.make_user(old, "c")
        self.assertEqual(self.owners(self.migrate(COPIED)), {"t1": "a", "t2": "a", "t3": "b"})

    def test_a_token_on_two_accounts_goes_to_the_most_recent_sign_in(self):
        old = self.migrate(BEFORE)
        now = timezone.now()
        self.make_user(old, "old", device_ids=["shared"], last_login=now - timedelta(days=5))
        self.make_user(old, "recent", device_ids=["shared"], last_login=now)
        self.make_user(old, "never", device_ids=["shared"])
        self.assertEqual(self.owners(self.migrate(COPIED)), {"shared": "recent"})

    def test_junk_entries_are_skipped(self):
        old = self.migrate(BEFORE)
        self.make_user(old, "a", device_ids=["", None, 5, "ok", "ok"])
        self.assertEqual(self.owners(self.migrate(COPIED)), {"ok": "a"})

    def test_dropping_the_list_keeps_the_copied_devices(self):
        old = self.migrate(BEFORE)
        self.make_user(old, "a", device_ids=["t1"])
        self.migrate(COPIED)
        latest = self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        self.assertEqual(self.owners(latest), {"t1": "a"})
        self.assertNotIn("device_ids", [f.name for f in latest.get_model("api", "CustomUser")._meta.get_fields()])
