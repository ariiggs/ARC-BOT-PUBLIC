import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import (
    close_scrim,
    open_scrim,
    register_team,
    set_registration_channel_open,
)
from scrim_state import Slot


class RegistrationControlTests(unittest.IsolatedAsyncioTestCase):
    def make_staff_context(self, channel_id=777):
        role = SimpleNamespace(id=456)
        guild = SimpleNamespace(
            id=123,
            default_role=SimpleNamespace(id=123),
            get_role=lambda role_id: role if role_id == 456 else None,
        )
        return SimpleNamespace(
            author=SimpleNamespace(id=900, roles=[role]),
            guild=guild,
            channel=SimpleNamespace(id=channel_id),
            message=SimpleNamespace(delete=AsyncMock()),
            send=AsyncMock(),
        )

    def make_registration_scrim(self):
        return SimpleNamespace(
            id="registration-controls",
            guild_id=123,
            public_channel_id=888,
            staff_channel_id=999,
            registration_channel_id=777,
            registration_role_id=456,
            registration_open=False,
            is_open=False,
            state_lock=asyncio.Lock(),
        )

    async def test_registration_channel_permission_toggle_is_persisted(self):
        ctx = self.make_staff_context()
        scrim = self.make_registration_scrim()
        channel = SimpleNamespace(set_permissions=AsyncMock())

        with (
            patch("main.configured_text_channel", new=AsyncMock(return_value=channel)),
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
        ):
            self.assertTrue(await set_registration_channel_open(ctx, scrim, True))

        self.assertTrue(scrim.registration_open)
        channel.set_permissions.assert_awaited_once_with(
            ctx.guild.get_role(456),
            send_messages=True,
            reason="Registration channel opened by staff",
        )

    async def test_open_and_close_in_registration_channel_do_not_change_public_state(self):
        ctx = self.make_staff_context()
        scrim = self.make_registration_scrim()
        scrim.registration_open = True

        with (
            patch("main.resolve_registration_scrim", return_value=scrim),
            patch("main.require_registration_staff_channel", new=AsyncMock(return_value=scrim)),
            patch("main.set_registration_channel_open", new=AsyncMock(return_value=True)) as toggle,
            patch("main.delete_command_message", new=AsyncMock()),
            patch("main.send_scrim_log", new=AsyncMock()),
        ):
            await close_scrim(ctx)
            await open_scrim(ctx)

        self.assertFalse(scrim.is_open)
        toggle.assert_any_await(ctx, scrim, False)
        toggle.assert_any_await(ctx, scrim, True)

    async def test_registration_can_continue_while_public_scrim_is_closed(self):
        ctx = self.make_staff_context()
        scrim = self.make_registration_scrim()
        scrim.is_open = False
        scrim.registration_open = True
        scrim.slots = {3: Slot(3)}
        scrim.state_lock = asyncio.Lock()
        snapshot = SimpleNamespace(number=3)

        with (
            patch("main.repository.list", return_value=[scrim]),
            patch(
                "main.apply_registration_entry",
                new=AsyncMock(return_value=(snapshot, None, True, True)),
            ),
        ):
            await register_team(ctx, arguments="Alpha / ALP")

        ctx.message.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()