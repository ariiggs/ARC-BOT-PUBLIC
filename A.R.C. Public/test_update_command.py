import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import UpdateScrimSelectView, update_slots_command


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_scrim(self, scrim_id="a" * 16, name="Friday Scrim"):
        return SimpleNamespace(
            id=scrim_id,
            guild_id=123,
            name=name,
            deleted=False,
            public_channel_id=1001,
            staff_channel_id=1002,
        )

    def make_context(self):
        return SimpleNamespace(
            author=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=456),
            message=SimpleNamespace(delete=AsyncMock()),
            send=AsyncMock(),
        )

    async def test_one_active_scrim_outside_channel_shows_selector(self):
        ctx = self.make_context()
        scrim = self.make_scrim()
        feedback = AsyncMock()

        with (
            patch("main.repository.list", return_value=[scrim]),
            patch("main.member_is_staff", return_value=True),
            patch("main.publish_scrim", new=AsyncMock(return_value=True)) as publish,
            patch("main.delete_command_message", new=AsyncMock()) as delete_command,
        ):
            await update_slots_command(ctx)

        publish.assert_not_awaited()
        self.assertIsInstance(ctx.send.call_args.kwargs["view"], UpdateScrimSelectView)
        delete_command.assert_awaited_once_with(ctx)

    async def test_multiple_active_scrims_show_selector(self):
        ctx = self.make_context()
        scrims = [
            self.make_scrim(),
            self.make_scrim("b" * 16, "Saturday Scrim"),
        ]

        with (
            patch("main.repository.list", return_value=scrims),
            patch("main.delete_command_message", new=AsyncMock()) as delete_command,
        ):
            await update_slots_command(ctx)

        view = ctx.send.call_args.kwargs["view"]
        self.assertIsInstance(view, UpdateScrimSelectView)
        self.assertEqual(view.children[0].placeholder, "🔄 Select a scrim to update...")
        self.assertEqual(len(view.children[0].options), 2)
        delete_command.assert_awaited_once_with(ctx)

    async def test_selector_updates_selected_scrim(self):
        scrim = self.make_scrim()
        view = UpdateScrimSelectView(
            owner_id=900,
            guild_id=123,
            scrims=[scrim],
        )
        select = view.children[0]
        select._values = [scrim.id]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.member_is_staff", return_value=True),
            patch("main.publish_scrim", new=AsyncMock(return_value=True)) as publish,
        ):
            await select.callback(interaction)

        publish.assert_awaited_once_with(scrim)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.message.edit.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "The slot board has been updated.",
            ephemeral=True,
        )


if __name__ == "__main__":
    unittest.main()