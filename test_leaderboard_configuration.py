import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw
from main import (
    LeaderboardRow,
    LeaderboardOrientationView,
    LeaderboardScrimEditView,
    LeaderboardSettingsView,
    LeaderboardTeamCountView,
    OperationalMessageView,
    ResultsMessageModal,
    LEADERBOARD_BACKGROUND,
    LEADERBOARD_ROW_HEIGHT,
    _current_leaderboard_background_path,
    _fit_scrim_title,
    _load_font,
    _read_leaderboard_background_metadata,
    _restore_default_leaderboard_background,
    _write_leaderboard_background_metadata,
    bot,
    build_leaderboard_image,
    calculate_leaderboard,
    build_results_publication_message,
    leaderboard_canvas_dimensions,
)
from scrim_state import (
    MatchScore,
    STATUS_CONFIRMED,
    LEADERBOARD_ORIENTATIONS,
    LEADERBOARD_TEAM_COUNTS,
    ScrimRepository,
    Slot,
    normalize_operational_messages,
)
from slot_storage import SlotStateStore


class LeaderboardConfigurationTests(unittest.TestCase):
    def test_leaderboard_font_loader_uses_montserrat_weight_variants(self):
        for weight, expected_style in (
            (400, "Regular"),
            (700, "Bold"),
            (800, "ExtraBold"),
        ):
            with self.subTest(weight=weight):
                font = _load_font(24, weight=weight)
                self.assertEqual(font.getname(), ("Montserrat", expected_style))

    def test_results_publication_uses_template_and_missing_rank_defaults(self):
        scrim = SimpleNamespace(
            id="a" * 16,
            name="Example",
            slots={
                1: SimpleNamespace(status=STATUS_CONFIRMED, team_name="Alpha"),
            },
            match_scores={},
            operational_messages={
                "publish_results": (
                    "{scrim}|{team_count}|{match_count}|"
                    "{top1_team}:{top1_points}:{top1_kills}:{top1_wins}|"
                    "{top2_team}:{top2_points}:{top2_kills}:{top2_wins}"
                )
            },
        )
        rows = [LeaderboardRow(1, "Alpha", 2, 7, 10, 17)]

        self.assertEqual(
            build_results_publication_message(scrim, rows),
            "Example|1|0|Alpha:17:7:2|—:0:0:0",
        )

    def test_results_template_rejects_unapproved_channel_placeholder(self):
        with self.assertRaisesRegex(ValueError, "unsupported placeholder"):
            normalize_operational_messages(
                {"publish_results": "{channel}"}
            )

    def test_msg_panel_includes_results_template_editor(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Results", 1001, 1002)
            with patch("main.repository", repository):
                view = OperationalMessageView(
                    owner_id=456,
                    guild_id=123,
                    selected_id=scrim.id,
                )
                modal = ResultsMessageModal(view)
                labels = [
                    item.label for item in view.children if hasattr(item, "label")
                ]
                fields = [field.name for field in view.embed().fields]

        self.assertIn("Results", labels)
        self.assertIn("Results publication", fields)
        self.assertIn("{top1_team}", modal.template.default)

    def test_rendered_leaderboard_changes_when_score_columns_change(self):
        scrim = SimpleNamespace(
            leaderboard_team_count=16,
            leaderboard_orientation="vertical",
            leaderboard_header_height=180,
            leaderboard_footer_height=120,
            timezone="UTC",
        )
        with tempfile.TemporaryDirectory() as directory:
            background_path = Path(directory) / "white.png"
            Image.new("RGB", (128, 128), (255, 255, 255)).save(background_path)
            first = build_leaderboard_image(
                scrim,
                [LeaderboardRow(1, "Alpha", 1, 2, 16, 18)],
                background_path=background_path,
            )
            second = build_leaderboard_image(
                scrim,
                [LeaderboardRow(1, "Alpha", 2, 5, 20, 25)],
                background_path=background_path,
            )

        self.assertNotEqual(first.getvalue(), second.getvalue())

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
        self.assertEqual(LEADERBOARD_ROW_HEIGHT, 48)

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
                "Restore Default Background",
                "Back to Dashboard",
            ],
        )
        self.assertTrue(edit_view.children[3].disabled)
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


