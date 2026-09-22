import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import ManagerSlotView
from scrim_state import STATUS_CONFIRMED, STATUS_PENDING, STATUS_RESERVED, Slot


class ManagerConfirmButtonTests(unittest.IsolatedAsyncioTestCase):
    def make_scrim(self, status):
        return SimpleNamespace(
            id="a" * 16,
            name="Test Scrim",
            is_open=True,
            deleted=False,
            state_lock=asyncio.Lock(),
            public_channel_id=10,
            staff_channel_id=11,
            slots={
                3: Slot(
                    3,
                    status=status,
                    team_name="Team",
                    tag="TAG",
                    manager_id=101,
                    captain_1_id=101,
                )
            },
        )

    def make_interaction(self):
        return SimpleNamespace(
            user=SimpleNamespace(id=101),
            message=SimpleNamespace(id=500),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_duplicate_confirm_is_rejected_for_pending_or_confirmed_slot(self):
        for status in (STATUS_PENDING, STATUS_CONFIRMED):
            with self.subTest(status=status):
                scrim = self.make_scrim(status)
                view = ManagerSlotView(scrim)
                interaction = self.make_interaction()

                with (
                    patch("main.interaction_in_public_channel", return_value=True),
                    patch("main.is_active", return_value=True),
                    patch("main.repository.transaction", return_value=nullcontext()) as transaction,
                ):
                    await view.finish_manager_action(
                        interaction,
                        confirm=True,
                        assignment=scrim.slots[3].snapshot(),
                        expected_board_id=500,
                    )

                interaction.response.send_message.assert_awaited_once_with(
                    "⏳ You have already confirmed your slot. "
                    "Please wait for the staff to validate it.",
                    ephemeral=True,
                )
                interaction.response.defer.assert_not_awaited()
                interaction.followup.send.assert_not_awaited()
                transaction.assert_not_called()

    async def test_reserved_slot_still_moves_to_pending(self):
        scrim = self.make_scrim(STATUS_RESERVED)
        view = ManagerSlotView(scrim)
        interaction = self.make_interaction()

        with (
            patch("main.interaction_in_public_channel", return_value=True),
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
            patch("main.notify_staff_of_manager_action", new=AsyncMock(return_value=True)),
            patch("main.send_scrim_log", new=AsyncMock()),
        ):
            await view.finish_manager_action(
                interaction,
                confirm=True,
                assignment=scrim.slots[3].snapshot(),
                expected_board_id=500,
            )

        self.assertEqual(scrim.slots[3].status, STATUS_PENDING)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        self.assertIn("Pending", interaction.followup.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()