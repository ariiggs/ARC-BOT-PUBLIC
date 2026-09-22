import unittest
from types import SimpleNamespace

from setup_panel import _scrim_configuration_details


class SetupPanelTests(unittest.TestCase):
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