import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import confirm_captain_role, confirm_slot
from scrim_state import STATUS_CONFIRMED, STATUS_PENDING, STATUS_RESERVED, Slot


class ConfirmCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self):
        return SimpleNamespace(
            author=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
        )

    def make_scrim(self, status):
        slot = Slot(
            3,
            status=status,
            team_name="Team",
            tag="TAG",
            manager_id=101,
            captain_1_id=101,
        )
        return SimpleNamespace(
            name="Test Scrim",
            state_lock=asyncio.Lock(),
            slots={3: slot},
        )

    async def test_confirm_moves_pending_slot_to_confirmed(self):
        ctx = self.make_context()
        scrim = self.make_scrim(STATUS_PENDING)
        confirm_role = AsyncMock(return_value=True)
        feedback = AsyncMock()
        require_scrim = AsyncMock(return_value=scrim)

        with (
            patch("main.require_staff_scrim", new=require_scrim),
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.confirm_captain_role", new=confirm_role),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.send_private_command_feedback", new=feedback),
        ):
            await confirm_slot(ctx, slot_numbers="3")

        self.assertEqual(scrim.slots[3].status, STATUS_CONFIRMED)
        require_scrim.assert_awaited_once_with(ctx, allow_public=True, silent=False)
        confirm_role.assert_awaited_once_with(scrim, 101)
        feedback.assert_awaited_once()
        self.assertIn("Confirmed slot(s): 03", feedback.await_args.args[1])
        self.assertEqual(feedback.await_args.kwargs["delete_after"], 3)

    async def test_confirm_moves_reserved_slot_to_confirmed(self):
        ctx = self.make_context()
        scrim = self.make_scrim(STATUS_RESERVED)
        confirm_role = AsyncMock(return_value=True)
        feedback = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.confirm_captain_role", new=confirm_role),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.send_private_command_feedback", new=feedback),
        ):
            await confirm_slot(ctx, slot_numbers="3")

        self.assertEqual(scrim.slots[3].status, STATUS_CONFIRMED)
        confirm_role.assert_awaited_once_with(scrim, 101)
        feedback.assert_awaited_once()
        self.assertIn("Confirmed slot(s): 03", feedback.await_args.args[1])
        self.assertEqual(feedback.await_args.kwargs["delete_after"], 3)

    async def test_confirm_rejects_slot_that_is_already_confirmed(self):
        ctx = self.make_context()
        scrim = self.make_scrim(STATUS_CONFIRMED)
        feedback = AsyncMock()
        confirm_role = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.is_active", return_value=True),
            patch("main.send_private_command_feedback", new=feedback),
            patch("main.confirm_captain_role", new=confirm_role),
        ):
            await confirm_slot(ctx, slot_numbers="3")

        self.assertEqual(scrim.slots[3].status, STATUS_CONFIRMED)
        confirm_role.assert_not_awaited()
        feedback.assert_awaited_once()

    async def test_confirm_changes_only_the_requested_slot_for_same_manager(self):
        ctx = self.make_context()
        scrim = SimpleNamespace(
            state_lock=asyncio.Lock(),
            slots={
                3: Slot(
                    3,
                    status=STATUS_PENDING,
                    team_name="Team Three",
                    tag="T3",
                    manager_id=101,
                    captain_1_id=101,
                ),
                4: Slot(
                    4,
                    status=STATUS_PENDING,
                    team_name="Team Four",
                    tag="T4",
                    manager_id=101,
                    captain_1_id=101,
                ),
            },
        )
        confirm_role = AsyncMock(return_value=True)
        feedback = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.confirm_captain_role", new=confirm_role),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.send_private_command_feedback", new=feedback),
        ):
            await confirm_slot(ctx, slot_numbers="3")

        self.assertEqual(scrim.slots[3].status, STATUS_CONFIRMED)
        self.assertEqual(scrim.slots[4].status, STATUS_PENDING)
        confirm_role.assert_awaited_once_with(scrim, 101)
        feedback.assert_awaited_once()
        self.assertIn("Confirmed slot(s): 03", feedback.await_args.args[1])


class ConfirmCaptainRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_swaps_pending_role_for_confirmed_role(self):
        pending_role = object()
        confirmed_role = object()

        def get_role(role_id):
            return {10: pending_role, 20: confirmed_role}.get(role_id)

        guild = SimpleNamespace(get_role=get_role)
        member = SimpleNamespace(
            guild=guild,
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        scrim = SimpleNamespace(
            name="Test Scrim",
            pending_role_id=10,
            confirmed_role_id=20,
            slots={
                3: Slot(
                    3,
                    status=STATUS_CONFIRMED,
                    team_name="Team",
                    tag="TAG",
                    manager_id=101,
                    captain_1_id=101,
                )
            },
        )

        result = await confirm_captain_role(scrim, 101, member=member)

        self.assertTrue(result)
        member.add_roles.assert_awaited_once_with(
            confirmed_role,
            reason="Confirmed captain assignment for Test Scrim",
        )
        member.remove_roles.assert_awaited_once_with(
            pending_role,
            reason="Remove pending captain role for Test Scrim",
        )

    async def test_confirm_keeps_pending_role_for_another_pending_slot(self):
        pending_role = object()
        confirmed_role = object()

        def get_role(role_id):
            return {10: pending_role, 20: confirmed_role}.get(role_id)

        guild = SimpleNamespace(get_role=get_role)
        member = SimpleNamespace(
            guild=guild,
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        scrim = SimpleNamespace(
            name="Test Scrim",
            pending_role_id=10,
            confirmed_role_id=20,
            slots={
                3: Slot(
                    3,
                    status=STATUS_CONFIRMED,
                    team_name="Team Three",
                    tag="T3",
                    manager_id=101,
                    captain_1_id=101,
                ),
                4: Slot(
                    4,
                    status=STATUS_PENDING,
                    team_name="Team Four",
                    tag="T4",
                    manager_id=101,
                    captain_1_id=101,
                ),
            },
        )

        result = await confirm_captain_role(scrim, 101, member=member)

        self.assertTrue(result)
        member.add_roles.assert_awaited_once_with(
            confirmed_role,
            reason="Confirmed captain assignment for Test Scrim",
        )
        member.remove_roles.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()