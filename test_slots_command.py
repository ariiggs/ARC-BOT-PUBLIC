import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import SlotsScrimSelectView, build_slot_status_embed, show_slots
from scrim_state import (
    STATUS_AVAILABLE,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_RESERVED,
    Slot,
)


class SlotsCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, *, category_id=None, role_id=77):
        return SimpleNamespace(
            author=SimpleNamespace(
                id=900,
                roles=[SimpleNamespace(id=role_id)],
            ),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(category_id=category_id),
            message=SimpleNamespace(delete=AsyncMock()),
            send=AsyncMock(),
        )

    def make_scrim(self, scrim_id="a" * 16, name="Friday Scrim"):
        return SimpleNamespace(
            id=scrim_id,
            guild_id=123,
            name=name,
            public_channel_id=1001,
            staff_channel_id=1002,
            deleted=False,
            emoji_available="⚪",
            emoji_reserved="🔵",
            emoji_pending="🟠",
            emoji_confirmed="🟢",
            slots={
                1: Slot(1, status=STATUS_AVAILABLE),
                2: Slot(2, status=STATUS_RESERVED),
                3: Slot(3, status=STATUS_PENDING),
                4: Slot(4, status=STATUS_CONFIRMED),
            },
        )

    def manager_config(self):
        return SimpleNamespace(manager_role_id=77, staff_role_id=77)

    def test_embed_merges_pending_into_confirmed(self):
        embed = build_slot_status_embed(self.make_scrim())

        self.assertEqual(embed.title, "📊 Slot Status - Friday Scrim")
        self.assertIn("**Total Slots:** 4", embed.description)
        self.assertIn("⚪ **Free:** 1", embed.description)
        self.assertIn("🔵 **Reserved:** 1", embed.description)
        self.assertIn("🟢 **Confirmed:** 2", embed.description)
        self.assertNotIn("Pending", embed.description)

    async def test_category_match_renders_the_matching_scrim(self):
        ctx = self.make_context(category_id=555)
        scrim = self.make_scrim()

        with (
            patch("main.repository.get_server_config", return_value=self.manager_config()),
            patch("main.repository.list", return_value=[scrim]),
            patch("main.scrim_category_ids", new=AsyncMock(return_value={555})),
            patch("main.delete_command_message", new=AsyncMock()) as delete_command,
        ):
            await show_slots(ctx)

        embed = ctx.send.call_args.kwargs["embed"]
        self.assertEqual(embed.title, "📊 Slot Status - Friday Scrim")
        delete_command.assert_awaited_once_with(ctx)

    async def test_one_scrim_outside_category_uses_smart_fallback(self):
        ctx = self.make_context(category_id=999)
        scrim = self.make_scrim()

        with (
            patch("main.repository.get_server_config", return_value=self.manager_config()),
            patch("main.repository.list", return_value=[scrim]),
            patch("main.scrim_category_ids", new=AsyncMock(return_value=set())),
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await show_slots(ctx)

        self.assertEqual(ctx.send.call_args.kwargs["embed"].title, "📊 Slot Status - Friday Scrim")

    async def test_multiple_scrims_outside_category_show_selector(self):
        ctx = self.make_context(category_id=999)
        scrims = [self.make_scrim(), self.make_scrim("b" * 16, "Saturday Scrim")]

        with (
            patch("main.repository.get_server_config", return_value=self.manager_config()),
            patch("main.repository.list", return_value=scrims),
            patch("main.scrim_category_ids", new=AsyncMock(return_value=set())),
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await show_slots(ctx)

        view = ctx.send.call_args.kwargs["view"]
        self.assertIsInstance(view, SlotsScrimSelectView)
        self.assertEqual(view.children[0].placeholder, "📊 Select a scrim to view...")
        self.assertEqual(len(view.children[0].options), 2)

    async def test_selector_edits_dropdown_message_with_selected_summary(self):
        scrim = self.make_scrim()
        view = SlotsScrimSelectView(
            owner_id=900,
            guild_id=123,
            scrims=[scrim],
        )
        select = view.children[0]
        select._values = [scrim.id]
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=900,
                roles=[SimpleNamespace(id=77)],
            ),
            guild=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )

        with (
            patch("main.repository.get_server_config", return_value=self.manager_config()),
            patch("main.repository.get", return_value=scrim),
        ):
            await select.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.call_args.kwargs
        self.assertEqual(kwargs["embed"].title, "📊 Slot Status - Friday Scrim")
        self.assertIsNone(kwargs["view"])


if __name__ == "__main__":
    unittest.main()