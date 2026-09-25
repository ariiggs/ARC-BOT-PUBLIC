import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

from main import HELP_COPY_TEXT, HelpView, bot, build_help_text
from scrim_state import DEFAULT_MATCH_MAPS, MAX_MATCHES, ScrimRepository
from slot_storage import SlotStateStore


class V15UpdateTests(unittest.TestCase):
    def test_snapshots_with_more_than_25_matches_are_trimmed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Legacy Match Limit", 10, 11)
            payload = repository.payload()
            legacy_maps = [f"Map {number}" for number in range(1, 33)]
            payload["scrims"][0].update(
                max_matches=32,
                maps=legacy_maps,
                match_maps=legacy_maps,
                current_match_counter=32,
            )
            store.save(payload)

            migrated = ScrimRepository(store)
            migrated.load()
            restored = migrated.get(scrim.id)

            self.assertIsNotNone(restored)
            self.assertEqual(restored.max_matches, 25)
            self.assertEqual(len(restored.maps), 25)
            self.assertEqual(len(restored.match_maps), 25)
            self.assertEqual(restored.current_match_counter, 25)

    def test_v16_scrim_snapshot_gets_v15_defaults_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Friday Scrim", 10, 11)
            legacy = repository.payload()
            legacy["version"] = 16
            for field_name in (
                "timezone",
                "maps",
                "max_matches",
                "pw_type",
                "fixed_pw",
                "current_match_counter",
            ):
                legacy["scrims"][0].pop(field_name)
            store.save(legacy)

            migrated = ScrimRepository(store)
            migrated.load()
            restored = migrated.get(scrim.id)

            self.assertIsNotNone(restored)
            self.assertEqual(restored.timezone, "UTC+01:00")
            self.assertEqual(restored.maps, list(DEFAULT_MATCH_MAPS))
            self.assertEqual(restored.max_matches, 4)
            self.assertEqual(restored.pw_type, "fixed")
            self.assertEqual(restored.fixed_pw, "")
            self.assertEqual(restored.current_match_counter, 1)
        self.assertEqual(migrated.payload()["version"], 32)

    def test_help_text_contains_both_categories_and_fits_discord_message_limit(self):
        help_text = HELP_COPY_TEXT
        panel_text = build_help_text()

        self.assertTrue(panel_text.startswith(">>> "))
        self.assertIn("Choose a category", panel_text)
        self.assertIn("!setup", build_help_text("Getting started"))
        self.assertIn("!setup", help_text)
        self.assertIn("!register Team / TAG [/ @Captain]", help_text)
        self.assertIn("!cap add @User", help_text)
        self.assertIn("!cap transfer @User", help_text)
        self.assertIn("!cap remove @User", help_text)
        self.assertIn(f"!resg1-{MAX_MATCHES} slot kills", help_text)
        self.assertIn("best team first", help_text)
        self.assertIn("Omit teams that did not play", help_text)
        self.assertNotIn("!export", help_text)
        self.assertNotIn("!export", HELP_COPY_TEXT)
        self.assertIn("!editres <match>", help_text)
        self.assertIn("!editres <match>", HELP_COPY_TEXT)
        self.assertNotIn("```", panel_text)
        self.assertLessEqual(len(help_text), 2000)

    def test_specific_match_commands_are_registered(self):
        self.assertIsNotNone(bot.get_command("idpwg1"))
        self.assertIsNotNone(bot.get_command("idpwg25"))
        self.assertIsNone(bot.get_command("idpwg32"))
        self.assertIsNotNone(bot.get_command("editres"))
        self.assertIsNone(bot.get_command("export"))

    def test_dynamic_idpw_configuration_persists_without_a_fixed_password(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Dynamic Scrim", 10, 11)

            repository.save_idpw_config(
                scrim.id,
                target_channel_id=99,
                fixed_password="",
                timezone_name="UTC+02:00",
                password_type="dynamic",
            )

            restored = ScrimRepository(store)
            restored.load()
            saved_scrim = restored.get(scrim.id)
            saved_config = restored.get_idpw_config(scrim.id)

            self.assertIsNotNone(saved_scrim)
            self.assertEqual(saved_scrim.pw_type, "dynamic")
            self.assertEqual(saved_scrim.fixed_pw, "")
            self.assertIsNotNone(saved_config)
            self.assertEqual(saved_config.fixed_password, "")


class HelpViewTests(unittest.IsolatedAsyncioTestCase):
    def make_interaction(self, user_id=7):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                edit_message=AsyncMock(),
            ),
        )

    async def test_copy_button_returns_plain_copyable_help(self):
        view = HelpView(owner_id=7)
        interaction = self.make_interaction()
        copy_button = next(
            button for button in view.children if button.label == "Copy text"
        )

        await copy_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            HELP_COPY_TEXT,
            ephemeral=True,
            allowed_mentions=ANY,
        )

    async def test_close_button_removes_help_controls(self):
        view = HelpView(owner_id=7)
        interaction = self.make_interaction()
        close_button = next(
            button for button in view.children if button.label == "Close"
        )

        await close_button.callback(interaction)

        interaction.response.edit_message.assert_awaited_once_with(
            content="Help closed.",
            view=None,
        )


class HelpPanelCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_category_selector_renders_selected_help_category(self):
        view = HelpView(owner_id=7)
        category_select = next(
            item for item in view.children if hasattr(item, "options")
        )
        category_select._values = ["Getting started"]
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock())
        )

        await category_select.callback(interaction)

        interaction.response.edit_message.assert_awaited_once_with(
            content=build_help_text("Getting started"),
            view=view,
            allowed_mentions=ANY,
        )


class IdPwSnapshotTests(unittest.TestCase):
    def test_v31_idpw_snapshot_adds_schedule_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Migration", 10, 11)
            legacy = repository.payload()
            legacy["version"] = 31
            legacy["idpw_configs"] = [
                {
                    "scrim_id": scrim.id,
                    "target_channel_id": 99,
                    "fixed_password": "legacy-secret",
                    "timezone_name": "UTC+02:00",
                    "announcement_message_id": 777,
                }
            ]
            store.save(legacy)

            migrated = ScrimRepository(store)
            migrated.load()
            config = migrated.get_idpw_config(scrim.id)

            self.assertIsNotNone(config)
            self.assertIsNone(config.announcement_channel_id)
            self.assertIsNone(config.start_timestamp)
            self.assertIsNone(config.three_minute_reminder_message_id)
            self.assertIsNone(config.one_minute_reminder_message_id)
            self.assertEqual(migrated.payload()["version"], 32)

    def test_idpw_schedule_round_trip_and_config_update_preserve_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "IDPW", 10, 11)
            repository.save_idpw_config(
                scrim.id,
                target_channel_id=99,
                fixed_password="pw",
                timezone_name="UTC",
                password_type="fixed",
            )

            repository.set_idpw_run(
                scrim.id,
                announcement_message_id=777,
                announcement_channel_id=888,
                start_timestamp=1000,
            )
            repository.set_idpw_reminder(scrim.id, "three_minute", 901)
            repository.set_idpw_reminder(scrim.id, "one_minute", 902)
            repository.save_idpw_config(
                scrim.id,
                target_channel_id=199,
                fixed_password="new-pw",
                timezone_name="Europe/Zurich",
                password_type="fixed",
            )

            config = repository.get_idpw_config(scrim.id)
            self.assertEqual(config.target_channel_id, 199)
            self.assertEqual(config.fixed_password, "new-pw")
            self.assertEqual(config.announcement_message_id, 777)
            self.assertEqual(config.announcement_channel_id, 888)
            self.assertEqual(config.start_timestamp, 1000)
            self.assertEqual(config.three_minute_reminder_message_id, 901)
            self.assertEqual(config.one_minute_reminder_message_id, 902)

            restored = ScrimRepository(store)
            restored.load()
            reloaded_config = restored.get_idpw_config(scrim.id)
            self.assertEqual(reloaded_config.announcement_message_id, 777)
            self.assertEqual(reloaded_config.announcement_channel_id, 888)
            self.assertEqual(reloaded_config.start_timestamp, 1000)
            self.assertEqual(
                reloaded_config.three_minute_reminder_message_id,
                901,
            )
            self.assertEqual(
                reloaded_config.one_minute_reminder_message_id,
                902,
            )

            restored.set_idpw_run(
                scrim.id,
                announcement_message_id=778,
                announcement_channel_id=889,
                start_timestamp=2000,
            )
            replaced = restored.get_idpw_config(scrim.id)
            self.assertIsNone(replaced.three_minute_reminder_message_id)
            self.assertIsNone(replaced.one_minute_reminder_message_id)
            restored.set_idpw_announcement(scrim.id, None)
            cleared = restored.get_idpw_config(scrim.id)
            self.assertIsNone(cleared.announcement_message_id)
            self.assertIsNone(cleared.announcement_channel_id)
            self.assertIsNone(cleared.start_timestamp)
            self.assertIsNone(cleared.three_minute_reminder_message_id)
            self.assertIsNone(cleared.one_minute_reminder_message_id)


if __name__ == "__main__":
    unittest.main()