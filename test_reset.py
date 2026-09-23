import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import perform_scrim_reset, purge_channel_messages
from scrim_state import RegistrationRequest, Slot, STATUS_RESERVED


class ResetCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_purge_keeps_pinned_messages(self):
        channel = SimpleNamespace(
            purge=AsyncMock(
                return_value=[
                    SimpleNamespace(pinned=True),
                    SimpleNamespace(pinned=False),
                ]
            )
        )

        self.assertTrue(await purge_channel_messages(channel))

        check = channel.purge.call_args.kwargs["check"]
        self.assertFalse(check(SimpleNamespace(pinned=True)))
        self.assertTrue(check(SimpleNamespace(pinned=False)))

    async def test_reset_clears_registration_channel_and_pending_requests(self):
        scrim = SimpleNamespace(
            id="reset-scrim",
            guild_id=123,
            board_lock=asyncio.Lock(),
            state_lock=asyncio.Lock(),
            deleted=False,
            staff_channel_id=11,
            public_channel_id=22,
            registration_channel_id=33,
            current_match_counter=4,
            slots={1: Slot(1, status=STATUS_RESERVED, manager_id=900)},
            pending_registrations={
                "request": RegistrationRequest(
                    "request", 1, "Alpha", "ALP", 900, 0, 321
                )
            },
        )
        channels = {
            channel_id: SimpleNamespace(id=channel_id)
            for channel_id in (11, 22, 33)
        }

        with (
            patch("main.is_active", return_value=True),
            patch("main.send_reset_history_snapshot", new=AsyncMock(return_value=True)),
            patch("main.clear_active_idpw", new=AsyncMock()),
            patch("main.revoke_manager_access_if_unused", new=AsyncMock()),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch(
                "main.configured_text_channel",
                new=AsyncMock(side_effect=lambda _scrim, channel_id: channels[channel_id]),
            ),
            patch(
                "main.purge_channel_messages", new=AsyncMock(return_value=True)
            ) as purge,
            patch("main.refresh_public_slots_locked", new=AsyncMock(return_value=True)),
        ):
            result = await perform_scrim_reset(scrim, channels[11])

        self.assertIn("registration channels", result)
        self.assertEqual(purge.await_count, 3)
        self.assertEqual(
            {call.args[0].id for call in purge.await_args_list},
            {11, 22, 33},
        )
        self.assertEqual(scrim.pending_registrations, {})
        self.assertEqual(scrim.slots[1].status, "Disponible")
        self.assertEqual(scrim.current_match_counter, 1)

        scrim.registration_channel_id = None
        with (
            patch("main.is_active", return_value=True),
            patch("main.send_reset_history_snapshot", new=AsyncMock(return_value=True)),
            patch("main.clear_active_idpw", new=AsyncMock()),
            patch("main.revoke_manager_access_if_unused", new=AsyncMock()),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch(
                "main.configured_text_channel",
                new=AsyncMock(side_effect=lambda _scrim, channel_id: channels[channel_id]),
            ),
            patch(
                "main.purge_channel_messages", new=AsyncMock(return_value=True)
            ) as optional_purge,
            patch("main.refresh_public_slots_locked", new=AsyncMock(return_value=True)),
        ):
            result = await perform_scrim_reset(scrim, channels[11])

        self.assertNotIn("registration channels", result)
        self.assertEqual(optional_purge.await_count, 2)


if __name__ == "__main__":
    unittest.main()