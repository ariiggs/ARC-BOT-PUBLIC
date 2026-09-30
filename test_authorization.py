import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from arc_bot.commands import authorization as authorization_commands
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

    def test_authorization_listing_can_filter_tier_and_include_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.load()
            repository.authorize_guild(123, days=1, license_type="Standard")
            repository.authorize_guild(456, days=30, license_type="Gold")
            expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            repository.authorized_guild_expires_at[123] = expired_at

            self.assertEqual(
                repository.list_authorizations(
                    include_expired=True,
                    license_type="Standard",
                ),
                [(123, expired_at, 1)],
            )
            self.assertEqual(
                [
                    guild_id
                    for guild_id, _, _ in repository.list_authorizations(
                        license_type="Gold"
                    )
                ],
                [456],
            )

    def test_unlimited_authorization_has_no_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.load()
            repository.authorize_guild(123)

            self.assertEqual(repository.list_authorizations(), [(123, None, 0)])

    def test_diamond_authorization_is_local_and_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()

            repository.authorize_guild(123, days=30, license_type="Diamond")
            self.assertTrue(repository.is_guild_authorized(123))
            self.assertEqual(repository.get_server_license_type(123), "Diamond")

            reloaded = ScrimRepository(store)
            reloaded.load()
            self.assertEqual(
                reloaded.list_authorizations(license_type="Diamond"),
                repository.list_authorizations(license_type="Diamond"),
            )
            self.assertEqual(reloaded.get_server_license_type(123), "Diamond")

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
            self.assertEqual(migrated.payload()["version"], 34)


class AuthOwnerOnlyTests(unittest.IsolatedAsyncioTestCase):
    class FakeBot:
        async def is_owner(self, user):
            return getattr(user, "id", None) == 100

    class FakeRepository:
        def is_admin_authorized(self, user_id):
            return user_id == 200

    def setUp(self):
        self.previous_bot = authorization_commands._bot
        self.previous_repository = authorization_commands._repository
        authorization_commands._bot = self.FakeBot()
        authorization_commands._repository = self.FakeRepository()

    def tearDown(self):
        authorization_commands._bot = self.previous_bot
        authorization_commands._repository = self.previous_repository

    async def test_only_bot_owner_can_run_auth(self):
        owner_ctx = SimpleNamespace(author=SimpleNamespace(id=100))
        self.assertTrue(await authorization_commands.auth_admin_check(owner_ctx))

        delegated_admin_ctx = SimpleNamespace(author=SimpleNamespace(id=200))
        with self.assertRaises(authorization_commands.UnauthorizedAuthAdmin):
            await authorization_commands.auth_admin_check(delegated_admin_ctx)

    async def test_guild_owner_is_not_treated_as_bot_owner(self):
        guild_owner = SimpleNamespace(id=300, is_guild_owner=True)
        ctx = SimpleNamespace(author=guild_owner)
        with self.assertRaises(authorization_commands.UnauthorizedAuthAdmin):
            await authorization_commands.auth_admin_check(ctx)

    async def test_existing_panel_and_pending_removal_are_bot_owner_only(self):
        delegated_admin = SimpleNamespace(id=200)
        delegated_panel = authorization_commands.AuthAdminPanelView(owner_id=200)
        self.assertFalse(await delegated_panel.user_is_authorized(delegated_admin))

        class FakeResponse:
            def __init__(self):
                self.message = None

            async def send_message(self, message, **kwargs):
                self.message = message

        response = FakeResponse()
        interaction = SimpleNamespace(user=delegated_admin, response=response)
        self.assertFalse(await delegated_panel.interaction_check(interaction))
        self.assertEqual(
            response.message,
            "You are no longer authorized to use this panel.",
        )

        remove_modal = authorization_commands.AuthRemoveGuildModal(
            delegated_panel,
            authorization_commands.LICENSE_TYPES[0],
        )
        response.message = None
        await remove_modal.on_submit(interaction)
        self.assertEqual(
            response.message,
            "You are no longer authorized to use this panel.",
        )

        owner = SimpleNamespace(id=100)
        owner_panel = authorization_commands.AuthAdminPanelView(owner_id=100)
        self.assertTrue(await owner_panel.user_is_authorized(owner))
        self.assertFalse(await owner_panel.user_is_authorized(delegated_admin))


if __name__ == "__main__":
    unittest.main()