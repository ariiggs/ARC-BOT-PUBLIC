import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path

from setup_panel import _scrim_configuration_details
from scrim_state import ScrimRepository
from slot_storage import SlotStateStore


class SetupPanelTests(unittest.TestCase):
    def test_legacy_null_registration_mode_migrates_before_channel_update(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(
                123,
                "Legacy Registration",
                101,
                102,
                cap_channel_id=103,
                logs_channel_id=104,
                history_channel_id=105,
            )
            legacy = repository.payload()
            legacy["version"] = 18
            legacy["scrims"][0]["registration_channel_id"] = None
            legacy["scrims"][0]["registration_role_id"] = None
            legacy["scrims"][0]["registration_auto_accept"] = None
            store.save(legacy)

            migrated = ScrimRepository(store)
            migrated.load()
            restored = migrated.get(scrim.id)
            self.assertIsNotNone(restored)
            self.assertFalse(restored.registration_auto_accept)

            migrated.update_scrim(
                scrim.id,
                123,
                registration_channel_id=106,
                registration_role_id=None,
                registration_auto_accept=restored.registration_auto_accept,
            )
            self.assertEqual(migrated.get(scrim.id).registration_channel_id, 106)

    def test_update_normalizes_an_old_in_memory_null_registration_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "In Memory Legacy", 101, 102)
            scrim.registration_auto_accept = None

            repository.update_scrim(
                scrim.id,
                123,
                registration_channel_id=106,
            )

            updated = repository.get(scrim.id)
            self.assertEqual(updated.registration_channel_id, 106)
            self.assertFalse(updated.registration_auto_accept)

    def test_registration_channel_update_persists_atomic_registration_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(
                123,
                "Friday Scrim",
                101,
                102,
                cap_channel_id=103,
                logs_channel_id=104,
                history_channel_id=105,
                registration_role_id=201,
                registration_auto_accept=True,
            )

            repository.update_scrim(
                scrim.id,
                123,
                registration_channel_id=106,
                registration_role_id=scrim.registration_role_id,
                registration_auto_accept=scrim.registration_auto_accept,
            )

            restored_repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            restored_repository.load()
            restored = restored_repository.get(scrim.id)

            self.assertIsNotNone(restored)
            self.assertEqual(restored.registration_channel_id, 106)
            self.assertEqual(restored.registration_role_id, 201)
            self.assertTrue(restored.registration_auto_accept)

    def test_configuration_details_reads_idpw_from_supplied_repository(self):
        scrim = SimpleNamespace(
            id="scrim-1",
            name="Friday Scrim",
            public_channel_id=101,
            staff_channel_id=102,
            cap_channel_id=103,
            logs_channel_id=104,
            history_channel_id=105,
            staff_role_id=201,
            pending_role_id=202,
            confirmed_role_id=203,
            slot_start=3,
            slot_end=25,
            timezone="UTC+01:00",
            pw_type="fixed",
            maps=["Erangel", "Miramar"],
            max_matches=2,
            current_match_counter=1,
        )
        repository = SimpleNamespace(
            get_idpw_config=lambda scrim_id: SimpleNamespace(
                target_channel_id=999
            )
        )

        details = _scrim_configuration_details(scrim, repository)

        self.assertIn("ID/PW target: <#999>", details)
        self.assertIn("Map rotation: **2 Matches Configured**", details)
        self.assertIn("Current match: **1** / 2", details)


if __name__ == "__main__":
    unittest.main()