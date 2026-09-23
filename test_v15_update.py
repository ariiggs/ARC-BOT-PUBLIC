import tempfile
import unittest
from pathlib import Path

from main import bot, build_help_copy_text, build_help_embed
from scrim_state import DEFAULT_MATCH_MAPS, ScrimRepository
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
            self.assertEqual(migrated.payload()["version"], 19)

    def test_help_embed_has_requested_categories_and_commands(self):
        embed = build_help_embed()

        self.assertEqual(embed.title, "A.R.C. Bot - Command Center")
        field_names = [field.name for field in embed.fields]
        self.assertEqual(field_names, ["🛠️ Staff", "🎖️ Captains"])
        staff = embed.fields[0].value
        captains = embed.fields[1].value
        self.assertIn("!add Team / TAG / @Captain", staff)
        self.assertIn("multiple lines to bulk-add teams", staff)
        self.assertIn("!say <message>", staff)
        self.assertIn("!slots", staff)
        self.assertIn("!remind", staff)
        self.assertIn("!register Team Name / Tag [/ @Manager]", captains)
        self.assertIn("only members with the configured registration role", captains)
        self.assertNotIn("captains and members", captains)
        self.assertIn("!cap add", captains)
        self.assertIn("Confirm", captains)
        self.assertNotIn("`!slots", captains)
        self.assertNotIn("`!remind", captains)

    def test_help_copy_text_contains_both_categories_and_fits_discord_message_limit(self):
        copy_text = build_help_copy_text()

        self.assertIn("STAFF", copy_text)
        self.assertIn("CAPTAINS", copy_text)
        self.assertIn("multiple lines to bulk-add teams", copy_text)
        self.assertIn("!register Team Name / Tag [/ @Manager]", copy_text)
        self.assertIn("only members with the configured registration role", copy_text)
        self.assertLessEqual(len(copy_text), 2000)

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


if __name__ == "__main__":
    unittest.main()