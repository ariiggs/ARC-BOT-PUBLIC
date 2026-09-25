import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from main import (
    _format_idpw_announcement,
    _format_idpw_reminder,
    _parse_idpw_input,
    _publish_idpwg,
    _send_scheduled_idpw_reminder,
    active_idpw,
    active_idpw_locks,
    clear_active_idpw,
    restore_active_idpw,
)


class IdpwgCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        active_idpw.clear()
        active_idpw_locks.clear()

    async def asyncTearDown(self):
        tasks = [
            task
            for state in active_idpw.values()
            for task in state.get("tasks", [])
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        active_idpw.clear()
        active_idpw_locks.clear()

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
        target_channel = SimpleNamespace(
            id=321,
            send=AsyncMock(return_value=sent_message),
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
            patch("main.repository.set_idpw_run"),
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

    async def test_clearing_a_run_deletes_a_mismatched_persisted_announcement(self):
        active_message = SimpleNamespace(id=101, delete=AsyncMock())
        persisted_message = SimpleNamespace(id=202, delete=AsyncMock())
        channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=persisted_message)
        )
        scrim = SimpleNamespace(id="scrim-1", guild_id=123)
        config = SimpleNamespace(
            announcement_message_id=202,
            target_channel_id=303,
            announcement_channel_id=303,
            start_timestamp=600,
            three_minute_reminder_message_id=None,
            one_minute_reminder_message_id=None,
        )
        active = {
            "scrim-1": {
                "message": active_message,
                "messages": [active_message],
                "tasks": [],
            }
        }

        with (
            patch("main.active_idpw", active),
            patch("main.repository.get_idpw_config", return_value=config),
            patch("main.repository.get", return_value=scrim),
            patch("main.configured_text_channel", new=AsyncMock(return_value=channel)),
            patch("main.repository.set_idpw_announcement"),
        ):
            await clear_active_idpw("scrim-1")

        active_message.delete.assert_awaited_once_with()
        persisted_message.delete.assert_awaited_once_with()

    async def test_fixed_mode_uses_the_individual_match_assignment(self):
        scrim = self.make_scrim(
            match_maps=["Miramar", "Erangel", "Erangel", "Miramar"]
        )
        ctx = self.make_context()
        target_channel = SimpleNamespace(
            id=321,
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
            patch("main.repository.set_idpw_run"),
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
        target_channel = SimpleNamespace(
            id=321,
            send=AsyncMock(return_value=sent_message),
        )

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
            patch("main.repository.set_idpw_run"),
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

    async def test_failed_schedule_persistence_keeps_old_announcement(self):
        scrim = self.make_scrim()
        ctx = self.make_context()
        old_message = SimpleNamespace(id=111, delete=AsyncMock())
        old_state = {
            "message": old_message,
            "messages": [old_message],
            "tasks": [],
        }
        active_idpw[scrim.id] = old_state
        new_message = SimpleNamespace(id=222, delete=AsyncMock())
        target_channel = SimpleNamespace(
            id=321,
            send=AsyncMock(return_value=new_message),
        )
        config = SimpleNamespace(
            target_channel_id=321,
            fixed_password="legacy-secret",
            timezone_name="UTC",
        )
        clear_run = AsyncMock()
        feedback = AsyncMock()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.repository.get_idpw_config", return_value=config),
            patch("main.configured_text_channel", new=AsyncMock(return_value=target_channel)),
            patch(
                "main.repository.set_idpw_run",
                side_effect=ValueError("snapshot unavailable"),
            ),
            patch("main.clear_active_idpw", new=clear_run),
            patch("main.send_private_command_feedback", new=feedback),
            patch("main.time.time", return_value=0),
        ):
            await _publish_idpwg(ctx, 1, "ROOM-1 / 5")

        new_message.delete.assert_awaited_once_with()
        old_message.delete.assert_not_awaited()
        clear_run.assert_not_awaited()
        self.assertIs(active_idpw[scrim.id], old_state)
        feedback.assert_awaited_once()

    async def test_reminder_retries_transient_discord_failure(self):
        scrim = self.make_scrim()
        state = {"messages": [], "tasks": []}
        active = {scrim.id: state}
        response = SimpleNamespace(
            status=500,
            reason="Server Error",
            text="temporary error",
        )
        failure = discord.HTTPException(response, "temporary failure")
        reminder = SimpleNamespace(id=333, delete=AsyncMock())
        target_channel = SimpleNamespace(
            send=AsyncMock(side_effect=[failure, reminder])
        )

        with (
            patch("main.active_idpw", active),
            patch("main.asyncio.sleep", new=AsyncMock()),
            patch("main.time.time", return_value=0),
            patch("main.repository.set_idpw_reminder") as persist_reminder,
        ):
            await _send_scheduled_idpw_reminder(
                scrim,
                state,
                target_channel,
                start_timestamp=240,
                stage="one_minute",
                confirmed_role_id=456,
            )

        self.assertEqual(target_channel.send.await_count, 2)
        self.assertEqual(state["messages"], [reminder])
        persist_reminder.assert_called_once_with(scrim.id, "one_minute", 333)

    async def test_restart_restores_announcement_and_pending_reminders(self):
        scrim = self.make_scrim()
        announcement = SimpleNamespace(id=444)
        config = SimpleNamespace(
            scrim_id=scrim.id,
            start_timestamp=600,
            announcement_message_id=444,
            announcement_channel_id=321,
            three_minute_reminder_message_id=None,
            one_minute_reminder_message_id=None,
        )
        target_channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=announcement)
        )
        active = {}

        with (
            patch("main.repository.idpw_configs", {scrim.id: config}),
            patch("main.repository.get", return_value=scrim),
            patch("main.configured_text_channel", new=AsyncMock(return_value=target_channel)),
            patch("main.active_idpw", active),
            patch("main.time.time", return_value=0),
        ):
            await restore_active_idpw()
            restored = active[scrim.id]
            tasks = list(restored["tasks"])
            self.assertIs(restored["message"], announcement)
            self.assertEqual(len(tasks), 2)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()