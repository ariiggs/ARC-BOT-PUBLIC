import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from main import (
    AuthAddGuildModal,
    AuthAdminPanelView,
    auth_command,
    bot,
    build_auth_tier_embed,
    authorized_guild_name,
)


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

    async def test_auth_command_opens_version_picker(self):
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
            ["Standard", "Gold", "Close"],
        )
        self.assertEqual(kwargs["delete_after"], 330)
        self.assertIs(kwargs["view"].message, message)
        self.assertIn("Authorization Manager", kwargs["embed"].title)

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
        self.assertIn("Gold Version", embed.title)
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

    async def test_management_buttons_appear_after_tier_is_selected(self):
        view = AuthAdminPanelView(owner_id=1234)
        standard_button = view.children[0]
        interaction = SimpleNamespace()
        with patch.object(view, "show_tier", new=AsyncMock()) as show_tier:
            await standard_button.callback(interaction)
        show_tier.assert_awaited_once_with(interaction, "Standard")

        view.selected_tier = "Gold"
        view.rebuild()
        self.assertEqual(
            [button.label for button in view.children],
            ["Add", "Remove", "Return"],
        )

    async def test_add_button_opens_modal_for_selected_tier(self):
        view = AuthAdminPanelView(owner_id=1234)
        view.selected_tier = "Gold"
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
        self.assertEqual(modal.license_type, "Gold")

    def test_auth_is_the_only_registered_auth_command(self):
        command = bot.get_command("auth")
        self.assertIs(command, auth_command)
        self.assertNotIsInstance(command, commands.Group)


if __name__ == "__main__":
    unittest.main()