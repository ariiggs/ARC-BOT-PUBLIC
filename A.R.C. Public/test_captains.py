import tempfile
import unittest
from pathlib import Path

from scrim_state import STATUS_RESERVED, ScrimRepository
from slot_storage import SlotStateStore


class CaptainSlotTests(unittest.TestCase):
    def test_two_captains_are_persisted_and_clearable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()
            scrim = repository.create(
                123,
                "Test",
                10,
                11,
                cap_channel_id=12,
                slot_start=3,
                slot_end=3,
            )
            slot = scrim.slots[3]

            with repository.transaction():
                slot.status = STATUS_RESERVED
                slot.team_name = "Team"
                slot.tag = "TAG"
                slot.manager_id = 456
                slot.captain_1_id = 456
                slot.captain_2_id = 789

            reloaded = ScrimRepository(store)
            reloaded.load()
            restored = reloaded.get(scrim.id).slots[3]
            self.assertEqual(restored.manager_id, 456)
            self.assertEqual(restored.captain_1_id, 456)
            self.assertEqual(restored.captain_2_id, 789)
            self.assertEqual(reloaded.get(scrim.id).cap_channel_id, 12)

            with reloaded.transaction():
                restored.captain_2_id = None

            final = ScrimRepository(store)
            final.load()
            self.assertIsNone(final.get(scrim.id).slots[3].captain_2_id)

    def test_legacy_manager_id_becomes_captain_one(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            repository.load()
            scrim = repository.create(
                123,
                "Test",
                10,
                11,
                slot_start=3,
                slot_end=3,
            )
            slot = scrim.slots[3]
            with repository.transaction():
                slot.status = STATUS_RESERVED
                slot.team_name = "Team"
                slot.tag = "TAG"
                slot.manager_id = 456
                slot.captain_1_id = 456

            legacy = repository.payload()
            legacy["version"] = 13
            for entry in legacy["scrims"][0]["slots"]:
                entry.pop("captain_1_id")
                entry.pop("captain_2_id")
            store.save(legacy)

            migrated = ScrimRepository(store)
            migrated.load()
            restored = migrated.get(scrim.id).slots[3]
            self.assertEqual(restored.manager_id, 456)
            self.assertEqual(restored.captain_1_id, 456)
            self.assertIsNone(restored.captain_2_id)


if __name__ == "__main__":
    unittest.main()