class LeaderboardBackgroundResetTests(unittest.IsolatedAsyncioTestCase):
    def test_restore_default_button_tracks_custom_background_presence(self):
        scrim_id = "a" * 16
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir):
                default_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim_id,
                    background_url="",
                )
                self.assertTrue(default_view.children[3].disabled)

                (background_dir / f"{scrim_id}.png").write_bytes(b"custom")
                custom_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim_id,
                    background_url="https://cdn.discordapp.com/custom.png",
                )
                self.assertFalse(custom_view.children[3].disabled)

    async def test_restore_default_removes_custom_image_and_replaces_preview(self):
        scrim = SimpleNamespace(id="b" * 16, guild_id=123, name="Example Scrim")
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            custom_path = background_dir / f"{scrim.id}.png"
            custom_path.write_bytes(b"custom background")
            old_message = SimpleNamespace(deleted=False)

            async def delete_old_message():
                old_message.deleted = True

            old_message.delete = delete_old_message

            class OldPreviewChannel:
                async def fetch_message(self, message_id):
                    self.message_id = message_id
                    return old_message

            old_channel = OldPreviewChannel()

            class PreviewChannel:
                id = 555

                def __init__(self):
                    self.filename = None

                async def send(self, **kwargs):
                    self.filename = kwargs["file"].filename
                    return SimpleNamespace(
                        attachments=[
                            SimpleNamespace(
                                url="https://cdn.discordapp.com/default-background.png"
                            )
                        ],
                        channel=self,
                        id=777,
                    )

            channel = PreviewChannel()
            with (
                patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir),
                patch("main.bot.get_channel", return_value=old_channel),
            ):
                _write_leaderboard_background_metadata(
                    scrim.id,
                    {
                        "channel_id": 444,
                        "message_id": 333,
                        "attachment_url": "https://cdn.discordapp.com/custom.png",
                    },
                )
                background_url = await _restore_default_leaderboard_background(
                    scrim,
                    channel,
                )
                metadata = _read_leaderboard_background_metadata(scrim.id)
                current_path = _current_leaderboard_background_path(scrim)

            self.assertEqual(
                background_url,
                "https://cdn.discordapp.com/default-background.png",
            )
            self.assertEqual(
                channel.filename,
                f"leaderboard-background-{scrim.id}.png",
            )
            self.assertFalse(custom_path.exists())
            self.assertEqual(current_path, LEADERBOARD_BACKGROUND)
            self.assertEqual(metadata["attachment_url"], background_url)
            self.assertTrue(old_message.deleted)


class LeaderboardEmptyResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_res_sends_an_empty_leaderboard_table(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Empty Results", 1001, 1002)
            ctx = SimpleNamespace(send=AsyncMock())

            with (
                patch("main.repository", repository),
                patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            ):
                await bot.get_command("res").callback(ctx)

            ctx.send.assert_awaited_once()
            image_file = ctx.send.await_args.kwargs["file"]
            self.assertEqual(image_file.filename, "leaderboard.png")
            with Image.open(image_file.fp) as rendered:
                self.assertEqual(
                    rendered.size,
                    leaderboard_canvas_dimensions(24, "vertical", 180, 120),
                )


class LeaderboardScrimNameRenderingTests(unittest.TestCase):
    @staticmethod
    def make_scrim(name):
        return SimpleNamespace(
            id="c" * 16,
            guild_id=123,
            name=name,
            slots={},
            match_scores={},
        )

    def test_default_background_renders_scrim_name_for_each_layout(self):
        scrim = self.make_scrim("Example Scrim")
        unnamed_scrim = self.make_scrim("")

        def header_pixels(image_buffer):
            with Image.open(image_buffer) as rendered:
                return rendered.crop((0, 0, rendered.width, 220)).tobytes()

        with patch(
            "main.repository.get_server_config",
            return_value=SimpleNamespace(license_type="Gold"),
        ):
            for team_count in LEADERBOARD_TEAM_COUNTS:
                for orientation in LEADERBOARD_ORIENTATIONS:
                    scrim.leaderboard_team_count = team_count
                    scrim.leaderboard_orientation = orientation
                    unnamed_scrim.leaderboard_team_count = team_count
                    unnamed_scrim.leaderboard_orientation = orientation
                    with_name = build_leaderboard_image(scrim, [])
                    without_name = build_leaderboard_image(unnamed_scrim, [])
                    self.assertNotEqual(
                        header_pixels(with_name),
                        header_pixels(without_name),
                        f"Missing default-background scrim name for "
                        f"{team_count} teams, {orientation} layout",
                    )

    def test_scrim_title_uses_max_size_for_short_names_and_shrinks_long_names(self):
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        long_name = "A Very Long Scrim Name That Needs To Fit"
        short_title, short_font = _fit_scrim_title(
            draw,
            "BETA",
            max_width=600,
            max_height=164,
        )
        fitted_long_title, long_font = _fit_scrim_title(
            draw,
            long_name,
            max_width=600,
            max_height=164,
        )

        self.assertEqual(short_title, "BETA")
        self.assertEqual(short_font.size, 80)
        self.assertEqual(fitted_long_title, long_name)
        self.assertLess(long_font.size, short_font.size)

    def test_custom_background_does_not_render_scrim_name(self):
        scrim = self.make_scrim("Example Scrim")
        unnamed_scrim = self.make_scrim("")

        with tempfile.TemporaryDirectory() as directory:
            custom_background = Path(directory) / "custom-background.png"
            custom_background.write_bytes(LEADERBOARD_BACKGROUND.read_bytes())
            with_name = build_leaderboard_image(
                scrim,
                [],
                background_path=custom_background,
            )
            without_name = build_leaderboard_image(
                unnamed_scrim,
                [],
                background_path=custom_background,
            )

        with Image.open(with_name) as named_image, Image.open(
            without_name
        ) as unnamed_image:
            self.assertEqual(named_image.tobytes(), unnamed_image.tobytes())


if __name__ == "__main__":
    unittest.main()