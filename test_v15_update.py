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
        self.assertEqual(migrated.payload()["version"], 30)

    def test_help_text_contains_both_categories_and_fits_discord_message_limit(self):
        help_text = build_help_text()

        self.assertTrue(help_text.startswith(">>> "))
        self.assertIn("!setup", help_text)
        self.assertIn("!setres", help_text)
        self.assertIn("!register Team / TAG [/ @Manager]", help_text)
        self.assertIn("!cap add|transfer|remove @User", help_text)
        self.assertIn(f"!resg1-{MAX_MATCHES} slot kills placement", help_text)
        self.assertIn(f"!resg1-{MAX_MATCHES} slot kills placement", HELP_COPY_TEXT)
        self.assertIn("!setres", HELP_COPY_TEXT)
        self.assertNotIn("```", help_text)
        self.assertLessEqual(len(help_text), 1000)

    def test_specific_match_commands_are_registered(self):
        self.assertIsNotNone(bot.get_command("idpwg1"))
        self.assertIsNotNone(bot.get_command("idpwg25"))
        self.assertIsNone(bot.get_command("idpwg32"))

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


if __name__ == "__main__":
    unittest.main()