import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import (
    AddDraftEntry,
    AddRegistrationView,
    SlotReviewView,
    add_team,
    apply_registration_entry,
    build_add_draft,
    register_team,
    update_registration_reaction,
)
from scrim_state import (
    STATUS_AVAILABLE,
    STATUS_PENDING,
    STATUS_RESERVED,
    RegistrationRequest,
    Slot,
    SlotSnapshot,
)


class AddCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self):
        return SimpleNamespace(
            author=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=777),
            message=SimpleNamespace(
                delete=AsyncMock(),
                add_reaction=AsyncMock(),
            ),
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

    async def test_register_uses_the_configured_role_and_auto_accepts(self):
        ctx = self.make_context()
        ctx.author.roles = [SimpleNamespace(id=456)]
        scrim = SimpleNamespace(
            id="scrim-1",
            is_open=True,
            state_lock=asyncio.Lock(),
            slots={3: Slot(3)},
            registration_channel_id=777,
            registration_role_id=456,
            registration_auto_accept=True,
        )
        snapshot = SimpleNamespace(number=3)

        with (
            patch("main.repository.list", return_value=[scrim]),
            patch(
                "main.apply_registration_entry",
                new=AsyncMock(return_value=(snapshot, None, True, True)),
            ) as apply_registration,
            patch("main.delete_command_message", new=AsyncMock()) as delete,
        ):
            await register_team(ctx, arguments="Alpha / ALP")

        entry = apply_registration.call_args.args[1]
        self.assertEqual(entry.team_name, "Alpha")
        self.assertEqual(entry.tag, "ALP")
        self.assertEqual(entry.member.id, 900)
        ctx.send.assert_not_awaited()
        ctx.message.add_reaction.assert_awaited_once_with("✅")
        delete.assert_not_awaited()

    async def test_register_rejects_members_without_the_configured_role(self):
        ctx = self.make_context()
        ctx.author.roles = [SimpleNamespace(id=999)]
        scrim = SimpleNamespace(
            is_open=True,
            registration_channel_id=777,
            registration_role_id=456,
        )

        with (
            patch("main.repository.list", return_value=[scrim]),
            patch("main.send_private_command_feedback", new=AsyncMock()) as feedback,
        ):
            await register_team(ctx, arguments="Alpha / ALP")

        feedback.assert_awaited_once()
        self.assertIn("role", feedback.call_args.args[1])

    async def test_validation_registration_creates_pending_request_and_notifies_staff(self):
        scrim = self.make_scrim()
        scrim.id = "a" * 16
        scrim.guild_id = 123
        scrim.registration_auto_accept = False
        member = SimpleNamespace(id=900, guild=SimpleNamespace())
        entry = AddDraftEntry("Alpha / ALP", "Alpha", 3, member, "ALP")

        with (
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.grant_manager_access", new=AsyncMock(return_value=True)),
            patch("main.notify_staff_for_registration", new=AsyncMock(return_value=True)) as notify,
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
        ):
            snapshot, error, access_ok, board_ok = await apply_registration_entry(
                scrim, entry, registration_message_id=321
            )

        self.assertIsNone(error)
        self.assertTrue(access_ok)
        self.assertTrue(board_ok)
        self.assertEqual(scrim.slots[3].status, STATUS_AVAILABLE)
        self.assertEqual(scrim.slots[3].team_name, "")
        self.assertEqual(len(scrim.pending_registrations), 1)
        notify.assert_awaited_once()
        self.assertEqual(snapshot.number, 3)
        self.assertEqual(snapshot.status, "En attente")
        request = next(iter(scrim.pending_registrations.values()))
        self.assertEqual(request.registration_message_id, 321)

    async def test_validation_registration_reacts_with_ok(self):
        ctx = self.make_context()
        ctx.author.roles = [SimpleNamespace(id=456)]
        scrim = SimpleNamespace(
            id="c" * 16,
            is_open=True,
            state_lock=asyncio.Lock(),
            slots={3: Slot(3)},
            registration_channel_id=777,
            registration_role_id=456,
            registration_auto_accept=False,
        )
        snapshot = SimpleNamespace(number=3)

        with (
            patch("main.repository.list", return_value=[scrim]),
            patch(
                "main.apply_registration_entry",
                new=AsyncMock(return_value=(snapshot, None, True, True)),
            ),
        ):
            await register_team(ctx, arguments="Alpha / ALP")

        ctx.message.add_reaction.assert_awaited_once_with("🆗")
        ctx.message.delete.assert_not_awaited()

    async def test_staff_approval_turns_registration_request_into_reserved_slot(self):
        scrim = self.make_scrim()
        scrim.id = "d" * 16
        scrim.guild_id = 123
        request = RegistrationRequest("e" * 16, 3, "Alpha", "ALP", 900, 0, 321)
        scrim.pending_registrations = {request.request_id: request}
        member = SimpleNamespace(id=900, guild=SimpleNamespace())
        interaction = SimpleNamespace(
            guild=SimpleNamespace(get_member=lambda member_id: member),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        view = SlotReviewView(
            scrim,
            SlotSnapshot(3, "En attente", "Alpha", "ALP", 900, 0, 900),
            registration_request=True,
            registration_request_id=request.request_id,
        )

        with (
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.grant_manager_access", new=AsyncMock(return_value=True)),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.update_registration_reaction", new=AsyncMock(return_value=True)) as update_reaction,
        ):
            await view.finish_review(interaction, True)

        self.assertEqual(scrim.slots[3].status, STATUS_RESERVED)
        self.assertEqual(scrim.slots[3].team_name, "Alpha")
        self.assertEqual(scrim.slots[3].manager_id, 900)
        self.assertEqual(scrim.pending_registrations, {})
        update_reaction.assert_awaited_once_with(scrim, request)

    async def test_approved_registration_replaces_ok_reaction_with_checkmark(self):
        scrim = SimpleNamespace(id="e" * 16, registration_channel_id=777)
        request = RegistrationRequest("f" * 16, 3, "Alpha", "ALP", 900, 0, 321)
        message = SimpleNamespace(
            add_reaction=AsyncMock(),
            remove_reaction=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        bot_user = SimpleNamespace(id=1)

        with (
            patch("main.configured_text_channel", new=AsyncMock(return_value=channel)),
            patch("main.bot", SimpleNamespace(user=bot_user)),
        ):
            self.assertTrue(await update_registration_reaction(scrim, request))

        message.add_reaction.assert_awaited_once_with("✅")
        message.remove_reaction.assert_awaited_once_with("🆗", bot_user)

    async def test_auto_accept_registration_creates_reserved_slot(self):
        scrim = self.make_scrim()
        scrim.id = "b" * 16
        scrim.guild_id = 123
        scrim.registration_auto_accept = True
        member = SimpleNamespace(id=900, guild=SimpleNamespace())
        entry = AddDraftEntry("Alpha / ALP", "Alpha", 3, member, "ALP")

        with (
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.grant_manager_access", new=AsyncMock(return_value=True)),
            patch("main.send_scrim_log", new=AsyncMock()),
            patch("main.refresh_public_slots", new=AsyncMock(return_value=True)),
            patch("main.notify_staff_for_registration", new=AsyncMock()) as notify,
        ):
            snapshot, error, access_ok, board_ok = await apply_registration_entry(
                scrim, entry
            )

        self.assertIsNone(error)
        self.assertTrue(access_ok)
        self.assertTrue(board_ok)
        self.assertEqual(scrim.slots[3].status, STATUS_RESERVED)
        self.assertEqual(snapshot.status, STATUS_RESERVED)
        notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()