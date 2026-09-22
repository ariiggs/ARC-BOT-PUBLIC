import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import send_scrim_log, scrim_log_coverage_details
from main import repository
from scrim_state import STATUS_RESERVED, Slot


class ScrimLogRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrim_log_uses_scrim_channel_when_global_channel_is_empty(self):
        scrim = SimpleNamespace(
            guild_id=123,
            id="scrim-test",
            name="Test Scrim",
            logs_channel_id=456,
        )
        channel = SimpleNamespace(sent=[])

        async def send(content, **kwargs):
            channel.sent.append((content, kwargs))

        channel.send = send
        with (
            patch.object(
                repository,
                "get_server_config",
                return_value=SimpleNamespace(logs_channel_id=None),
            ),
            patch(
                "main.configured_text_channel",
                new=AsyncMock(return_value=channel),
            ),
        ):
            result = await send_scrim_log(scrim, "TEAM ADDED", "Slot 03")

        self.assertTrue(result)
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("TEAM ADDED", channel.sent[0][0])

    async def test_long_log_details_are_split_into_discord_sized_messages(self):
        scrim = SimpleNamespace(
            guild_id=123,
            id="scrim-test",
            name="Test Scrim",
            logs_channel_id=456,
        )
        channel = SimpleNamespace(sent=[])

        async def send(content, **kwargs):
            channel.sent.append((content, kwargs))

        channel.send = send
        details = "\n".join("x" * 250 for _ in range(12))
        with (
            patch.object(
                repository,
                "get_server_config",
                return_value=SimpleNamespace(logs_channel_id=None),
            ),
            patch(
                "main.configured_text_channel",
                new=AsyncMock(return_value=channel),
            ),
        ):
            result = await send_scrim_log(scrim, "AUDIT COVERAGE SNAPSHOT", details)

        self.assertTrue(result)
        self.assertGreater(len(channel.sent), 1)
        self.assertTrue(all(len(content) <= 2000 for content, _ in channel.sent))

    def test_coverage_snapshot_includes_new_scrim_and_captain_options(self):
        slot = Slot(
            3,
            status=STATUS_RESERVED,
            team_name="Alpha",
            tag="A",
            manager_id=111,
            captain_1_id=111,
            captain_2_id=222,
        )
        scrim = SimpleNamespace(
            public_channel_id=10,
            staff_channel_id=11,
            cap_channel_id=12,
            logs_channel_id=13,
            history_channel_id=14,
            staff_role_id=15,
            manager_role_id=16,
            is_open=True,
            slot_start=3,
            slot_end=5,
            emoji_available="⚪",
            emoji_reserved="🔵",
            emoji_pending="🟠",
            emoji_confirmed="🟢",
            slots={3: slot},
        )

        details = scrim_log_coverage_details(scrim)

        self.assertIn("Captain management channel: <#12>", details)
        self.assertIn("History channel: <#14>", details)
        self.assertIn("Slot 03 · team **Alpha**", details)
        self.assertIn("primary <@111> · co-captain <@222>", details)

    async def test_global_logs_channel_takes_precedence_when_configured(self):
        scrim = SimpleNamespace(
            guild_id=123,
            id="scrim-test",
            name="Test Scrim",
            logs_channel_id=456,
        )
        channel = SimpleNamespace(sent=[])

        async def send(content, **kwargs):
            channel.sent.append((content, kwargs))

        channel.send = send
        with (
            patch.object(
                repository,
                "get_server_config",
                return_value=SimpleNamespace(logs_channel_id=789),
            ),
            patch(
                "main.configured_text_channel",
                new=AsyncMock(return_value=channel),
            ),
        ):
            result = await send_scrim_log(scrim, "TEAM ADDED", "Slot 03")

        self.assertTrue(result)
        self.assertEqual(len(channel.sent), 1)


if __name__ == "__main__":
    unittest.main()