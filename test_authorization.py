import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrim_state import ScrimRepository
from slot_storage import SlotStateStore


class GuildAuthorizationTests(unittest.TestCase):
    def test_authorization_is_persisted_and_revocable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()

            self.assertEqual(repository.list_authorized_guild_ids(), [])
            self.assertTrue(repository.authorize_guild(123))
            self.assertFalse(repository.authorize_guild(123))
            self.assertTrue(repository.is_guild_authorized(123))
            self.assertEqual(repository.payload()["authorized_guild_ids"], [123])

            reloaded = ScrimRepository(store)
            reloaded.load()
            self.assertEqual(reloaded.list_authorized_guild_ids(), [123])

            self.assertTrue(reloaded.revoke_guild(123))
            self.assertFalse(reloaded.is_guild_authorized(123))
            self.assertFalse(reloaded.revoke_guild(123))

    def test_authorization_rejects_invalid_guild_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.load()

            with self.assertRaises(ValueError):
                repository.authorize_guild(0)
            with self.assertRaises(ValueError):
                repository.revoke_guild(-1)

    def test_temporary_authorization_is_persisted_with_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()

            self.assertTrue(repository.authorize_guild(123, days=7))
            self.assertTrue(repository.is_guild_authorized(123))
            authorizations = repository.list_authorizations()
            self.assertEqual(len(authorizations), 1)
            self.assertIsNotNone(authorizations[0][1])
            self.assertGreater(
                authorizations[0][1],
                datetime.now(timezone.utc) + timedelta(days=6),
            )

            reloaded = ScrimRepository(store)
            reloaded.load()
            self.assertTrue(reloaded.is_guild_authorized(123))
            self.assertIsNotNone(reloaded.list_authorizations()[0][1])

    def test_expired_authorization_is_not_active(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.load()
            repository.authorize_guild(123, days=1)
            repository.authorized_guild_expires_at[123] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )

            self.assertFalse(repository.is_guild_authorized(123))
            self.assertEqual(repository.list_authorizations(), [])

    def test_unlimited_authorization_has_no_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.load()
            repository.authorize_guild(123)

            self.assertEqual(repository.list_authorizations(), [(123, None, 0)])

    def test_reauthorizing_replaces_and_renews_the_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.load()
            repository.authorize_guild(123, days=1)

            self.assertFalse(repository.authorize_guild(123, days=30))
            guild_id, expires_at, duration_days = repository.list_authorizations()[0]
            self.assertEqual(guild_id, 123)
            self.assertEqual(duration_days, 30)
            self.assertIsNotNone(expires_at)
            self.assertGreater(
                expires_at,
                datetime.now(timezone.utc) + timedelta(days=29),
            )

            self.assertFalse(repository.authorize_guild(123, days=0))
            self.assertEqual(repository.list_authorizations(), [(123, None, 0)])

    def test_existing_authorizations_migrate_to_unlimited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()
            repository.authorize_guild(123, days=None)
            legacy = repository.payload()
            legacy["version"] = 11
            legacy.pop("authorized_guild_duration_days")
            store.save(legacy)

            migrated = ScrimRepository(store)
            migrated.load()
            self.assertEqual(migrated.list_authorizations(), [(123, None, 0)])

    def test_authorized_admin_ids_are_persisted_and_revocable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()

            self.assertEqual(repository.list_authorized_admin_ids(), [])
            self.assertTrue(repository.authorize_admin(456))
            self.assertFalse(repository.authorize_admin(456))
            self.assertTrue(repository.is_admin_authorized(456))
            self.assertEqual(repository.payload()["authorized_admin_ids"], [456])

            reloaded = ScrimRepository(store)
            reloaded.load()
            self.assertEqual(reloaded.list_authorized_admin_ids(), [456])
            self.assertTrue(reloaded.revoke_admin(456))
            self.assertFalse(reloaded.is_admin_authorized(456))
            self.assertFalse(reloaded.revoke_admin(456))

    def test_authorized_admin_ids_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.load()

            with self.assertRaises(ValueError):
                repository.authorize_admin(0)
            with self.assertRaises(ValueError):
                repository.revoke_admin(-1)

    def test_existing_snapshots_migrate_without_admins(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()
            legacy = repository.payload()
            legacy["version"] = 12
            legacy.pop("authorized_admin_ids")
            store.save(legacy)

            migrated = ScrimRepository(store)
            migrated.load()
            self.assertEqual(migrated.list_authorized_admin_ids(), [])
            self.assertEqual(migrated.payload()["version"], 18)


if __name__ == "__main__":
    unittest.main()