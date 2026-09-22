import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from main import (
    _format_idpw_announcement,
    _format_idpw_reminder,
    _parse_idpw_input,
    _publish_idpwg,
    clear_active_idpw,
)


class IdpwgCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_scrim(self, *, pw_type="fixed", role_id=456, match_maps=None):
        maps = ["Erangel", "Miramar"]
        return SimpleNamespace(
            id="a" * 16,
            name="Friday Scrim",
            pw_type=pw_type,
            fixed_pw="fixed-secret",
            maps=maps,
            max_matches=4,
            match_maps=match_maps if match_maps is not None else list(maps),
            current_match_counter=1,
            timezone="UTC",
            confirmed_role_id=role_id,
        )

    def make_context(self):
        return SimpleNamespace(
            message=SimpleNamespace(delete=AsyncMock()),
        )

    async def test_fixed_mode_uses_database_password_and_map(self):
        scrim = self.make_scrim()
        ctx = self.make_context()
        sent_message = SimpleNamespace(id=987)
        target_channel = SimpleNamespace(send=AsyncMock(return_value=sent_message))

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch(
                "main.repository.get_idpw_config",
                return_value=SimpleNamespace(
                    target_channel_id=321,
                    fixed_password="legacy-secret",
                    timezone_name="UTC",
                ),
            ),
            patch("main.configured_text_channel", new=AsyncMock(return_value=target_channel)),
            patch("main.clear_active_idpw", new=AsyncMock()),
            patch("main.repository.set_idpw_announcement"),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.time.time", return_value=0),
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await _publish_idpwg(ctx, 1, "ROOM-1 / 5")

        content = target_channel.send.call_args.kwargs["content"]
        self.assertEqual(
            content,
            "# __**Match 1**__\n\n"
            "**Map : Erangel\n"
            "ID : `ROOM-1`\n"
            "PW : fixed-secret\n"
            "Start Time : 00:05**\n"
            "\n"
            "<@&456>",
        )
        self.assertEqual(scrim.current_match_counter, 2)

    def test_announcement_format_matches_discord_edit_text(self):
        self.assertEqual(
            _format_idpw_announcement(
                match_number=1,
                room_id="1233456",
                password="BETA",
                start_time="00:58",
                confirmed_role_id=456,
                map_name="Erangel",
            ),
            "# __**Match 1**__\n"
            "**Map : Erangel\n"
            "ID : `1233456`\n"
            "PW : BETA\n"
            "Start : 00:58**\n"
            "<@&456>",
        )

    def test_reminder_format_matches_discord_edit_text(self):
        self.assertEqual(
            _format_idpw_reminder(
                title="The match starts in 3 minutes",
                message="Please prepare.",
                confirmed_role_id=456,
            ),
            "# **The match starts in 3 minutes**\n"
            "**Please prepare.**\n"
            "<@&456>",
        )
        self.assertEqual(
            _format_idpw_reminder(
                title="FINAL CALL",
                message="The match begins in 1 minute.",
                confirmed_role_id=456,
            ),
            "# **FINAL CALL**\n"
            "**The match begins in 1 minute.**\n"
            "<@&456>",
        )

    async def test_clearing_a_run_deletes_announcement_and_reminders(self):
        announcement = SimpleNamespace(id=101, delete=AsyncMock())
        reminder = SimpleNamespace(id=102, delete=AsyncMock())
        task = Mock()
        task.done.return_value = False
        state = {
            "message": announcement,
            "messages": [announcement, reminder],
            "tasks": [task],
        }
        active = {"scrim-1": state}

        with (
            patch("main.active_idpw", active),
            patch("main.repository.get_idpw_config", return_value=None),
            patch("main.repository.get", return_value=None),
        ):
            await clear_active_idpw("scrim-1")

        task.cancel.assert_called_once_with()
        announcement.delete.assert_awaited_once_with()
        reminder.delete.assert_awaited_once_with()
        self.assertNotIn("scrim-1", active)

    async def test_fixed_mode_uses_the_individual_match_assignment(self):
        scrim = self.make_scrim(
            match_maps=["Miramar", "Erangel", "Erangel", "Miramar"]
        )
        ctx = self.make_context()
        target_channel = SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(id=987))
        )

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch(
                "main.repository.get_idpw_config",
                return_value=SimpleNamespace(
                    target_channel_id=321,
                    fixed_password="legacy-secret",
                    timezone_name="UTC",
                ),
            ),
            patch("main.configured_text_channel", new=AsyncMock(return_value=target_channel)),
            patch("main.clear_active_idpw", new=AsyncMock()),
            patch("main.repository.set_idpw_announcement"),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.time.time", return_value=0),
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await _publish_idpwg(ctx, 2, "ROOM-2 / 5")

        self.assertIn("Map : Erangel", target_channel.send.call_args.kwargs["content"])

    async def test_dynamic_mode_uses_password_argument_and_omits_missing_fields(self):
        scrim = self.make_scrim(pw_type="dynamic", role_id=None)
        scrim.maps = []
        scrim.match_maps = []
        ctx = self.make_context()
        sent_message = SimpleNamespace(id=987)
        target_channel = SimpleNamespace(send=AsyncMock(return_value=sent_message))

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch(
                "main.repository.get_idpw_config",
                return_value=SimpleNamespace(
                    target_channel_id=321,
                    fixed_password="unused",
                    timezone_name="UTC",
                ),
            ),
            patch("main.configured_text_channel", new=AsyncMock(return_value=target_channel)),
            patch("main.clear_active_idpw", new=AsyncMock()),
            patch("main.repository.set_idpw_announcement"),
            patch("main.repository.transaction", return_value=nullcontext()),
            patch("main.time.time", return_value=0),
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await _publish_idpwg(ctx, 2, "ROOM-2 / dynamic password / 10")

        self.assertEqual(
            target_channel.send.call_args.kwargs["content"],
            "# __**Match 2**__\n\n"
            "**ID : `ROOM-2`\n"
            "PW : dynamic password\n"
            "Start Time : 00:10**",
        )
        self.assertEqual(scrim.current_match_counter, 3)

    async def test_base_parser_preserves_spaces_in_dynamic_password(self):
        scrim = self.make_scrim(pw_type="dynamic")
        ctx = self.make_context()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch(
                "main.repository.get_idpw_config",
                return_value=SimpleNamespace(fixed_password="unused"),
            ),
        ):
            parsed = await _parse_idpw_input(
                ctx,
                "ROOM-BASE / password with spaces / 7",
                "!idpw",
            )

        self.assertIsNotNone(parsed)
        _, _, room_id, minutes, password = parsed
        self.assertEqual(room_id, "ROOM-BASE")
        self.assertEqual(minutes, 7)
        self.assertEqual(password, "password with spaces")

    async def test_argument_count_error_uses_fixed_format(self):
        scrim = self.make_scrim()
        ctx = self.make_context()
        feedback = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.repository.get_idpw_config", return_value=Mock()),
            patch("main.send_private_command_feedback", new=feedback),
        ):
            await _publish_idpwg(ctx, 3, "ROOM-3")

        feedback.assert_awaited_once_with(
            ctx,
            "❌ Format: `!idpwg3 <room_id> / <minutes>`",
        )

    async def test_argument_count_error_uses_dynamic_format(self):
        scrim = self.make_scrim(pw_type="dynamic")
        ctx = self.make_context()
        feedback = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.repository.get_idpw_config", return_value=Mock()),
            patch("main.send_private_command_feedback", new=feedback),
        ):
            await _publish_idpwg(ctx, 3, "ROOM-3 / 5")

        feedback.assert_awaited_once_with(
            ctx,
            "❌ Format: `!idpwg3 <room_id> / <password> / <minutes>`",
        )


if __name__ == "__main__":
    unittest.main()