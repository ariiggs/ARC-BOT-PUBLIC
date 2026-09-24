import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import ExportScrimSelectView, build_export_messages, export_teams
from scrim_state import STATUS_AVAILABLE, STATUS_CONFIRMED, STATUS_RESERVED, Slot


class ExportMessageTests(unittest.TestCase):
    def test_exports_one_code_block_per_team_with_manager_mention(self):
        scrim = SimpleNamespace(
            slots={
                3: Slot(
                    3,
                    status=STATUS_CONFIRMED,
                    team_name="NaVi",
                    tag="NV",
                    manager_id=123456789012345678,
                ),
                4: Slot(4, status=STATUS_AVAILABLE),
                5: Slot(
                    5,
                    status=STATUS_RESERVED,
                    team_name="Team Five",
                    tag="T5",
                    manager_id=987654321098765432,
                ),
            }
        )

        messages = build_export_messages(scrim)

        self.assertEqual(
            messages,
            [
                "```text\nNaVi NV <@123456789012345678>\n```",
                "```text\nTeam Five T5 <@987654321098765432>\n```",
            ],
        )
        self.assertIn("<@123456789012345678>", messages[0])

    def test_returns_no_messages_when_no_team_is_registered(self):
        scrim = SimpleNamespace(
            slots={
                3: Slot(3, status=STATUS_AVAILABLE),
                4: Slot(4, status=STATUS_AVAILABLE),
            }
        )

        self.assertEqual(build_export_messages(scrim), [])

    def test_exports_co_captain_id_after_primary_captain_id(self):
        scrim = SimpleNamespace(
            slots={
                3: Slot(
                    3,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha",
                    tag="A",
                    manager_id=123456789012345678,
                    captain_2_id=987654321098765432,
                )
            }
        )

        self.assertEqual(
            build_export_messages(scrim),
            ["```text\nAlpha A <@123456789012345678> <@987654321098765432>\n```"],
        )


class ExportCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self):
        return SimpleNamespace(
            author=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=456),
            send=AsyncMock(),
        )

    async def test_export_can_start_from_any_channel_and_lists_server_scrims(self):
        ctx = self.make_context()
        scrim = SimpleNamespace(id="scrim-1", name="Friday Scrim")

        with (
            patch("main.member_is_staff_in_guild", return_value=True),
            patch("main.repository.list", return_value=[scrim]) as list_scrims,
            patch("main.require_staff_scrim", new=AsyncMock()) as require_scrim,
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await export_teams(ctx)

        list_scrims.assert_called_once_with(123)
        require_scrim.assert_not_awaited()
        sent_kwargs = ctx.send.call_args.kwargs
        view = sent_kwargs["view"]
        self.assertIsInstance(view, ExportScrimSelectView)
        select = view.children[0]
        self.assertEqual(select.placeholder, "📂 Select a Scrim to export...")
        self.assertEqual(select.options[0].label, "Friday Scrim")
        self.assertEqual(select.options[0].value, "scrim-1")

    async def test_selector_exports_selected_scrim_ephemerally(self):
        scrim = SimpleNamespace(
            id="scrim-1",
            guild_id=123,
            name="Friday Scrim",
            deleted=False,
            slots={},
        )
        view = ExportScrimSelectView(
            owner_id=900,
            guild_id=123,
            scrims=[scrim],
        )
        select = view.children[0]
        select._values = ["scrim-1"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=900),
            guild=SimpleNamespace(id=123),
            response=SimpleNamespace(send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch("main.member_is_staff_in_guild", return_value=True),
            patch("main.repository.get", return_value=scrim),
            patch(
                "main.build_export_messages",
                return_value=["```text\nAlpha A <@101>\n```", "```text\nBravo B <@202>\n```"],
            ) as build_messages,
        ):
            await select.callback(interaction)

        build_messages.assert_called_once_with(scrim)
        interaction.response.send_message.assert_awaited_once()
        first_args, first_kwargs = interaction.response.send_message.await_args
        self.assertEqual(first_args[0], "```text\nAlpha A <@101>\n```")
        self.assertTrue(first_kwargs["ephemeral"])
        interaction.followup.send.assert_awaited_once()
        second_args, second_kwargs = interaction.followup.send.await_args
        self.assertEqual(second_args[0], "```text\nBravo B <@202>\n```")
        self.assertTrue(second_kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()