import tempfile
import unittest
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from main import (
    LeaderboardBackgroundDimensionsError,
    LeaderboardBlueprintOfferView,
    LeaderboardAccentColorModal,
    LeaderboardAccentColorView,
    LeaderboardRow,
    LeaderboardOrientationView,
    LeaderboardScrimSelectView,
    LeaderboardScrimEditView,
    LeaderboardSettingsView,
    LeaderboardTeamCountView,
    OperationalMessageView,
    ResultsMessageModal,
    _current_leaderboard_background_path,
    _build_empty_leaderboard_blueprint,
    _restore_default_leaderboard_background,
    _read_leaderboard_background_metadata,
    _record_match_scores,
    _store_leaderboard_background,
    _write_leaderboard_background_metadata,
    LEADERBOARD_BACKGROUND,
    LEADERBOARD_DATE_BADGE_TEMPLATE,
    LEADERBOARD_OUTER_MARGIN,
    LEADERBOARD_SECTION_GAP,
    LEADERBOARD_TABLE_HEADER_HEIGHT,
    LEADERBOARD_TABLE_TEMPLATE_DIR,
    HEX_COLOR_GENERATOR_URL,
    LEADERBOARD_ROW_HEIGHT,
    _load_font,
    bot,
    build_leaderboard_image,
    build_results_publication_message,
    calculate_leaderboard,
    leaderboard_canvas_dimensions,
    member_can_configure_scrim,
    parse_match_score_lines,
    _leaderboard_accent_rgb,
)
from scrim_state import (
    DEFAULT_LEADERBOARD_ACCENT_COLOR,
    DEFAULT_LEADERBOARD_FOOTER_HEIGHT,
    DEFAULT_LEADERBOARD_HEADER_HEIGHT,
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
        self.assertNotIn(700, used_weights)
        self.assertNotIn(800, used_weights)

    def make_scrim(self):
        return SimpleNamespace(
            id="a" * 16,
            guild_id=123,
            name="V2 Scrim",
            max_matches=MAX_MATCHES,
            kill_points_value=1,
            placement_points_string="10 6",
            leaderboard_layout="1_col",
            leaderboard_accent_color=DEFAULT_LEADERBOARD_ACCENT_COLOR,
            leaderboard_team_count=24,
            leaderboard_orientation="vertical",
            leaderboard_accent_colors={},
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

    def test_leaderboard_settings_keep_fixed_header_and_footer_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Display Settings", 1001, 1002)

            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_team_count=18,
                leaderboard_orientation="vertical",
                leaderboard_accent_color="#FFD700",
            )
            with self.assertRaisesRegex(ValueError, "fixed"):
                repository.update_leaderboard_settings(
                    scrim.id,
                    123,
                    leaderboard_header_height=220,
                )
            with self.assertRaisesRegex(ValueError, "fixed"):
                repository.update_leaderboard_settings(
                    scrim.id,
                    123,
                    leaderboard_footer_height=200,
                )

            restored = ScrimRepository(store)
            restored.load()
            settings = restored.get(scrim.id)
            self.assertEqual(settings.leaderboard_team_count, 18)
            self.assertEqual(settings.leaderboard_orientation, "vertical")
            self.assertEqual(
                settings.leaderboard_header_height,
                DEFAULT_LEADERBOARD_HEADER_HEIGHT,
            )
            self.assertEqual(
                settings.leaderboard_footer_height,
                DEFAULT_LEADERBOARD_FOOTER_HEIGHT,
            )
            self.assertEqual(settings.leaderboard_accent_color, "#FFD700")

    def test_v30_snapshot_migration_resets_custom_header_and_footer_heights(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Legacy Dimensions", 1001, 1002)
            legacy = repository.payload()
            legacy["version"] = 30
            legacy["scrims"][0]["leaderboard_header_height"] = 220
            legacy["scrims"][0]["leaderboard_footer_height"] = 200
            store.save(legacy)

            restored = ScrimRepository(store)
            restored.load()
            settings = restored.get(scrim.id)

        self.assertEqual(
            settings.leaderboard_header_height,
            DEFAULT_LEADERBOARD_HEADER_HEIGHT,
        )
        self.assertEqual(
            settings.leaderboard_footer_height,
            DEFAULT_LEADERBOARD_FOOTER_HEIGHT,
        )
        self.assertEqual(restored.payload()["version"], 31)

    def test_leaderboard_accent_color_rejects_nonstandard_hex(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Invalid Accent", 1001, 1002)

            for invalid in ("FF00FF", "#FFF", "#GG00FF", "#1234567", "#12 456"):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        repository.update_leaderboard_settings(
                            scrim.id,
                            123,
                            leaderboard_accent_color=invalid,
                        )

    def test_leaderboard_accent_color_hex_parser_is_strict(self):
        self.assertEqual(_leaderboard_accent_rgb("#FF00fF"), (255, 0, 255))
        for invalid in ("FF00FF", "#FFF", "#GG00FF", "#1234567"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _leaderboard_accent_rgb(invalid)

    async def test_setres_scrim_selection_opens_settings_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            repository.create(123, "Alpha Scrim", 1001, 1002)
            selected_scrim = repository.create(123, "Beta Scrim", 1003, 1004)
            with patch("main.repository", repository):
                view = LeaderboardScrimSelectView(
                    owner_id=456,
                    guild_id=123,
                    scrims=repository.list(123),
                )
                selector = view.children[0]
                selector._values = [selected_scrim.id]
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=456),
                    channel=SimpleNamespace(),
                    response=SimpleNamespace(defer=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )
                with (
                    patch("main.member_can_configure_scrim", return_value=True),
                    patch("main.is_active", return_value=True),
                    patch(
                        "main._ensure_leaderboard_background_preview",
                        new=AsyncMock(
                            return_value="https://cdn.discordapp.com/bg.png"
                        ),
                    ),
                ):
                    await selector.callback(interaction)

            opened_view = interaction.edit_original_response.await_args.kwargs["view"]
            self.assertIsInstance(opened_view, LeaderboardScrimEditView)
            self.assertEqual(opened_view.scrim_id, selected_scrim.id)
            self.assertEqual(opened_view.children[3].label, "Text Color")
            self.assertTrue(opened_view.children[3].disabled)

    async def test_custom_hex_opens_modal_and_validates_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "HEX Scrim", 1001, 1002)
            background_dir = Path(directory)
            (background_dir / f"{scrim.id}-vertical-24.png").write_bytes(
                b"custom background"
            )
            with (
                patch("main.repository", repository),
                patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir),
            ):
                color_view = LeaderboardAccentColorView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="",
                )
                selector = color_view.children[0]
                selector._values = ["custom"]
                modal_response = SimpleNamespace(send_modal=AsyncMock())
                await selector.callback(
                    SimpleNamespace(
                        response=modal_response,
                        message=SimpleNamespace(),
                    )
                )
                modal_response.send_modal.assert_awaited_once()
                modal = modal_response.send_modal.await_args.args[0]
                self.assertIsInstance(modal, LeaderboardAccentColorModal)

                invalid_response = SimpleNamespace(send_message=AsyncMock())
                modal.hex_code._value = "#12GGFF"
                await modal.on_submit(
                    SimpleNamespace(response=invalid_response)
                )
                invalid_message = invalid_response.send_message.await_args.args[0]
                self.assertIn(HEX_COLOR_GENERATOR_URL, invalid_message)
                self.assertEqual(
                    repository.get(scrim.id).leaderboard_accent_color,
                    DEFAULT_LEADERBOARD_ACCENT_COLOR,
                )

                prompt_message = SimpleNamespace(edit=AsyncMock())
                modal = LeaderboardAccentColorModal(
                    color_view=color_view,
                    prompt_message=prompt_message,
                )
                modal.hex_code._value = "#a12Bc3"
                response = SimpleNamespace(
                    defer=AsyncMock(),
                    send_message=AsyncMock(),
                )
                followup = SimpleNamespace(send=AsyncMock())
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=456),
                    guild_id=123,
                    response=response,
                    followup=followup,
                )
                with (
                    patch("main.member_can_configure_scrim", return_value=True),
                    patch("main.is_active", return_value=True),
                ):
                    await modal.on_submit(interaction)

            self.assertEqual(
                repository.get(scrim.id).leaderboard_accent_color,
                "#A12BC3",
            )
            response.defer.assert_awaited_once_with(
                ephemeral=True,
                thinking=True,
            )
            prompt_message.edit.assert_awaited_once()
            followup.send.assert_awaited_once()

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
                with patch.object(
                    repository,
                    "get_server_license_type",
                    return_value="Gold",
                ):
                    gold_edit_view = LeaderboardScrimEditView(
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
                    "get_server_license_type",
                    return_value="Gold",
                ):
                    gold_orientation_view = LeaderboardOrientationView(
                        owner_id=456,
                        guild_id=123,
                        scrim_id=scrim.id,
                        background_url="https://cdn.discordapp.com/background.png",
                    )
                color_view = LeaderboardAccentColorView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="https://cdn.discordapp.com/background.png",
                )
                color_modal = LeaderboardAccentColorModal(
                    color_view=color_view,
                    prompt_message=None,
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
                    "Text Color",
                    "Restore Default Background",
                    "Back to Dashboard",
                    "Blueprint",
                ],
            )
            self.assertTrue(edit_view.children[4].disabled)
            self.assertFalse(edit_view.children[6].disabled)
            self.assertTrue(edit_view.children[2].disabled)
            self.assertFalse(gold_edit_view.children[2].disabled)
            fields = {field.name: field.value for field in edit_fields}
            self.assertEqual(fields["Teams to Display"], "24")
            self.assertEqual(fields["Text Color"], "White (`#FFFFFF`)")
            self.assertIn("Built-in default", fields["Background"])
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
            self.assertEqual(
                [child.label for child in color_view.children],
                ["Custom HEX", "Reset to White", "Back to Leaderboard"],
            )
            self.assertIn(HEX_COLOR_GENERATOR_URL, color_view.embed().description)
            self.assertEqual(color_modal.hex_code.min_length, 7)
            self.assertEqual(color_modal.hex_code.max_length, 7)

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
                background_dir
                / (
                    f"{scrim.id}-{scrim.leaderboard_orientation}-"
                    f"{scrim.leaderboard_team_count}.png"
                )
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
        dimensions = leaderboard_canvas_dimensions(
            scrim.leaderboard_team_count,
            scrim.leaderboard_orientation,
            getattr(scrim, "leaderboard_header_height", DEFAULT_LEADERBOARD_HEADER_HEIGHT),
            getattr(scrim, "leaderboard_footer_height", DEFAULT_LEADERBOARD_FOOTER_HEIGHT),
        )
        Image.new("RGB", dimensions, (25, 80, 140)).save(payload, format="PNG")

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
                saved_metadata = _read_leaderboard_background_metadata(
                    scrim.id,
                    scrim.leaderboard_orientation,
                    scrim.leaderboard_team_count,
                )

            self.assertTrue(
                (
                    background_dir
                    / f"{scrim.id}-vertical-24.png"
                ).is_file()
            )
            self.assertEqual(
                link,
                "https://cdn.discordapp.com/current-background.png",
            )
            self.assertEqual(saved_metadata["attachment_url"], link)

    async def test_background_upload_requires_exif_normalized_profile_dimensions(self):
        scrim = self.make_scrim()
        payload = io.BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (80, 100), (25, 80, 140)).save(
            payload,
            format="JPEG",
            exif=exif,
        )

        with tempfile.TemporaryDirectory() as directory:
            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", Path(directory)):
                with self.assertRaises(
                    LeaderboardBackgroundDimensionsError
                ) as raised:
                    await _store_leaderboard_background(
                        scrim,
                        SimpleNamespace(send=AsyncMock()),
                        payload.getvalue(),
                    )

        self.assertEqual(raised.exception.actual, (100, 80))
        self.assertEqual(
            raised.exception.required,
            leaderboard_canvas_dimensions(
                scrim.leaderboard_team_count,
                scrim.leaderboard_orientation,
                getattr(scrim, "leaderboard_header_height", DEFAULT_LEADERBOARD_HEADER_HEIGHT),
                getattr(scrim, "leaderboard_footer_height", DEFAULT_LEADERBOARD_FOOTER_HEIGHT),
            ),
        )
        self.assertIn("100×80", str(raised.exception))

    def test_blueprint_matches_current_leaderboard_canvas(self):
        scrim = self.make_scrim()
        buffer, filename = _build_empty_leaderboard_blueprint(scrim)
        with Image.open(buffer) as blueprint:
            self.assertEqual(
                blueprint.size,
                leaderboard_canvas_dimensions(
                    scrim.leaderboard_team_count,
                    scrim.leaderboard_orientation,
                    getattr(scrim, "leaderboard_header_height", DEFAULT_LEADERBOARD_HEADER_HEIGHT),
                    getattr(scrim, "leaderboard_footer_height", DEFAULT_LEADERBOARD_FOOTER_HEIGHT),
                ),
            )
        self.assertIn(
            f"{scrim.leaderboard_team_count}-{scrim.leaderboard_orientation}",
            filename,
        )
        offer = LeaderboardBlueprintOfferView(
            owner_id=456,
            guild_id=scrim.guild_id,
            scrim_id=scrim.id,
        )
        self.assertEqual(
            [child.label for child in offer.children],
            ["Yes, send blueprint", "No"],
        )

    def test_text_color_overrides_are_scoped_to_orientation_and_team_count(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Profile Colors", 1001, 1002)
            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_accent_color="#FF4655",
            )
            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_team_count=16,
            )
            self.assertEqual(
                repository.get(scrim.id).leaderboard_accent_color,
                DEFAULT_LEADERBOARD_ACCENT_COLOR,
            )

            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_accent_color="#FFD700",
            )
            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_team_count=24,
            )
            self.assertEqual(
                repository.get(scrim.id).leaderboard_accent_color,
                "#FF4655",
            )

            repository.save_server_config(
                123,
                head_staff_role_id=2001,
                staff_role_id=2002,
                logs_channel_id=2003,
                license_type="Gold",
            )
            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_orientation="horizontal",
            )
            self.assertEqual(
                repository.get(scrim.id).leaderboard_accent_color,
                DEFAULT_LEADERBOARD_ACCENT_COLOR,
            )
            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_accent_color="#00AEFF",
            )
            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_orientation="vertical",
            )
            self.assertEqual(
                repository.get(scrim.id).leaderboard_accent_color,
                "#FF4655",
            )
            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_orientation="horizontal",
            )
            self.assertEqual(
                repository.get(scrim.id).leaderboard_accent_color,
                "#00AEFF",
            )

    def test_v28_custom_text_color_migrates_to_the_current_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Legacy Profile Color", 1001, 1002)
            legacy = repository.payload()
            legacy["version"] = 28
            legacy["scrims"][0]["leaderboard_team_count"] = 18
            legacy["scrims"][0]["leaderboard_accent_color"] = "#a12Bc3"
            legacy["scrims"][0].pop("leaderboard_accent_colors")
            store.save(legacy)

            restored = ScrimRepository(store)
            restored.load()

            loaded = restored.get(scrim.id)
            self.assertEqual(loaded.leaderboard_accent_color, "#A12BC3")
            self.assertEqual(
                loaded.leaderboard_accent_colors,
                {"vertical:18": "#A12BC3"},
            )
            self.assertEqual(restored.payload()["version"], 31)

    async def test_backgrounds_and_preview_links_are_isolated_by_profile(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 16

        def upload_payload(orientation):
            dimensions = leaderboard_canvas_dimensions(
                16,
                orientation,
                getattr(scrim, "leaderboard_header_height", DEFAULT_LEADERBOARD_HEADER_HEIGHT),
                getattr(scrim, "leaderboard_footer_height", DEFAULT_LEADERBOARD_FOOTER_HEIGHT),
            )
            payload = io.BytesIO()
            Image.new("RGB", dimensions, (25, 80, 140)).save(
                payload,
                format="PNG",
            )
            return payload.getvalue()

        vertical_payload = upload_payload("vertical")
        horizontal_payload = upload_payload("horizontal")

        class PreviewChannel:
            id = 555

            def __init__(self):
                self.preview_number = 0

            async def send(self, **kwargs):
                self.preview_number += 1
                return SimpleNamespace(
                    attachments=[
                        SimpleNamespace(
                            url=(
                                "https://cdn.discordapp.com/profile-"
                                f"{self.preview_number}.png"
                            )
                        )
                    ],
                    channel=self,
                    id=self.preview_number,
                )

        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            channel = PreviewChannel()
            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir):
                vertical_16_url = await _store_leaderboard_background(
                    scrim,
                    channel,
                    vertical_payload,
                )
                vertical_16_path = background_dir / f"{scrim.id}-vertical-16.png"

                scrim.leaderboard_orientation = "horizontal"
                horizontal_16_url = await _store_leaderboard_background(
                    scrim,
                    channel,
                    horizontal_payload,
                )
                horizontal_16_path = (
                    background_dir / f"{scrim.id}-horizontal-16.png"
                )

                scrim.leaderboard_orientation = "vertical"
                scrim.leaderboard_team_count = 18
                self.assertEqual(
                    _current_leaderboard_background_path(scrim),
                    LEADERBOARD_BACKGROUND,
                )
                scrim.leaderboard_team_count = 16
                self.assertEqual(
                    _current_leaderboard_background_path(scrim),
                    vertical_16_path,
                )
                self.assertEqual(
                    _read_leaderboard_background_metadata(
                        scrim.id,
                        "vertical",
                        16,
                    )["attachment_url"],
                    vertical_16_url,
                )

                await _restore_default_leaderboard_background(scrim, channel)
                self.assertEqual(
                    _current_leaderboard_background_path(scrim),
                    LEADERBOARD_BACKGROUND,
                )
                scrim.leaderboard_orientation = "horizontal"
                self.assertEqual(
                    _current_leaderboard_background_path(scrim),
                    horizontal_16_path,
                )
                self.assertEqual(
                    _read_leaderboard_background_metadata(
                        scrim.id,
                        "horizontal",
                        16,
                    )["attachment_url"],
                    horizontal_16_url,
                )

    def test_legacy_background_and_preview_migrate_only_to_active_profile(self):
        scrim = self.make_scrim()
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            legacy_image = background_dir / f"{scrim.id}.png"
            legacy_image.write_bytes(b"legacy custom background")
            legacy_url = "https://cdn.discordapp.com/legacy-background.png"
            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir):
                _write_leaderboard_background_metadata(
                    scrim.id,
                    {"attachment_url": legacy_url},
                )
                current_path = _current_leaderboard_background_path(scrim)
                migrated_metadata = _read_leaderboard_background_metadata(
                    scrim.id,
                    "vertical",
                    24,
                )
                other_profile_path = background_dir / f"{scrim.id}-vertical-18.png"

            self.assertEqual(current_path, background_dir / f"{scrim.id}-vertical-24.png")
            self.assertEqual(current_path.read_bytes(), b"legacy custom background")
            self.assertEqual(migrated_metadata["attachment_url"], legacy_url)
            self.assertFalse(legacy_image.exists())
            self.assertFalse((background_dir / f"{scrim.id}.json").exists())
            self.assertFalse(other_profile_path.exists())

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
                self.assertTrue(default_view.children[4].disabled)

                (
                    background_dir / f"{scrim.id}-vertical-24.png"
                ).write_bytes(b"custom")
                custom_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="https://cdn.discordapp.com/custom.png",
                )
                self.assertFalse(custom_view.children[4].disabled)

    async def test_restore_default_removes_custom_image_and_replaces_preview(self):
        scrim = self.make_scrim()
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            custom_path = background_dir / f"{scrim.id}-vertical-24.png"
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
                    scrim.leaderboard_orientation,
                    scrim.leaderboard_team_count,
                )
                background_url = await _restore_default_leaderboard_background(
                    scrim,
                    channel,
                )
                metadata = _read_leaderboard_background_metadata(
                    scrim.id,
                    scrim.leaderboard_orientation,
                    scrim.leaderboard_team_count,
                )
                current_path = _current_leaderboard_background_path(scrim)

            self.assertEqual(background_url, "https://cdn.discordapp.com/default-background.png")
            self.assertEqual(
                channel.filename,
                f"leaderboard-background-{scrim.id}-vertical-24.png",
            )
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
                "leaderboard_accent_color",
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
            self.assertEqual(
                loaded.leaderboard_accent_color,
                DEFAULT_LEADERBOARD_ACCENT_COLOR,
            )
            self.assertEqual(loaded.match_scores, {})
            self.assertEqual(restored.get_server_config(123).license_type, "Standard")
            self.assertEqual(restored.payload()["version"], 31)

    def test_v27_snapshot_migrates_default_accent_color(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "V27 Accent", 1001, 1002)
            legacy = repository.payload()
            legacy["version"] = 27
            legacy["scrims"][0].pop("leaderboard_accent_color")
            store.save(legacy)

            restored = ScrimRepository(store)
            restored.load()

            loaded = restored.get(scrim.id)
            self.assertEqual(
                loaded.leaderboard_accent_color,
                DEFAULT_LEADERBOARD_ACCENT_COLOR,
            )
            self.assertEqual(restored.payload()["version"], 31)

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

    def test_vertical_24_renders_all_24_registered_teams(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 24
        scrim.leaderboard_orientation = "vertical"
        scrim.slots = {
            number: Slot(
                number,
                status=STATUS_CONFIRMED,
                team_name=f"Team {number:02d}",
            )
            for number in range(1, 25)
        }

        rows = calculate_leaderboard(scrim)
        image_buffer = build_leaderboard_image(scrim, rows)

        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[-1].slot_number, 24)
        with Image.open(image_buffer) as rendered:
            self.assertEqual(
                rendered.size,
                leaderboard_canvas_dimensions(24, "vertical", 180, 120),
            )

    def test_renderer_generates_only_date_team_and_score_text(self):
        rows = [
            LeaderboardRow(number, f"Team {number:02d}", 0, 0, 0, 0)
            for number in range(1, 24)
        ]
        original_text = ImageDraw.ImageDraw.text

        for orientation in ("vertical", "horizontal"):
            with self.subTest(orientation=orientation):
                scrim = self.make_scrim()
                scrim.leaderboard_team_count = 24
                scrim.leaderboard_orientation = orientation
                rendered_text = []

                def record_text(drawer, xy, text, *args, **kwargs):
                    rendered_text.append(str(text))
                    return original_text(drawer, xy, text, *args, **kwargs)

                with (
                    patch.object(
                        ImageDraw.ImageDraw,
                        "text",
                        new=record_text,
                    ),
                    patch.object(
                        ImageDraw.ImageDraw,
                        "rounded_rectangle",
                        side_effect=AssertionError(
                            "Runtime renderer must not draw background shapes."
                        ),
                    ),
                    patch.object(
                        ImageDraw.ImageDraw,
                        "rectangle",
                        side_effect=AssertionError(
                            "Runtime renderer must not draw background shapes."
                        ),
                    ),
                    patch.object(
                        ImageDraw.ImageDraw,
                        "line",
                        side_effect=AssertionError(
                            "Runtime renderer must not draw background shapes."
                        ),
                    ),
                    patch(
                        "main.repository.get_server_license_type",
                        return_value="Gold",
                    ),
                ):
                    build_leaderboard_image(scrim, rows)

                self.assertRegex(rendered_text[0], r"^\d{2} [A-Z]{3} \d{4}$")
                self.assertEqual(rendered_text[1:6], ["Team 01", "0", "0", "0", "0"])
                self.assertNotIn("#", rendered_text[1:])
                self.assertNotIn("TEAM", rendered_text[1:])
                self.assertNotIn("24", rendered_text[1:])

    def test_leaderboard_text_color_changes_generated_text_only(self):
        scrim = self.make_scrim()
        rows = [LeaderboardRow(1, "Alpha", 1, 2, 16, 18)]
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            (background_dir / f"{scrim.id}-vertical-24.png").write_bytes(
                LEADERBOARD_BACKGROUND.read_bytes()
            )
            with patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir):
                blue = build_leaderboard_image(scrim, rows)
                scrim.leaderboard_accent_color = "#FFD700"
                gold = build_leaderboard_image(scrim, rows)

        self.assertNotEqual(blue.getvalue(), gold.getvalue())
        table_border_y = (
            LEADERBOARD_OUTER_MARGIN
            + getattr(scrim, "leaderboard_header_height", 180)
            + LEADERBOARD_SECTION_GAP
            + LEADERBOARD_TABLE_HEADER_HEIGHT // 2
        )
        with Image.open(blue) as blue_image, Image.open(gold) as gold_image:
            self.assertEqual(
                blue_image.getpixel((LEADERBOARD_OUTER_MARGIN, table_border_y)),
                gold_image.getpixel((LEADERBOARD_OUTER_MARGIN, table_border_y)),
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

    def test_first_slot_row_starts_at_same_height_in_both_orientations(self):
        team_positions = {"vertical": {}, "horizontal": {}}
        original_text = ImageDraw.ImageDraw.text

        def capture_team_position(drawer, xy, text, *args, **kwargs):
            if isinstance(text, str) and text.startswith("Team "):
                team_positions[orientation][text] = xy[1]
            return original_text(drawer, xy, text, *args, **kwargs)

        rows = [
            LeaderboardRow(number, f"Team {number:02d}", 1, 2, 16, 18)
            for number in range(1, 17)
        ]
        for orientation in ("vertical", "horizontal"):
            scrim = self.make_scrim()
            scrim.leaderboard_team_count = 16
            scrim.leaderboard_orientation = orientation
            # Simulate values retained by an older saved configuration.
            scrim.leaderboard_header_height = 260
            scrim.leaderboard_footer_height = 240
            with (
                patch.object(
                    ImageDraw.ImageDraw,
                    "text",
                    new=capture_team_position,
                ),
                patch(
                    "main.repository.get_server_license_type",
                    return_value="Gold",
                ),
            ):
                image_buffer = build_leaderboard_image(scrim, rows)

            with Image.open(image_buffer) as rendered:
                self.assertEqual(
                    rendered.size,
                    leaderboard_canvas_dimensions(16, orientation),
                )

        self.assertEqual(team_positions["vertical"]["Team 01"], 318)
        self.assertEqual(team_positions["horizontal"]["Team 01"], 318)
        self.assertEqual(team_positions["horizontal"]["Team 09"], 318)

    def test_static_dark_templates_include_all_configured_ranks(self):
        for orientation in ("vertical", "horizontal"):
            for team_count in LEADERBOARD_TEAM_COUNTS:
                with self.subTest(orientation=orientation, team_count=team_count):
                    capacity = (
                        (team_count + 1) // 2
                        if orientation == "horizontal"
                        else team_count
                    )
                    template_path = (
                        LEADERBOARD_TABLE_TEMPLATE_DIR
                        / f"table-{team_count}-{orientation}.png"
                    )
                    with Image.open(template_path) as template:
                        self.assertEqual(template.mode, "RGBA")
                        self.assertEqual(
                            template.size,
                            (
                                1920 if orientation == "horizontal" else 1080,
                                LEADERBOARD_TABLE_HEADER_HEIGHT
                                + capacity * LEADERBOARD_ROW_HEIGHT,
                            ),
                        )
                        self.assertEqual(
                            template.getpixel((100, 10)),
                            (13, 39, 57, 255),
                        )

        with Image.open(
            LEADERBOARD_TABLE_TEMPLATE_DIR / "table-24-vertical.png"
        ) as template:
            rank_24_top = (
                LEADERBOARD_TABLE_HEADER_HEIGHT + 23 * LEADERBOARD_ROW_HEIGHT
            )
            rank_pixels = template.crop(
                (45, rank_24_top, 85, rank_24_top + LEADERBOARD_ROW_HEIGHT)
            )
            muted_rank_pixels = sum(
                pixel[:3] == (143, 177, 202)
                for pixel in rank_pixels.get_flattened_data()
            )
            self.assertGreater(muted_rank_pixels, 0)
        with Image.open(LEADERBOARD_DATE_BADGE_TEMPLATE) as badge:
            self.assertEqual(badge.mode, "RGBA")
            self.assertGreater(badge.width, 40)
            self.assertEqual(badge.height, 40)

    def test_clean_leaderboard_background_is_the_default(self):
        from main import LEADERBOARD_BACKGROUND

        self.assertTrue(LEADERBOARD_BACKGROUND.is_file())
        self.assertEqual(LEADERBOARD_BACKGROUND.name, "leaderboard-background.png")

    def test_scrim_name_is_not_generated_on_the_default_background(self):
        scrim = self.make_scrim()
        unnamed_scrim = SimpleNamespace(**vars(scrim))
        unnamed_scrim.name = ""
        with_name = build_leaderboard_image(scrim, [])
        without_name = build_leaderboard_image(unnamed_scrim, [])
        self.assertEqual(with_name.getvalue(), without_name.getvalue())

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

    def test_gold_horizontal_layout_uses_two_column_canvas_dimensions(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 16
        scrim.leaderboard_orientation = "horizontal"

        with patch(
            "main.repository.get_server_license_type",
            return_value="Gold",
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

    def test_image_overlays_dynamic_results_and_date_but_leaves_header_footer(self):
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