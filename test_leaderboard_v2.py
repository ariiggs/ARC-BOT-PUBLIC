import tempfile
import unittest
import io
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
    _current_leaderboard_background_path,
    _restore_default_leaderboard_background,
    _read_leaderboard_background_metadata,
    _record_match_scores,
    _store_leaderboard_background,
    _write_leaderboard_background_metadata,
    LEADERBOARD_BACKGROUND,
    LEADERBOARD_ROW_HEIGHT,
    _fit_scrim_title,
    _load_font,
    bot,
    build_leaderboard_image,
    build_results_publication_message,
    calculate_leaderboard,
    leaderboard_canvas_dimensions,
    member_can_configure_scrim,
    parse_match_score_lines,
)
from scrim_state import (
    MatchScore,
    MAX_MATCHES,
    STATUS_CONFIRMED,
    LEADERBOARD_ORIENTATIONS,
    LEADERBOARD_TEAM_COUNTS,
    ScrimRepository,
    Slot,
    normalize_operational_messages,
)
from slot_storage import SlotStateStore


class LeaderboardV2Tests(unittest.IsolatedAsyncioTestCase):
    def test_leaderboard_font_loader_uses_montserrat_weight_variants(self):
        for weight, expected_style in (
            (400, "Regular"),
            (700, "Bold"),
            (800, "ExtraBold"),
        ):
            with self.subTest(weight=weight):
                font = _load_font(24, weight=weight)
                self.assertEqual(font.getname(), ("Montserrat", expected_style))

    def test_leaderboard_teams_and_scores_use_regular_weight(self):
        scrim = self.make_scrim()
        with patch("main._load_font", wraps=_load_font) as font_loader:
            build_leaderboard_image(
                scrim,
                [LeaderboardRow(1, "Alpha", 1, 2, 16, 18)],
            )

        used_weights = {
            call.kwargs.get("weight", 400)
            for call in font_loader.call_args_list
        }
        self.assertIn(400, used_weights)
        self.assertIn(800, used_weights)
        self.assertNotIn(700, used_weights)

    def make_scrim(self):
        return SimpleNamespace(
            id="a" * 16,
            guild_id=123,
            name="V2 Scrim",
            max_matches=MAX_MATCHES,
            kill_points_value=1,
            placement_points_string="10 6",
            leaderboard_layout="1_col",
            slots={
                1: Slot(
                    1,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha",
                    tag="A",
                    manager_id=101,
                    captain_1_id=101,
                ),
                2: Slot(
                    2,
                    status=STATUS_CONFIRMED,
                    team_name="Bravo",
                    tag="B",
                    manager_id=102,
                    captain_1_id=102,
                ),
                3: Slot(
                    3,
                    status=STATUS_CONFIRMED,
                    team_name="Charlie",
                    tag="C",
                    manager_id=103,
                    captain_1_id=103,
                ),
            },
            match_scores={
                (1, 1): MatchScore(1, 1, 2, 2),
                (1, 2): MatchScore(1, 2, 0, 1),
                (1, 3): MatchScore(1, 3, 3, 3),
                (2, 1): MatchScore(2, 1, 0, 1),
            },
        )

    def test_calculation_pads_placement_points_and_sorts_by_total(self):
        rows = calculate_leaderboard(self.make_scrim())

        self.assertEqual(
            rows,
            [
                LeaderboardRow(1, "Alpha", 1, 2, 16, 18),
                LeaderboardRow(2, "Bravo", 1, 0, 10, 10),
                LeaderboardRow(3, "Charlie", 0, 3, 0, 3),
            ],
        )

    def test_results_publication_formats_approved_tokens_and_missing_ranks(self):
        scrim = self.make_scrim()
        scrim.operational_messages = {
            "publish_results": (
                "{scrim}|{team_count}|{match_count}|"
                "{top1_team}:{top1_points}:{top1_kills}:{top1_wins}|"
                "{top2_team}:{top2_points}:{top2_kills}:{top2_wins}"
            )
        }

        message = build_results_publication_message(
            scrim,
            calculate_leaderboard(scrim)[:1],
        )

        self.assertEqual(
            message,
            "V2 Scrim|3|2|Alpha:18:2:1|—:0:0:0",
        )

    def test_results_template_accepts_only_approved_placeholders(self):
        template = (
            "{scrim} {team_count} {match_count} "
            "{top1_team} {top1_points} {top1_kills} {top1_wins} "
            "{top2_team} {top2_points} {top2_kills} {top2_wins} "
            "{top3_team} {top3_points} {top3_kills} {top3_wins}"
        )
        normalized = normalize_operational_messages(
            {"publish_results": template}
        )

        self.assertEqual(normalized["publish_results"], template)
        with self.assertRaisesRegex(ValueError, "unsupported placeholder"):
            normalize_operational_messages(
                {"publish_results": "{channel}"}
            )

    def test_team_limit_is_independent_of_registered_slot_count(self):
        scrim = self.make_scrim()
        for number in range(4, 18):
            scrim.slots[number] = Slot(
                number,
                status=STATUS_CONFIRMED,
                team_name=f"Team {number}",
                tag=f"T{number}",
                manager_id=100 + number,
            )
        scrim.match_scores[(1, 17)] = MatchScore(1, 17, 100, 1)
        scrim.leaderboard_team_count = 16

        rows = calculate_leaderboard(scrim)

        self.assertEqual(len(scrim.slots), 17)
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0].slot_number, 17)
        self.assertNotIn(16, [row.slot_number for row in rows])

    def test_invalid_score_lines_are_ignored(self):
        scores = parse_match_score_lines(
            "1 5 2\nbad\n2 -1 1\n2 3 0\n3 4 3\n3 5 2",
            match_number=1,
            scrim=self.make_scrim(),
        )

        self.assertEqual(
            scores,
            [
                MatchScore(1, 1, 5, 2),
                MatchScore(1, 3, 4, 3),
            ],
        )

    def test_repository_upsert_replaces_same_match_and_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(
                123,
                "Scores",
                1001,
                1002,
                slot_start=1,
                slot_end=1,
            )
            with repository.transaction():
                scrim.slots[1] = Slot(
                    1,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha",
                    tag="A",
                    manager_id=101,
                    captain_1_id=101,
                )
            repository.upsert_match_scores(
                scrim.id,
                123,
                1,
                [MatchScore(1, 1, 4, 2)],
            )
            repository.upsert_match_scores(
                scrim.id,
                123,
                1,
                [MatchScore(1, 1, 7, 1)],
            )

            self.assertEqual(
                repository.get_match_scores(scrim.id),
                [MatchScore(1, 1, 7, 1)],
            )

    def test_leaderboard_settings_persist_team_limit_layout_and_heights(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Display Settings", 1001, 1002)

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
            self.assertIn("No scrims have been created", empty_view.embed().description)
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
            self.assertIn(
                "Choose 16, 18, 20, 22, or 24 teams",
                team_view.children[0].placeholder,
            )
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

    def test_msg_panel_exposes_results_template_editor(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Results Panel", 1001, 1002)
            with patch("main.repository", repository):
                view = OperationalMessageView(
                    owner_id=456,
                    guild_id=123,
                    selected_id=scrim.id,
                )
                modal = ResultsMessageModal(view)
                field_names = [field.name for field in view.embed().fields]

        self.assertIn(
            "Results",
            [item.label for item in view.children if hasattr(item, "label")],
        )
        self.assertIn("Results publication", field_names)
        self.assertIn("{top1_team}", modal.template.default)

    def test_uploaded_background_is_used_for_rendered_leaderboard(self):
        scrim = self.make_scrim()
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            Image.new("RGB", (128, 128), (240, 20, 20)).save(
                background_dir / f"{scrim.id}.png"
            )
            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir):
                buffer = build_leaderboard_image(
                    scrim,
                    calculate_leaderboard(scrim),
                )
            with Image.open(buffer) as rendered:
                red, green, blue = rendered.getpixel((0, 0))
        self.assertGreater(red, green * 3)
        self.assertGreater(red, blue * 3)

    async def test_background_upload_is_saved_and_rehosted_for_a_stable_link(self):
        scrim = self.make_scrim()
        payload = io.BytesIO()
        Image.new("RGB", (128, 96), (25, 80, 140)).save(payload, format="PNG")

        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)

            class PreviewChannel:
                id = 555

                async def send(self, **kwargs):
                    return SimpleNamespace(
                        attachments=[
                            SimpleNamespace(
                                url="https://cdn.discordapp.com/current-background.png"
                            )
                        ],
                        channel=self,
                        id=777,
                    )

            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir):
                link = await _store_leaderboard_background(
                    scrim,
                    PreviewChannel(),
                    payload.getvalue(),
                )
                saved_metadata = _read_leaderboard_background_metadata(scrim.id)

            self.assertTrue((background_dir / f"{scrim.id}.png").is_file())
            self.assertEqual(
                link,
                "https://cdn.discordapp.com/current-background.png",
            )
            self.assertEqual(saved_metadata["attachment_url"], link)

    def test_restore_default_button_tracks_custom_background_presence(self):
        scrim = self.make_scrim()
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir):
                default_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="",
                )
                self.assertTrue(default_view.children[3].disabled)

                (background_dir / f"{scrim.id}.png").write_bytes(b"custom")
                custom_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="https://cdn.discordapp.com/custom.png",
                )
                self.assertFalse(custom_view.children[3].disabled)

    async def test_restore_default_removes_custom_image_and_replaces_preview(self):
        scrim = self.make_scrim()
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

            self.assertEqual(background_url, "https://cdn.discordapp.com/default-background.png")
            self.assertEqual(channel.filename, f"leaderboard-background-{scrim.id}.png")
            self.assertFalse(custom_path.exists())
            self.assertEqual(current_path, LEADERBOARD_BACKGROUND)
            self.assertEqual(metadata["attachment_url"], background_url)
            self.assertTrue(old_message.deleted)

    def test_leaderboard_access_is_limited_to_authorized_scrims(self):
        member = SimpleNamespace(roles=[SimpleNamespace(id=201)])
        scrim = SimpleNamespace(guild_id=123, staff_role_id=201)
        with (
            patch("main.member_is_head_staff", return_value=False),
            patch("main.member_is_staff", return_value=False),
        ):
            self.assertTrue(member_can_configure_scrim(member, scrim))
            scrim.staff_role_id = 202
            self.assertFalse(member_can_configure_scrim(member, scrim))

    def test_v22_snapshot_migrates_leaderboard_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Legacy Scores", 1001, 1002)
            repository.save_server_config(
                123,
                head_staff_role_id=2001,
                staff_role_id=2002,
                logs_channel_id=1003,
            )
            legacy = repository.payload()
            legacy["version"] = 22
            for field in (
                "kill_points_value",
                "placement_points_string",
                "leaderboard_layout",
                "match_scores",
            ):
                legacy["scrims"][0].pop(field)
            legacy["server_configs"][0].pop("license_type")
            store.save(legacy)

            restored = ScrimRepository(store)
            restored.load()
            loaded = restored.get(scrim.id)

            self.assertEqual(loaded.kill_points_value, 1)
            self.assertEqual(loaded.placement_points_string, "10 6 5 4 3 2 1")
            self.assertEqual(loaded.leaderboard_layout, "1_col")
            self.assertEqual(loaded.leaderboard_team_count, 24)
            self.assertEqual(loaded.leaderboard_orientation, "vertical")
            self.assertEqual(loaded.leaderboard_header_height, 180)
            self.assertEqual(loaded.leaderboard_footer_height, 120)
            self.assertEqual(loaded.match_scores, {})
            self.assertEqual(restored.get_server_config(123).license_type, "Standard")
            self.assertEqual(restored.payload()["version"], 27)

    async def test_score_command_reports_processed_teams(self):
        scrim = self.make_scrim()
        ctx = SimpleNamespace()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.repository.upsert_match_scores", return_value=2) as upsert,
            patch("main.send_private_command_feedback", new=AsyncMock()) as feedback,
        ):
            await _record_match_scores(ctx, 4, "1 5 2\n2 3 1")

        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.args[:3], (scrim.id, scrim.guild_id, 4))
        self.assertIn("Processed 2 teams.", feedback.call_args.args[1])

    async def test_score_command_rejects_match_above_scrim_limit(self):
        scrim = self.make_scrim()
        scrim.max_matches = 3
        ctx = SimpleNamespace()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.repository.upsert_match_scores") as upsert,
            patch("main.send_private_command_feedback", new=AsyncMock()) as feedback,
        ):
            await _record_match_scores(ctx, 4, "1 5 2")

        upsert.assert_not_called()
        self.assertIn("match limit is 3", feedback.call_args.args[1])

    def test_all_score_aliases_are_registered(self):
        for match_number in range(1, MAX_MATCHES + 1):
            self.assertIsNotNone(bot.get_command(f"resg{match_number}"))
        self.assertIsNone(bot.get_command(f"resg{MAX_MATCHES + 1}"))
        self.assertIsNotNone(bot.get_command("res"))
        self.assertIsNotNone(bot.get_command("leaderboard"))
        self.assertIsNotNone(bot.get_command("setres"))

    async def test_res_command_sends_final_png_directly(self):
        scrim = self.make_scrim()
        ctx = SimpleNamespace(send=AsyncMock())
        command = bot.get_command("res")

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.build_leaderboard_image", return_value=io.BytesIO(b"png")),
        ):
            await command.callback(ctx)

        ctx.send.assert_awaited_once()
        self.assertEqual(
            ctx.send.await_args.kwargs["file"].filename,
            "leaderboard.png",
        )
        self.assertIn("V2 Scrim Results", ctx.send.await_args.kwargs["content"])

    async def test_res_command_sends_an_empty_leaderboard_table(self):
        scrim = self.make_scrim()
        scrim.slots = {}
        scrim.match_scores = {}
        ctx = SimpleNamespace(send=AsyncMock())
        command = bot.get_command("res")

        with patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)):
            await command.callback(ctx)

        ctx.send.assert_awaited_once()
        image_file = ctx.send.await_args.kwargs["file"]
        self.assertEqual(image_file.filename, "leaderboard.png")
        with Image.open(image_file.fp) as rendered:
            self.assertEqual(
                rendered.size,
                leaderboard_canvas_dimensions(24, "vertical", 180, 120),
            )

    def test_vertical_image_uses_configured_count_and_canvas_dimensions(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 16
        scrim.leaderboard_header_height = 180
        scrim.leaderboard_footer_height = 120
        buffer = build_leaderboard_image(scrim, calculate_leaderboard(scrim))
        with Image.open(buffer) as rendered:
            self.assertEqual(
                rendered.size,
                leaderboard_canvas_dimensions(16, "vertical", 180, 120),
            )

    def test_row_height_is_48px_in_both_orientations(self):
        team_counts = (16, 18, 20, 22, 24)
        vertical_heights = [
            leaderboard_canvas_dimensions(count, "vertical", 180, 120)[1]
            for count in team_counts
        ]
        horizontal_heights = [
            leaderboard_canvas_dimensions(count, "horizontal", 180, 120)[1]
            for count in team_counts
        ]

        self.assertEqual(LEADERBOARD_ROW_HEIGHT, 48)
        self.assertEqual(vertical_heights[-1] - vertical_heights[0], 8 * 48)
        self.assertEqual(horizontal_heights[-1] - horizontal_heights[0], 4 * 48)

    def test_clean_leaderboard_background_is_the_default(self):
        from main import LEADERBOARD_BACKGROUND

        self.assertTrue(LEADERBOARD_BACKGROUND.is_file())
        self.assertEqual(LEADERBOARD_BACKGROUND.name, "leaderboard-background.png")

    def test_default_background_renders_scrim_name_for_each_layout(self):
        scrim = self.make_scrim()
        unnamed_scrim = SimpleNamespace(**vars(scrim))
        unnamed_scrim.name = ""

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

    def test_custom_background_does_not_render_scrim_name(self):
        scrim = self.make_scrim()
        unnamed_scrim = SimpleNamespace(**vars(scrim))
        unnamed_scrim.name = ""

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

    def test_gold_horizontal_layout_uses_two_column_canvas_dimensions(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 16
        scrim.leaderboard_orientation = "horizontal"

        with patch(
            "main.repository.get_server_config",
            return_value=SimpleNamespace(license_type="Gold"),
        ):
            buffer = build_leaderboard_image(
                scrim,
                calculate_leaderboard(scrim),
            )
        with Image.open(buffer) as rendered:
            self.assertEqual(
                rendered.size,
                leaderboard_canvas_dimensions(16, "horizontal", 180, 120),
            )
            self.assertGreater(rendered.size[0], rendered.size[1])

    def test_image_overlays_ranked_results_and_date_but_leaves_title_footer(self):
        scrim = self.make_scrim()
        with tempfile.TemporaryDirectory() as directory:
            background_path = Path(directory) / "white.png"
            Image.new("RGB", (128, 128), (255, 255, 255)).save(background_path)
            first = build_leaderboard_image(
                scrim,
                [LeaderboardRow(1, "Alpha", 1, 2, 16, 18)],
                background_path=background_path,
            )
            changed_scores = build_leaderboard_image(
                scrim,
                [LeaderboardRow(1, "Alpha", 8, 90, 200, 290)],
                background_path=background_path,
            )

        self.assertNotEqual(first.getvalue(), changed_scores.getvalue())
        with Image.open(first) as rendered:
            self.assertEqual(rendered.getpixel((540, 118)), (255, 255, 255))
            self.assertEqual(rendered.getpixel((540, 1528)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()