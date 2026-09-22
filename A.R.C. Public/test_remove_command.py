import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import remove_team, revoke_manager_access
from scrim_state import STATUS_AVAILABLE, STATUS_CONFIRMED, STATUS_PENDING, Slot


class RemoveCommandTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_remove_accepts_pending_and_confirmed_slots(self):
        for status in (STATUS_PENDING, STATUS_CONFIRMED):
            with self.subTest(status=status):
                ctx = self.make_context()
                scrim = self.make_scrim(status)
                revoke_access = AsyncMock(return_value=True)

                with (
                    patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
                    patch("main.is_active", return_value=True),
                    patch("main.repository.transaction", return_value=nullcontext()),
                    patch(
                        "main.revoke_manager_access_if_unused",
                        new=revoke_access,
                    ),
                    patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
                    patch("main.send_scrim_log", new=AsyncMock()),
                    patch("main.delete_command_message", new=AsyncMock()),
                ):
                    await remove_team(ctx, slot_numbers="3")

                slot = scrim.slots[3]
                self.assertEqual(slot.status, "Disponible")
                self.assertIsNone(slot.manager_id)
                self.assertIsNone(slot.captain_1_id)
                revoke_access.assert_awaited_once_with(scrim, 101)

    async def test_remove_keeps_roles_when_same_captain_has_another_slot(self):
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
                    status=STATUS_CONFIRMED,
                    team_name="Team Four",
                    tag="T4",
                    manager_id=101,
                    captain_1_id=101,
                ),
            },
        )
        revoke_access = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.revoke_manager_access", new=revoke_access),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await remove_team(ctx, slot_numbers="3")

        self.assertEqual(scrim.slots[3].status, STATUS_AVAILABLE)
        self.assertEqual(scrim.slots[4].status, STATUS_CONFIRMED)
        self.assertEqual(scrim.slots[4].manager_id, 101)
        revoke_access.assert_not_awaited()


class RevokeManagerAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_removes_pending_and_confirmed_roles(self):
        pending_role = object()
        confirmed_role = object()

        def get_role(role_id):
            return {10: pending_role, 20: confirmed_role}.get(role_id)

        guild = SimpleNamespace(get_role=get_role)
        channel = SimpleNamespace(
            guild=guild,
            set_permissions=AsyncMock(),
        )
        member = SimpleNamespace(
            guild=guild,
            remove_roles=AsyncMock(),
        )
        scrim = SimpleNamespace(
            name="Test Scrim",
            public_channel_id=50,
            pending_role_id=10,
            confirmed_role_id=20,
        )

        with patch(
            "main.configured_text_channel",
            new=AsyncMock(return_value=channel),
        ):
            result = await revoke_manager_access(scrim, 101, member=member)

        self.assertTrue(result)
        self.assertEqual(member.remove_roles.await_count, 2)
        removed_roles = {entry.args[0] for entry in member.remove_roles.await_args_list}
        self.assertEqual(removed_roles, {pending_role, confirmed_role})
        channel.set_permissions.assert_awaited_once_with(
            member,
            overwrite=None,
            reason="Remove released scrim manager access for Test Scrim",
        )


if __name__ == "__main__":
    unittest.main()