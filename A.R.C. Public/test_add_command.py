import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import AddDraftEntry, AddRegistrationView, add_team, build_add_draft
from scrim_state import STATUS_RESERVED, Slot


class AddCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self):
        return SimpleNamespace(
            author=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            send=AsyncMock(),
        )

    def make_scrim(self):
        return SimpleNamespace(
            name="Test Scrim",
            slot_start=3,
            slot_end=5,
            state_lock=asyncio.Lock(),
            slots={number: Slot(number) for number in range(3, 6)},
        )

    async def test_bulk_draft_parses_explicit_slots_without_persisting(self):
        ctx = self.make_context()
        scrim = self.make_scrim()
        first = SimpleNamespace(id=101)
        second = SimpleNamespace(id=102)

        with patch(
            "main.resolve_add_member",
            new=AsyncMock(side_effect=[first, second]),
        ):
            valid, errors = await build_add_draft(
                ctx,
                scrim,
                "Alpha / 03 / <@101>\nBravo / 04 / <@102>",
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [(entry.team_name, entry.slot_number, entry.member.id) for entry in valid],
            [("Alpha", 3, 101), ("Bravo", 4, 102)],
        )
        self.assertEqual([entry.tag for entry in valid], ["03", "04"])
        self.assertTrue(all(slot.status != STATUS_RESERVED for slot in scrim.slots.values()))

    async def test_draft_skips_occupied_slots_and_keeps_first_available_order(self):
        ctx = self.make_context()
        scrim = self.make_scrim()
        scrim.slots[4].status = STATUS_RESERVED
        member = SimpleNamespace(id=101)

        with patch(
            "main.resolve_add_member",
            new=AsyncMock(side_effect=[member, member]),
        ):
            valid, errors = await build_add_draft(
                ctx,
                scrim,
                "Alpha / 1 / <@101>\nBravo / 2 / <@101>",
            )

        self.assertEqual(
            [(entry.team_name, entry.slot_number) for entry in valid],
            [("Alpha", 3), ("Bravo", 5)],
        )
        self.assertEqual(errors, [])

    async def test_confirm_persists_all_valid_entries_as_reserved(self):
        ctx = self.make_context()
        scrim = self.make_scrim()
        first = SimpleNamespace(id=101)
        second = SimpleNamespace(id=102)
        entries = [
            AddDraftEntry("Alpha / 03 / <@101>", "Alpha", 3, first),
            AddDraftEntry("Bravo / 04 / <@102>", "Bravo", 4, second),
        ]
        view = AddRegistrationView(ctx, scrim, entries, [])
        interaction = SimpleNamespace(
            user=ctx.author,
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )

        with (
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.grant_manager_access", new_callable=AsyncMock, return_value=True),
            patch("main.send_scrim_log", new_callable=AsyncMock),
            patch("main.refresh_public_slots", new_callable=AsyncMock, return_value=True),
        ):
            await view._finish_confirm(interaction)

        self.assertEqual(scrim.slots[3].status, STATUS_RESERVED)
        self.assertEqual(scrim.slots[3].captain_1_id, 101)
        self.assertEqual(scrim.slots[4].status, STATUS_RESERVED)
        self.assertEqual(scrim.slots[4].captain_1_id, 102)
        self.assertTrue(view.completed)
        interaction.response.edit_message.assert_awaited_once()

    async def test_single_team_is_added_without_a_preview(self):
        ctx = self.make_context()
        scrim = self.make_scrim()
        entry = AddDraftEntry("Alpha / 03 / <@101>", "Alpha", 3, SimpleNamespace(id=101))

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.build_add_draft", new=AsyncMock(return_value=([entry], []))),
            patch(
                "main.apply_add_entries",
                new=AsyncMock(
                    return_value=(
                        [SimpleNamespace(number=3, team_name="Alpha")],
                        [],
                        [],
                        True,
                    )
                ),
            ),
            patch("main.delete_command_message", new=AsyncMock()) as delete,
            patch("main.AddRegistrationView") as view_class,
        ):
            await add_team(ctx, arguments="Alpha / 03 / <@101>")

        view_class.assert_not_called()
        delete.assert_awaited_once_with(ctx)

    async def test_two_teams_receive_the_interactive_preview(self):
        ctx = self.make_context()
        scrim = self.make_scrim()
        entries = [
            AddDraftEntry("Alpha / 03 / <@101>", "Alpha", 3, SimpleNamespace(id=101)),
            AddDraftEntry("Bravo / 04 / <@102>", "Bravo", 4, SimpleNamespace(id=102)),
        ]
        view = SimpleNamespace(message=None)
        ctx.send = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.build_add_draft", new=AsyncMock(return_value=(entries, []))),
            patch("main.AddRegistrationView", return_value=view) as view_class,
            patch("main.delete_command_message", new=AsyncMock()) as delete,
        ):
            await add_team(ctx, arguments="Alpha / 03 / <@101>\nBravo / 04 / <@102>")

        view_class.assert_called_once_with(ctx, scrim, entries, [])
        ctx.send.assert_awaited_once()
        self.assertIs(view.message, ctx.send.return_value)
        delete.assert_awaited_once_with(ctx)


if __name__ == "__main__":
    unittest.main()