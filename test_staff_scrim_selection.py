import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import StaffScrimSelectView, show_staff_scrim_selector


class StaffScrimSelectionTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, command=None):
        return SimpleNamespace(
            author=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=456),
            command=command or SimpleNamespace(invoke=AsyncMock()),
            message=SimpleNamespace(delete=AsyncMock()),
            send=AsyncMock(),
        )

    def make_scrim(self):
        return SimpleNamespace(
            id="a" * 16,
            guild_id=123,
            name="Friday Scrim",
            deleted=False,
        )

    async def test_selector_is_shown_even_for_one_active_scrim(self):
        ctx = self.make_context()
        scrim = self.make_scrim()

        with (
            patch("main.repository.list", return_value=[scrim]),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await show_staff_scrim_selector(ctx)

        view = ctx.send.call_args.kwargs["view"]
        self.assertIsInstance(view, StaffScrimSelectView)
        self.assertEqual(len(view.children[0].options), 1)

    async def test_selection_resumes_only_the_current_command(self):
        scrim = self.make_scrim()
        command = SimpleNamespace(callback=AsyncMock())
        ctx = self.make_context(command)
        view = StaffScrimSelectView(ctx, [scrim])
        select = view.children[0]
        select._values = [scrim.id]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(
                send_message=AsyncMock(),
                edit_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
        ):
            await select.callback(interaction)

        command.callback.assert_awaited_once_with(ctx)
        self.assertFalse(hasattr(ctx, "_staff_scrim_override"))
        interaction.response.edit_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()