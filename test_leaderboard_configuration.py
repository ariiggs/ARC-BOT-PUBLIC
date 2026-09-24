import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from main import (
    LeaderboardOrientationView,
    LeaderboardScrimEditView,
    LeaderboardSettingsView,
    LeaderboardTeamCountView,
    calculate_leaderboard,
    leaderboard_canvas_dimensions,
)
from scrim_state import MatchScore, STATUS_CONFIRMED, ScrimRepository, Slot
from slot_storage import SlotStateStore


class LeaderboardConfigurationTests(unittest.TestCase):
    def test_top_team_limit_is_independent_of_registered_slots(self):
        scrim = SimpleNamespace(
            kill_points_value=1,
            placement_points_string="10 6",
            leaderboard_team_count=16,
            max_matches=1,
            slots={},
            match_scores={},
        )
        for number in range(1, 18):
            scrim.slots[number] = Slot(
                number,
                status=STATUS_CONFIRMED,
                team_name=f"Team {number}",
                tag=f"T{number}",
            )
        scrim.match_scores[(1, 17)] = MatchScore(1, 17, 100, 1)

        rows = calculate_leaderboard(scrim)

        self.assertEqual(len(scrim.slots), 17)
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0].slot_number, 17)
        self.assertNotIn(16, [row.slot_number for row in rows])

    def test_settings_persist_and_dimensions_include_spacing_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Settings", 1001, 1002)

            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_team_count=18,
                leaderboard_orientation="vertical",
                leaderboard_header_height=220,
                leaderboard_footer_height=200,
            )

            restored = ScrimRepository(store)
            restored.load()
            settings = restored.get(scrim.id)

        self.assertEqual(settings.leaderboard_team_count, 18)
        self.assertEqual(settings.leaderboard_orientation, "vertical")
        self.assertEqual(settings.leaderboard_header_height, 220)
        self.assertEqual(settings.leaderboard_footer_height, 200)
        self.assertEqual(
            leaderboard_canvas_dimensions(16, "vertical", 180, 120),
            (1080, 1232),
        )
        self.assertEqual(
            leaderboard_canvas_dimensions(16, "horizontal", 180, 120),
            (1920, 848),
        )

    def test_setres_uses_dashboard_and_leaderboard_submenus(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Panel Settings", 1001, 1002)
            with patch("main.repository", repository):
                view = LeaderboardSettingsView(
                    owner_id=456,
                    guild_id=123,
                    scrims=[scrim],
                )
                empty_view = LeaderboardSettingsView(
                    owner_id=456,
                    guild_id=123,
                    scrims=[],
                    has_created_scrims=False,
                )
                edit_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="https://cdn.discordapp.com/background.png",
                )
                team_view = LeaderboardTeamCountView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="https://cdn.discordapp.com/background.png",
                )
                standard_orientation_view = LeaderboardOrientationView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="https://cdn.discordapp.com/background.png",
                )
                with patch.object(
                    repository,
                    "get_server_config",
                    return_value=SimpleNamespace(license_type="Gold"),
                ):
                    gold_orientation_view = LeaderboardOrientationView(
                        owner_id=456,
                        guild_id=123,
                        scrim_id=scrim.id,
                        background_url="https://cdn.discordapp.com/background.png",
                    )
                edit_fields = edit_view.embed().fields

        self.assertEqual(
            [child.label for child in view.children],
            ["Edit Leaderboard", "Close"],
        )
        self.assertFalse(view.children[0].disabled)
        self.assertTrue(empty_view.children[0].disabled)
        self.assertIn(
            "No scrims have been created",
            empty_view.embed().description,
        )
        self.assertEqual(
            [child.label for child in edit_view.children],
            [
                "Teams to Display",
                "Background",
                "Orientation",
                "Back to Dashboard",
            ],
        )
        fields = {field.name: field.value for field in edit_fields}
        self.assertEqual(fields["Teams to Display"], "24")
        self.assertIn("View current background", fields["Background"])
        self.assertEqual(
            [option.label for option in team_view.children[0].options],
            ["16 teams", "18 teams", "20 teams", "22 teams", "24 teams"],
        )
        self.assertEqual(
            [option.label for option in standard_orientation_view.children[0].options],
            ["Vertical"],
        )
        self.assertEqual(
            [option.label for option in gold_orientation_view.children[0].options],
            ["Vertical", "Horizontal"],
        )


if __name__ == "__main__":
    unittest.main()