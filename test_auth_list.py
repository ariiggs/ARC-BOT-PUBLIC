import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from main import (
    AuthAddGuildModal,
    AuthAdminPanelView,
    AuthRemoveGuildModal,
    ARC_AUTH_PRODUCT,
    auth_command,
    bot,
    build_auth_panel_embed,
    build_auth_tier_embed,
    authorized_guild_name,
)
from scrim_state import ScrimRepository
from slot_storage import SlotStateStore


def _pre_switch_license_store(path: Path):
    store = SlotStateStore(path)
    repository = ScrimRepository(store)
    payload = repository.payload()
    payload["version"] = 33
    licenses = {
        111111: (
            "Standard",
            30,
            datetime(2030, 3, 4, 12, 34, 56, 123456, tzinfo=timezone.utc),
        ),
        222222: (
            "Gold",
            180,
            datetime(2031, 6, 7, 8, 9, 10, 654321, tzinfo=timezone.utc),
        ),
        333333: (
            "Diamond",
            365,
            datetime(2032, 9, 10, 11, 12, 13, 987654, tzinfo=timezone.utc),
        ),
    }
    payload.update(
        authorized_guild_ids=sorted(licenses),
        authorized_guild_duration_days={
            str(guild_id): duration_days
            for guild_id, (_, duration_days, _) in licenses.items()
        },
        authorized_guild_license_types={
            str(guild_id): tier
            for guild_id, (tier, _, _) in licenses.items()
        },
        authorized_guild_expires_at={
            str(guild_id): expires_at.isoformat()
            for guild_id, (_, _, expires_at) in licenses.items()
        },
    )
    store.save(payload)
    return store, licenses


class AuthPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_cached_guild_name(self):
        guild = SimpleNamespace(name="A.R.C. Scrims")
        with patch.object(bot, "get_guild", return_value=guild):
            self.assertEqual(
                await authorized_guild_name(123),
                "A.R.C. Scrims",
            )

    async def test_fetches_guild_name_when_not_cached(self):
        guild = SimpleNamespace(name="Fetched Scrims")
        with (
            patch.object(bot, "get_guild", return_value=None),
            patch.object(bot, "fetch_guild", new=AsyncMock(return_value=guild)),
        ):
            self.assertEqual(
                await authorized_guild_name(456),
                "Fetched Scrims",
            )

    async def test_returns_none_when_guild_cannot_be_fetched(self):
        with (
            patch.object(bot, "get_guild", return_value=None),
            patch.object(
                bot,
                "fetch_guild",
                new=AsyncMock(
                    side_effect=discord.NotFound(
                        SimpleNamespace(status=404, reason="Not Found"),
                        "not available",
                    )
                ),
            ),
        ):
            self.assertIsNone(await authorized_guild_name(789))

    async def test_auth_command_opens_local_tier_picker(self):
        message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(
            author=SimpleNamespace(
                id=1234,
                send=AsyncMock(return_value=message),
            ),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        await auth_command.callback(ctx)

        kwargs = ctx.author.send.await_args.kwargs
        self.assertEqual(kwargs["view"].owner_id, 1234)
        self.assertEqual(
            [button.label for button in kwargs["view"].children],
            ["Standard", "Gold", "Diamond", "Close"],
        )
        self.assertEqual(kwargs["delete_after"], 330)
        self.assertIs(kwargs["view"].message, message)
        self.assertIn("Authorization Manager", kwargs["embed"].title)
        self.assertIn("A.R.C. Public", kwargs["embed"].description)

    def test_panel_is_scoped_to_public_not_beta(self):
        embed = build_auth_panel_embed()
        view = AuthAdminPanelView(owner_id=1234)

        self.assertIn("A.R.C. Public", embed.description)
        self.assertNotIn("A.R.C. Beta", embed.description)
        self.assertEqual(
            [button.label for button in view.children],
            ["Standard", "Gold", "Diamond", "Close"],
        )

    def test_pre_switch_sqlite_licenses_keep_tiers_durations_and_expiries(self):
        with tempfile.TemporaryDirectory() as directory:
            store, licenses = _pre_switch_license_store(
                Path(directory) / "pre-switch-public.sqlite3"
            )
            repository = ScrimRepository(store)
            repository.load()

            expected = [
                (guild_id, expires_at, duration_days)
                for guild_id, (_, duration_days, expires_at) in sorted(
                    licenses.items()
                )
            ]
            self.assertEqual(
                repository.list_authorizations(include_expired=True),
                expected,
            )
            for guild_id, (tier, duration_days, expires_at) in licenses.items():
                self.assertTrue(repository.is_guild_authorized(guild_id))
                self.assertEqual(repository.get_server_license_type(guild_id), tier)
                self.assertEqual(
                    repository.get_guild_subscription(guild_id),
                    (True, expires_at, duration_days),
                )

            reloaded = ScrimRepository(store)
            reloaded.load()
            self.assertEqual(
                reloaded.list_authorizations(include_expired=True),
                expected,
            )
            self.assertEqual(
                reloaded.authorized_guild_expires_at,
                {
                    guild_id: expires_at
                    for guild_id, (_, _, expires_at) in licenses.items()
                },
            )
            self.assertEqual(
                reloaded.authorized_guild_duration_days,
                {
                    guild_id: duration_days
                    for guild_id, (_, duration_days, _) in licenses.items()
                },
            )
            self.assertEqual(
                reloaded.authorized_guild_license_types,
                {
                    guild_id: tier
                    for guild_id, (tier, _, _) in licenses.items()
                },
            )

    async def test_tier_panel_shows_guild_name_tier_and_time_left(self):
        expires_at = datetime.now(timezone.utc) + timedelta(days=8, hours=3)
        with (
            patch(
                "main.repository.list_authorizations",
                return_value=[(123, expires_at, 8)],
            ) as list_authorizations,
            patch(
                "main.authorized_guild_name",
                new=AsyncMock(return_value="A.R.C. Scrims"),
            ),
        ):
            embed = await build_auth_tier_embed("Gold")

        list_authorizations.assert_called_once_with(
            include_expired=True,
            license_type="Gold",
        )
        self.assertIn("Gold Guild Authorizations", embed.title)
        self.assertIn("A.R.C. Scrims", embed.fields[0].name)
        self.assertIn("123", embed.fields[0].name)
        self.assertIn("Time left", embed.fields[0].value)
        self.assertIn("day(s)", embed.fields[0].value)

    async def test_tier_panel_shows_unlimited_and_expired_entries(self):
        expired_at = datetime.now(timezone.utc) - timedelta(days=2)
        with (
            patch(
                "main.repository.list_authorizations",
                return_value=[
                    (123, None, 0),
                    (456, expired_at, 7),
                ],
            ),
            patch(
                "main.authorized_guild_name",
                new=AsyncMock(side_effect=["Lifetime Guild", "Expired Guild"]),
            ),
        ):
            embed = await build_auth_tier_embed("Standard")

        self.assertIn("Unlimited", embed.fields[0].value)
        self.assertIn("Expired on", embed.fields[1].value)

    async def test_management_buttons_have_a_diamond_tier(self):
        view = AuthAdminPanelView(owner_id=1234)
        self.assertEqual(
            [button.label for button in view.children],
            ["Standard", "Gold", "Diamond", "Close"],
        )

        view.selected_tier = "Diamond"
        view.rebuild()
        self.assertEqual(
            [button.label for button in view.children],
            ["Add", "Remove", "Return", "Close"],
        )

    async def test_add_button_opens_modal_for_selected_tier(self):
        view = AuthAdminPanelView(owner_id=1234)
        view.selected_tier = "Diamond"
        view.rebuild()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        with patch.object(
            view,
            "user_is_authorized",
            new=AsyncMock(return_value=True),
        ):
            await view.children[0].callback(interaction)

        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, AuthAddGuildModal)
        self.assertEqual(modal.product, ARC_AUTH_PRODUCT)
        self.assertEqual(modal.license_type, "Diamond")

    async def test_adding_diamond_license_saves_to_this_bots_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            local_repository = ScrimRepository(
                SlotStateStore(Path(directory) / "public.sqlite3")
            )
            local_repository.load()
            panel = AuthAdminPanelView(owner_id=1234)
            modal = AuthAddGuildModal(panel, "Diamond")
            modal.guild_id_input._value = "987654321"
            modal.duration_input._value = "30"
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=1234),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            with (
                patch.object(
                    panel,
                    "user_is_authorized",
                    new=AsyncMock(return_value=True),
                ),
                patch("main.repository", local_repository),
            ):
                await modal.on_submit(interaction)

            reloaded = ScrimRepository(local_repository.store)
            reloaded.load()
            self.assertEqual(
                len(reloaded.list_authorizations(license_type="Diamond")),
                1,
            )
            self.assertEqual(reloaded.get_server_license_type(987654321), "Diamond")
            interaction.followup.send.assert_awaited_once()

    async def test_removing_diamond_license_uses_this_bots_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            local_repository = ScrimRepository(
                SlotStateStore(Path(directory) / "public.sqlite3")
            )
            local_repository.load()
            local_repository.authorize_guild(
                987654321,
                days=30,
                license_type="Diamond",
            )
            panel = AuthAdminPanelView(owner_id=1234)
            modal = AuthRemoveGuildModal(panel, "Diamond")
            modal.guild_id_input._value = "987654321"
            interaction = SimpleNamespace(
                user=SimpleNamespace(id=1234),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            with (
                patch.object(
                    panel,
                    "user_is_authorized",
                    new=AsyncMock(return_value=True),
                ),
                patch("main.repository", local_repository),
            ):
                await modal.on_submit(interaction)

            self.assertFalse(local_repository.is_guild_authorized(987654321))
            self.assertEqual(
                local_repository.list_authorizations(
                    include_expired=True,
                    license_type="Diamond",
                ),
                [],
            )
            interaction.followup.send.assert_awaited_once()

    def test_auth_is_the_only_registered_auth_command(self):
        command = bot.get_command("auth")
        self.assertIs(command, auth_command)
        self.assertNotIsInstance(command, commands.Group)


if __name__ == "__main__":
    unittest.main()