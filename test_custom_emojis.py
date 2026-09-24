import tempfile
import unittest
from pathlib import Path

from global_setup import extract_raw_emoji
from main import slot_display_line
from scrim_state import (
    DEFAULT_EMOJI_AVAILABLE,
    DEFAULT_EMOJI_CONFIRMED,
    DEFAULT_EMOJI_PENDING,
    DEFAULT_EMOJI_RESERVED,
    EMOJI_FIELDS,
    ScrimRepository,
    STATUS_RESERVED,
    Slot,
)
from slot_storage import SlotStateStore


class CustomEmojiTests(unittest.TestCase):
    def test_custom_emoji_in_slot_line_is_outside_code_formatting(self):
        scrim = type(
            "ScrimStub",
            (),
            {
                "emoji_available": "⚪",
                "emoji_reserved": "<:wait:1551344081425145929>",
                "emoji_pending": "🟠",
                "emoji_confirmed": "🟢",
            },
        )()
        slot = Slot(
            3,
            status=STATUS_RESERVED,
            team_name="Test",
            tag="T",
            manager_id=123,
        )

        rendered = slot_display_line(scrim, slot)

        self.assertEqual(
            rendered,
            "`03`\u2007<:wait:1551344081425145929>\u2007Test | T | <@123>",
        )

    def test_slot_line_mentions_primary_and_co_captain(self):
        scrim = type(
            "ScrimStub",
            (),
            {
                "emoji_available": "⚪",
                "emoji_reserved": "🟡",
                "emoji_pending": "🟠",
                "emoji_confirmed": "🟢",
            },
        )()
        slot = Slot(
            3,
            status=STATUS_RESERVED,
            team_name="Test",
            tag="T",
            manager_id=123,
            captain_1_id=123,
            captain_2_id=456,
        )

        self.assertTrue(
            slot_display_line(scrim, slot).endswith("| <@123> / <@456>")
        )

    def test_extracts_custom_and_unicode_emoji(self):
        self.assertEqual(
            extract_raw_emoji("<@123> <:my_emoji:456>"), "<:my_emoji:456>"
        )
        self.assertEqual(extract_raw_emoji("<@123> 🔥"), "🔥")
        self.assertEqual(extract_raw_emoji("<@123> cancel"), None)

    def test_each_scrim_persists_its_own_four_custom_emojis(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            config = repository.save_scrims_staff_role(123, 456)
            first = repository.create(123, "First", 10, 11)
            second = repository.create(123, "Second", 12, 13)
            self.assertEqual(
                [getattr(first, field) for field in EMOJI_FIELDS],
                [
                    DEFAULT_EMOJI_AVAILABLE,
                    DEFAULT_EMOJI_RESERVED,
                    DEFAULT_EMOJI_PENDING,
                    DEFAULT_EMOJI_CONFIRMED,
                ],
            )
            updated = repository.save_scrim_emoji(
                first.id, 123, "emoji_pending", "<a:pending:789>"
            )
            self.assertEqual(updated.emoji_pending, "<a:pending:789>")
            self.assertEqual(second.emoji_pending, DEFAULT_EMOJI_PENDING)
            reset = repository.reset_scrim_emojis(first.id, 123)
            self.assertEqual(
                [getattr(reset, field) for field in EMOJI_FIELDS],
                [
                    DEFAULT_EMOJI_AVAILABLE,
                    DEFAULT_EMOJI_RESERVED,
                    DEFAULT_EMOJI_PENDING,
                    DEFAULT_EMOJI_CONFIRMED,
                ],
            )
            self.assertEqual(
                set(config.payload()),
                {
                    "guild_id",
                    "head_staff_role_id",
                    "staff_role_id",
                    "logs_channel_id",
                    "license_type",
                },
            )
            self.assertEqual(
                set(updated.payload()) & set(EMOJI_FIELDS),
                set(EMOJI_FIELDS),
            )
            self.assertEqual(
                set(updated.payload()),
                {
                    "id",
                    "guild_id",
                    "name",
                    "public_channel_id",
                    "staff_channel_id",
                    "staff_role_id",
                    "pending_role_id",
                    "confirmed_role_id",
                    "cap_channel_id",
                    "registration_channel_id",
                    "registration_role_id",
                    "registration_auto_accept",
                    "logs_channel_id",
                    "history_channel_id",
                    "emoji_available",
                    "emoji_reserved",
                    "emoji_pending",
                    "emoji_confirmed",
                    "public_message_id",
                    "staff_message_id",
                    "is_open",
                    "registration_open",
                    "operational_messages",
                    "operational_message_refs",
                    "slot_start",
                    "slot_end",
                    "slots",
                    "timezone",
                    "maps",
                    "max_matches",
                    "match_maps",
                    "pw_type",
                    "fixed_pw",
                    "current_match_counter",
                    "kill_points_value",
                    "placement_points_string",
                    "leaderboard_layout",
                    "leaderboard_background",
                    "leaderboard_accent_color",
                    "leaderboard_accent_colors",
                    "leaderboard_team_count",
                    "leaderboard_orientation",
                    "leaderboard_header_height",
                    "leaderboard_footer_height",
                    "match_scores",
                    "pending_registrations",
                },
            )

    def test_version_eight_global_emojis_are_migrated_to_each_scrim(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.save_scrims_staff_role(123, 456)
            first = repository.create(123, "First", 10, 11)
            second = repository.create(123, "Second", 12, 13)

            legacy = repository.payload()
            legacy["version"] = 8
            for entry in legacy["scrims"]:
                for field_name in EMOJI_FIELDS:
                    entry.pop(field_name)
            legacy["server_configs"][0].update(
                {
                    "emoji_available": "⚫",
                    "emoji_reserved": "🟣",
                    "emoji_pending": "🟡",
                    "emoji_confirmed": "🟤",
                }
            )
            store.save(legacy)

            migrated = ScrimRepository(store)
            migrated.load()
            self.assertEqual(migrated.get(first.id).emoji_available, "⚫")
            self.assertEqual(migrated.get(second.id).emoji_confirmed, "🟤")
            self.assertEqual(
                set(migrated.get_server_config(123).payload()),
                {
                    "guild_id",
                    "head_staff_role_id",
                    "staff_role_id",
                    "logs_channel_id",
                    "license_type",
                },
            )
            self.assertEqual(migrated.payload()["version"], 30)


if __name__ == "__main__":
    unittest.main()