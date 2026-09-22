import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import (
    CAP_CHANNEL_RESTRICTION_MESSAGE,
    CaptainSlotSelectView,
    captain_assignment_or_selection,
    captain_slot_id,
    perform_cap_transfer,
    require_cap_channel,
    send_private_command_feedback,
)
from scrim_state import STATUS_RESERVED, Slot


class CapChannelRestrictionTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, author_id=1):
        return SimpleNamespace(
            author=SimpleNamespace(id=author_id),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=42),
            message=SimpleNamespace(delete=AsyncMock()),
            send=AsyncMock(),
        )

    async def test_feedback_allows_explicit_allowed_mentions(self):
        ctx = SimpleNamespace(
            send=AsyncMock(),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        mentions = object()

        await send_private_command_feedback(
            ctx,
            "Test",
            allowed_mentions=mentions,
        )

        ctx.send.assert_awaited_once()
        self.assertIs(ctx.send.call_args.kwargs["allowed_mentions"], mentions)

    async def test_wrong_channel_sends_exact_temporary_restriction(self):
        ctx = SimpleNamespace(
            channel=SimpleNamespace(id=99),
            guild=SimpleNamespace(id=123),
        )
        configured_scrim = SimpleNamespace(cap_channel_id=42)

        with (
            patch("main.repository.list", return_value=[configured_scrim]),
            patch(
                "main.send_private_command_feedback",
                new_callable=AsyncMock,
            ) as feedback,
        ):
            self.assertFalse(await require_cap_channel(ctx))

        feedback.assert_awaited_once_with(
            ctx,
            CAP_CHANNEL_RESTRICTION_MESSAGE,
            silent=False,
        )
        self.assertEqual(
            CAP_CHANNEL_RESTRICTION_MESSAGE,
            "❌ **Command restricted.** Please use the designated captain "
            "management channel for this command.",
        )

    async def test_no_slots_returns_requested_error(self):
        ctx = self.make_context()
        target = SimpleNamespace(id=2)

        with (
            patch("main.captain_assignments", return_value=[]),
            patch(
                "main.send_private_command_feedback",
                new_callable=AsyncMock,
            ) as feedback,
        ):
            result = await captain_assignment_or_selection(ctx, "add", target)

        self.assertIsNone(result)
        feedback.assert_awaited_once_with(
            ctx,
            "❌ You are not a captain of any registered slot.",
            silent=False,
        )

    async def test_one_slot_proceeds_without_a_selector(self):
        ctx = self.make_context()
        target = SimpleNamespace(id=2)
        scrim = SimpleNamespace(id="a" * 16, cap_channel_id=42)
        slot = Slot(
            3,
            status=STATUS_RESERVED,
            team_name="Alpha",
            tag="A",
            manager_id=1,
            captain_1_id=1,
        )
        assignment = (scrim, slot, 1)

        with patch("main.captain_assignments", return_value=[assignment]):
            result = await captain_assignment_or_selection(ctx, "add", target)

        self.assertIs(result, assignment)

    async def test_multiple_slots_create_the_requested_selector(self):
        ctx = self.make_context()
        target = SimpleNamespace(id=2)
        first_scrim = SimpleNamespace(id="a" * 16, cap_channel_id=42)
        second_scrim = SimpleNamespace(id="b" * 16, cap_channel_id=42)
        first_slot = Slot(
            3,
            status=STATUS_RESERVED,
            team_name="Alpha",
            tag="A",
            manager_id=1,
            captain_1_id=1,
        )
        second_slot = Slot(
            7,
            status=STATUS_RESERVED,
            team_name="Bravo",
            tag="B",
            manager_id=1,
            captain_1_id=1,
        )
        assignments = [
            (first_scrim, first_slot, 1),
            (second_scrim, second_slot, 1),
        ]

        with (
            patch("main.captain_assignments", return_value=assignments),
            patch("main.delete_command_message", new_callable=AsyncMock),
        ):
            await captain_assignment_or_selection(ctx, "transfer", target)

        sent = ctx.send.call_args
        view = sent.kwargs["view"]
        select = view.children[0]
        self.assertEqual(
            select.placeholder,
            "Select a slot to apply this command...",
        )
        self.assertEqual(
            [option.label for option in select.options],
            ["Slot 3 - Alpha", "Slot 7 - Bravo"],
        )
        self.assertEqual(
            [option.value for option in select.options],
            [
                captain_slot_id(first_scrim, first_slot),
                captain_slot_id(second_scrim, second_slot),
            ],
        )
        self.assertEqual(sent.kwargs["delete_after"], 120)

    async def test_selector_callback_executes_selected_slot_and_confirms(self):
        ctx = self.make_context()
        target = SimpleNamespace(id=2)
        scrim = SimpleNamespace(id="a" * 16, cap_channel_id=42)
        slot = Slot(
            3,
            status=STATUS_RESERVED,
            team_name="Alpha",
            tag="A",
            manager_id=1,
            captain_1_id=1,
        )
        assignment = (scrim, slot, 1)
        view = CaptainSlotSelectView(ctx, "add", target, [assignment])
        select = view.children[0]
        select._values = [captain_slot_id(scrim, slot)]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1),
            guild=SimpleNamespace(id=123),
            channel_id=42,
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )

        with (
            patch("main.captain_assignments", return_value=[assignment]),
            patch(
                "main.execute_cap_action",
                new_callable=AsyncMock,
                return_value=(True, "ignored"),
            ) as execute,
        ):
            await select.callback(interaction)

        execute.assert_awaited_once_with("add", ctx, target, assignment)
        interaction.response.edit_message.assert_awaited_once_with(
            content="✅ Command successfully applied to Slot 3 (Alpha).",
            view=None,
        )

    async def test_transfer_allows_a_member_who_captains_another_slot(self):
        ctx = self.make_context()
        target = SimpleNamespace(id=2)
        slot = Slot(
            3,
            status=STATUS_RESERVED,
            team_name="Alpha",
            tag="A",
            manager_id=1,
            captain_1_id=1,
        )
        other_slot = Slot(
            7,
            status=STATUS_RESERVED,
            team_name="Bravo",
            tag="B",
            manager_id=2,
            captain_1_id=2,
        )
        scrim = SimpleNamespace(
            id="a" * 16,
            state_lock=asyncio.Lock(),
            slots={3: slot, 7: other_slot},
        )
        assignment = (scrim, slot, 1)

        with (
            patch("main.is_active", return_value=True),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch(
                "main.grant_manager_access",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "main.revoke_manager_access_if_unused",
                new_callable=AsyncMock,
            ),
            patch(
                "main.refresh_cap_board",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("main.send_scrim_log", new_callable=AsyncMock),
        ):
            success, message = await perform_cap_transfer(ctx, target, assignment)

        self.assertTrue(success)
        self.assertIn("Leadership Transferred", message)
        self.assertEqual(slot.captain_1_id, 2)
        self.assertEqual(other_slot.captain_1_id, 2)

    async def test_configured_channel_is_allowed_for_the_scrim(self):
        ctx = SimpleNamespace(
            channel=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=123),
        )
        scrim = SimpleNamespace(cap_channel_id=42)

        with patch("main.send_private_command_feedback", new_callable=AsyncMock):
            self.assertTrue(await require_cap_channel(ctx, scrim))


if __name__ == "__main__":
    unittest.main()