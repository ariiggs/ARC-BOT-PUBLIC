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
    MatchScoreCorrectionModal,
    MatchScoreCorrectionReviewView,
    MatchScoreCorrectionView,
    MatchScoreSubmissionEditModal,
    MatchScoreSubmissionReviewView,
    OperationalMessageView,
    ResultsMessageModal,
    _current_leaderboard_background_path,
    _ensure_leaderboard_background_preview,
    _leaderboard_scrim_profile,
    _fit_leaderboard_cell_text,
    _build_empty_leaderboard_blueprint,
    _leaderboard_field_ranges,
    _load_leaderboard_background_canvas,
    _load_dimensioned_leaderboard_blueprint,
    _restore_default_leaderboard_background,
    _read_leaderboard_background_metadata,
    _record_match_scores,
    normalize_score_submission_text,
    _store_leaderboard_background,
    _write_leaderboard_background_metadata,
    LEADERBOARD_BACKGROUND,
    LEADERBOARD_BLUEPRINT_DIR,
    LEADERBOARD_OUTER_MARGIN,
    LEADERBOARD_SECTION_GAP,
    LEADERBOARD_TABLE_HEADER_HEIGHT,
    LEADERBOARD_TITLE_MAX_FONT_SIZE,
    LEADERBOARD_HORIZONTAL_RANK_CELL_CENTER_OFFSETS,
    LEADERBOARD_ROW_TEXT_VERTICAL_OFFSET,
    LEADERBOARD_HORIZONTAL_TEAM_NAME_LEFT_PADDING,
    LEADERBOARD_TEAM_NAME_LEFT_PADDING,
    HEX_COLOR_GENERATOR_URL,
    LEADERBOARD_VERTICAL_ROW_HEIGHT,
    LEADERBOARD_HORIZONTAL_ROW_HEIGHT,
    _load_font,
    bot,
    build_leaderboard_image,
    build_results_publication_message,
    calculate_leaderboard,
    leaderboard_canvas_dimensions,
    member_can_configure_scrim,
    missing_score_matches,
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

    def test_leaderboard_rows_use_bold_weight(self):
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
        self.assertIn(700, used_weights)
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

    def test_registered_teams_without_results_remain_zero_score_rows(self):
        scrim = self.make_scrim()
        scrim.match_scores = {
            (1, 1): MatchScore(1, 1, 4, 1),
        }

        rows = calculate_leaderboard(scrim)

        self.assertEqual(
            rows,
            [
                LeaderboardRow(1, "Alpha", 1, 4, 10, 14),
                LeaderboardRow(2, "Bravo", 0, 0, 0, 0),
                LeaderboardRow(3, "Charlie", 0, 0, 0, 0),
            ],
        )

    def test_results_publication_formats_approved_tokens_and_missing_ranks(self):
        scrim = self.make_scrim()
        scrim.max_matches = 2
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

    def test_results_publication_warns_about_missing_matches_only(self):
        scrim = self.make_scrim()
        scrim.max_matches = 4
        missing = missing_score_matches(scrim)

        self.assertEqual(missing, [3, 4])
        message = build_results_publication_message(
            scrim,
            calculate_leaderboard(scrim),
        )
        self.assertIn("no scores are saved for match(es) 3, 4", message)

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

        with patch("main.repository.get_server_license_type", return_value="Gold"):
            rows = calculate_leaderboard(scrim)

        self.assertEqual(len(scrim.slots), 17)
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0].slot_number, 17)
        self.assertNotIn(16, [row.slot_number for row in rows])

    def test_ordered_score_lines_assign_placement_from_rank_order(self):
        scores = parse_match_score_lines(
            "3 5\n\n1 4",
            match_number=1,
            scrim=self.make_scrim(),
        )

        self.assertEqual(
            scores,
            [
                MatchScore(1, 3, 5, 1),
                MatchScore(1, 1, 4, 2),
            ],
        )

    def test_legacy_explicit_placement_format_remains_supported(self):
        scores = parse_match_score_lines(
            "1 5 2\n3 4 3",
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

    def test_malformed_score_lines_reject_the_entire_submission(self):
        invalid_inputs = (
            "3 5\nbad",
            "3 5\n1 -1",
            "3 5\n3 4",
            "3 5\n1 4 2",
        )
        for input_string in invalid_inputs:
            with self.subTest(input_string=input_string):
                with self.assertRaises(ValueError):
                    parse_match_score_lines(
                        input_string,
                        match_number=1,
                        scrim=self.make_scrim(),
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
                slot_end=2,
            )
            with repository.transaction():
                for number, name in ((1, "Alpha"), (2, "Bravo")):
                    scrim.slots[number] = Slot(
                        number,
                        status=STATUS_CONFIRMED,
                        team_name=name,
                        tag=name[0],
                        manager_id=100 + number,
                        captain_1_id=100 + number,
                    )
            repository.upsert_match_scores(
                scrim.id,
                123,
                1,
                [
                    MatchScore(1, 1, 4, 2),
                    MatchScore(1, 2, 0, 3),
                ],
            )
            repository.upsert_match_scores(
                scrim.id,
                123,
                1,
                [MatchScore(1, 1, 7, 1)],
            )

            self.assertEqual(
                repository.get_match_scores(scrim.id),
                [
                    MatchScore(1, 1, 7, 1),
                    MatchScore(1, 2, 0, 3),
                ],
            )

    def test_replacing_match_results_clears_omitted_slots_only_for_that_match(self):
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
                slot_end=2,
            )
            with repository.transaction():
                for number, name in ((1, "Alpha"), (2, "Bravo")):
                    scrim.slots[number] = Slot(
                        number,
                        status=STATUS_CONFIRMED,
                        team_name=name,
                        tag=name[0],
                        manager_id=100 + number,
                        captain_1_id=100 + number,
                    )
            repository.upsert_match_scores(
                scrim.id,
                123,
                1,
                [
                    MatchScore(1, 1, 4, 2),
                    MatchScore(1, 2, 3, 1),
                ],
            )
            repository.upsert_match_scores(
                scrim.id,
                123,
                2,
                [MatchScore(2, 2, 1, 2)],
            )

            repository.replace_match_scores(
                scrim.id,
                123,
                1,
                [MatchScore(1, 1, 8, 1)],
            )

            self.assertEqual(
                repository.get_match_scores(scrim.id),
                [
                    MatchScore(1, 1, 8, 1),
                    MatchScore(2, 2, 1, 2),
                ],
            )

    def test_leaderboard_settings_keep_fixed_header_and_footer_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStateStore(Path(directory) / "state.sqlite3")
            repository = ScrimRepository(store)
            scrim = repository.create(123, "Display Settings", 1001, 1002)

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

    def test_standard_license_locks_team_count_but_allows_both_orientations(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Standard Layout", 1001, 1002)

            with self.assertRaisesRegex(ValueError, "locked to 20"):
                repository.update_leaderboard_settings(
                    scrim.id,
                    123,
                    leaderboard_team_count=18,
                )

            repository.update_leaderboard_settings(
                scrim.id,
                123,
                leaderboard_team_count=20,
                leaderboard_orientation="horizontal",
            )
            self.assertEqual(scrim.leaderboard_team_count, 20)
            self.assertEqual(scrim.leaderboard_orientation, "horizontal")

            with self.assertRaisesRegex(ValueError, "locked to 20"):
                repository.update_leaderboard_settings(
                    scrim.id,
                    123,
                    leaderboard_team_count=24,
                )

            with patch.object(
                repository,
                "get_server_license_type",
                return_value="Gold",
            ):
                repository.update_leaderboard_settings(
                    scrim.id,
                    123,
                    leaderboard_team_count=24,
                )
            self.assertEqual(scrim.leaderboard_team_count, 24)

    def test_standard_license_transitions_preserve_horizontal_orientation(self):
        for transition in ("license_change", "revocation"):
            with self.subTest(transition=transition):
                with tempfile.TemporaryDirectory() as directory:
                    repository = ScrimRepository(
                        SlotStateStore(Path(directory) / "state.sqlite3")
                    )
                    scrim = repository.create(
                        123,
                        "Horizontal Transition",
                        1001,
                        1002,
                    )
                    repository.authorize_guild(123, license_type="Gold")
                    repository.update_leaderboard_settings(
                        scrim.id,
                        123,
                        leaderboard_team_count=24,
                        leaderboard_orientation="horizontal",
                    )

                    if transition == "license_change":
                        repository.authorize_guild(
                            123,
                            license_type="Standard",
                        )
                    else:
                        repository.revoke_guild(123)

                    self.assertEqual(
                        repository.get_server_license_type(123),
                        "Standard",
                    )
                    self.assertEqual(
                        scrim.leaderboard_orientation,
                        "horizontal",
                    )
                    with patch("main.repository", repository):
                        self.assertEqual(
                            _leaderboard_scrim_profile(scrim),
                            ("horizontal", 20),
                        )

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
        self.assertEqual(restored.payload()["version"], 32)

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
            self.assertFalse(opened_view.children[3].disabled)

    async def test_setres_edit_button_opens_without_a_bundled_default_image(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Default Background Scrim", 1001, 1002)
            missing_default = Path(directory) / "missing-default.png"
            channel = SimpleNamespace(send=AsyncMock())
            with (
                patch("main.repository", repository),
                patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", Path(directory)),
                patch("main.LEADERBOARD_BACKGROUND", missing_default),
                patch("main.member_can_configure_scrim", return_value=True),
            ):
                view = LeaderboardSettingsView(
                    owner_id=456,
                    guild_id=123,
                    scrims=[scrim],
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=456),
                    channel=channel,
                    response=SimpleNamespace(defer=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )
                await view.children[0].callback(interaction)

            interaction.response.defer.assert_awaited_once()
            interaction.edit_original_response.assert_awaited_once()
            channel.send.assert_not_awaited()
            opened_view = (
                interaction.edit_original_response.await_args.kwargs["view"]
            )
            self.assertIsInstance(opened_view, LeaderboardScrimEditView)
            self.assertEqual(opened_view.scrim_id, scrim.id)

    async def test_default_background_preview_skips_missing_generated_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "No Default Image", 1001, 1002)
            missing_default = Path(directory) / "missing-default.png"
            channel = SimpleNamespace(send=AsyncMock())
            with (
                patch("main.repository", repository),
                patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", Path(directory)),
                patch("main.LEADERBOARD_BACKGROUND", missing_default),
            ):
                preview_url = await _ensure_leaderboard_background_preview(
                    scrim,
                    channel,
                )

            self.assertEqual(preview_url, "")
            channel.send.assert_not_awaited()

    async def test_custom_hex_opens_modal_and_validates_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "HEX Scrim", 1001, 1002)
            background_dir = Path(directory)
            (background_dir / f"{scrim.id}-vertical-20.png").write_bytes(
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

    async def test_standard_text_color_is_editable_and_used_without_custom_background(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Standard Text Color", 1001, 1002)
            background_dir = Path(directory) / "backgrounds"
            background_dir.mkdir()
            text_fills = []
            original_text = ImageDraw.ImageDraw.text

            def record_text(drawer, xy, text, *args, **kwargs):
                text_fills.append(kwargs.get("fill"))
                return original_text(drawer, xy, text, *args, **kwargs)

            with (
                patch("main.repository", repository),
                patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir),
                patch("main.member_can_configure_scrim", return_value=True),
                patch("main.is_active", return_value=True),
            ):
                edit_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="",
                )
                self.assertFalse(edit_view.children[3].disabled)

                color_view = LeaderboardAccentColorView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="",
                )
                response = SimpleNamespace(
                    edit_message=AsyncMock(),
                    send_message=AsyncMock(),
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=456),
                    guild_id=123,
                    response=response,
                )
                await color_view.save_color(interaction, "#12ABCD")

                self.assertEqual(
                    repository.get(scrim.id).leaderboard_accent_colors[
                        "vertical:20"
                    ],
                    "#12ABCD",
                )
                with patch.object(
                    ImageDraw.ImageDraw,
                    "text",
                    new=record_text,
                ):
                    rendered = build_leaderboard_image(
                        scrim,
                        [LeaderboardRow(1, "Alpha", 1, 2, 16, 18)],
                    )

            self.assertIn(
                _leaderboard_accent_rgb("#12ABCD"),
                text_fills,
            )
            with Image.open(rendered) as image:
                self.assertEqual(
                    image.size,
                    leaderboard_canvas_dimensions(20, "vertical"),
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
                    gold_team_view = LeaderboardTeamCountView(
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
                    "Teams to Display · Gold",
                    "Background",
                    "Orientation",
                    "Text Color",
                    "Restore Default Background",
                    "Back to Dashboard",
                    "Blueprint",
                ],
            )
            self.assertTrue(edit_view.children[0].disabled)
            self.assertFalse(gold_edit_view.children[0].disabled)
            self.assertTrue(edit_view.children[4].disabled)
            self.assertFalse(edit_view.children[6].disabled)
            self.assertFalse(edit_view.children[2].disabled)
            self.assertFalse(gold_edit_view.children[2].disabled)
            fields = {field.name: field.value for field in edit_fields}
            self.assertEqual(fields["Teams to Display"], "20")
            self.assertEqual(fields["Text Color"], "White (`#FFFFFF`)")
            self.assertIn("Built-in default", fields["Background"])
            self.assertIn(
                "Choose 16, 18, 20, 22, or 24 teams",
                gold_team_view.children[0].placeholder,
            )
            self.assertEqual(team_view.children[0].label, "Locked to 20 Teams")
            self.assertEqual(
                [option.label for option in gold_team_view.children[0].options],
                ["16 teams", "18 teams", "20 teams", "22 teams", "24 teams"],
            )
            self.assertEqual(
                [option.label for option in standard_orientation_view.children[0].options],
                ["Vertical", "Horizontal"],
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
        scrim.leaderboard_team_count = 20
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
        scrim.leaderboard_team_count = 20
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
                    / f"{scrim.id}-vertical-20.png"
                ).is_file()
            )
            self.assertEqual(
                link,
                "https://cdn.discordapp.com/current-background.png",
            )
            self.assertEqual(saved_metadata["attachment_url"], link)

    async def test_background_upload_requires_exif_normalized_profile_dimensions(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 20
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
                    20,
                    scrim.leaderboard_orientation,
                    getattr(scrim, "leaderboard_header_height", DEFAULT_LEADERBOARD_HEADER_HEIGHT),
                    getattr(scrim, "leaderboard_footer_height", DEFAULT_LEADERBOARD_FOOTER_HEIGHT),
                ),
            )
            self.assertEqual(blueprint.getpixel((0, 0)), (13, 18, 26))
        self.assertIn(
            f"20-{scrim.leaderboard_orientation}",
            filename,
        )
        offer = LeaderboardBlueprintOfferView(
            owner_id=456,
            guild_id=scrim.guild_id,
            scrim_id=scrim.id,
        )
        self.assertEqual(
            [child.label for child in offer.children],
            ["Yes, send both blueprints", "No"],
        )

    def test_dimensioned_vertical_16_blueprint_matches_updated_profile(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 16
        scrim.leaderboard_orientation = "vertical"
        with patch(
            "main.repository.get_server_license_type",
            return_value="Gold",
        ):
            buffer, filename = _load_dimensioned_leaderboard_blueprint(scrim)
        self.assertEqual(
            filename,
            "leaderboard-blueprint-vertical-16-dimensions.png",
        )
        self.assertEqual(
            buffer.getvalue(),
            (
                LEADERBOARD_BLUEPRINT_DIR
                / "vertical-16-complete-dimensions.png"
            ).read_bytes(),
        )
        with Image.open(buffer) as reference:
            self.assertEqual(reference.size, (1080, 1400))

    def test_dimensioned_blueprints_exist_for_every_profile(self):
        for orientation in ("vertical", "horizontal"):
            for team_count in (16, 18, 20, 22, 24):
                with self.subTest(orientation=orientation, team_count=team_count):
                    scrim = self.make_scrim()
                    scrim.leaderboard_orientation = orientation
                    scrim.leaderboard_team_count = team_count
                    with patch(
                        "main.repository.get_server_license_type",
                        return_value="Gold",
                    ):
                        buffer, filename = _load_dimensioned_leaderboard_blueprint(
                            scrim
                        )
                    self.assertEqual(
                        filename,
                        f"leaderboard-blueprint-{orientation}-{team_count}-dimensions.png",
                    )
                    expected_size = leaderboard_canvas_dimensions(
                        team_count,
                        orientation,
                    )
                    with Image.open(buffer) as reference:
                        self.assertEqual(reference.size, expected_size)

    async def test_wrong_dimension_offer_sends_both_blueprints(self):
        scrim = self.make_scrim()
        scrim.deleted = False
        offer = LeaderboardBlueprintOfferView(
            owner_id=456,
            guild_id=scrim.guild_id,
            scrim_id=scrim.id,
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock())
        )

        with patch("main.repository.get", return_value=scrim):
            await offer.children[0].callback(interaction)

        sent = interaction.response.send_message.await_args
        self.assertIn(
            "dimensioned 20-team Vertical reference (1080 × 1640 px)",
            sent.args[0],
        )
        self.assertIn("not an upload background", sent.args[0])
        self.assertEqual(
            [attachment.filename for attachment in sent.kwargs["files"]],
            [
                "leaderboard-blueprint-20-vertical-1080x1640.png",
                "leaderboard-blueprint-vertical-20-dimensions.png",
            ],
        )

    async def test_leaderboard_panel_blueprint_button_sends_both_files(self):
        scrim = self.make_scrim()
        scrim.deleted = False
        panel = LeaderboardScrimEditView(
            owner_id=456,
            guild_id=scrim.guild_id,
            scrim_id=scrim.id,
            background_url="",
        )
        blueprint_button = next(
            child for child in panel.children if child.label == "Blueprint"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                guild_permissions=SimpleNamespace(administrator=True),
                roles=(),
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch(
                "main.repository.get_server_license_type",
                return_value="Gold",
            ),
        ):
            await blueprint_button.callback(interaction)

        sent = interaction.response.send_message.await_args
        self.assertIn(
            "dimensioned 24-team Vertical reference (1080 × 1880 px)",
            sent.args[0],
        )
        self.assertEqual(
            [attachment.filename for attachment in sent.kwargs["files"]],
            [
                "leaderboard-blueprint-24-vertical-1080x1880.png",
                "leaderboard-blueprint-vertical-24-dimensions.png",
            ],
        )

    def test_text_color_overrides_are_scoped_to_orientation_and_team_count(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ScrimRepository(
                SlotStateStore(Path(directory) / "state.sqlite3")
            )
            scrim = repository.create(123, "Profile Colors", 1001, 1002)
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
            self.assertEqual(restored.payload()["version"], 32)

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
            with (
                patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir),
                patch(
                    "main.repository.get_server_license_type",
                    return_value="Gold",
                ),
            ):
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
        scrim.leaderboard_team_count = 20
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
                    20,
                )
                other_profile_path = background_dir / f"{scrim.id}-vertical-18.png"

            self.assertEqual(current_path, background_dir / f"{scrim.id}-vertical-20.png")
            self.assertEqual(current_path.read_bytes(), b"legacy custom background")
            self.assertEqual(migrated_metadata["attachment_url"], legacy_url)
            self.assertFalse(legacy_image.exists())
            self.assertFalse((background_dir / f"{scrim.id}.json").exists())
            self.assertFalse(other_profile_path.exists())

    def test_restore_default_button_tracks_custom_background_presence(self):
        scrim = self.make_scrim()
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            with (
                patch("main.LEADERBOARD_BACKGROUND_UPLOAD_DIR", background_dir),
                patch("main.repository.get", return_value=scrim),
            ):
                default_view = LeaderboardScrimEditView(
                    owner_id=456,
                    guild_id=123,
                    scrim_id=scrim.id,
                    background_url="",
                )
                self.assertTrue(default_view.children[4].disabled)

                (
                    background_dir / f"{scrim.id}-vertical-20.png"
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
        scrim.leaderboard_team_count = 20
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            custom_path = background_dir / f"{scrim.id}-vertical-20.png"
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
                f"leaderboard-background-{scrim.id}-vertical-20.png",
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
            self.assertEqual(restored.payload()["version"], 32)

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
            self.assertEqual(restored.payload()["version"], 32)

    async def test_score_command_requires_confirmation_before_replacing_match(self):
        scrim = self.make_scrim()
        progress_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=456),
            send=AsyncMock(return_value=progress_message),
        )

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch(
                "main.repository.replace_match_scores",
                return_value=2,
            ) as replace,
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await _record_match_scores(ctx, 4, "1 5\n3 3")

        replace.assert_not_called()
        review = progress_message.edit.call_args.kwargs["view"]
        self.assertIsInstance(review, MatchScoreSubmissionReviewView)
        self.assertEqual(
            progress_message.edit.call_args.kwargs["embed"].title,
            "Review Match 4 results",
        )
        self.assertIn("01.** Slot 01 · Alpha", review.embed().description)
        self.assertIn("02.** Slot 03 · Charlie", review.embed().description)
        self.assertIn("**5** kills", review.embed().description)
        self.assertEqual(
            [button.label for button in review.children],
            ["Confirm", "Edit", "Cancel"],
        )

        confirm_button = next(
            button for button in review.children if button.label == "Confirm"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )
        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.repository.replace_match_scores", return_value=2) as replace,
        ):
            await confirm_button.callback(interaction)

        replace.assert_called_once_with(
            scrim.id,
            scrim.guild_id,
            4,
            [MatchScore(4, 1, 5, 1), MatchScore(4, 3, 3, 2)],
        )
        self.assertIn(
            "Processed 2 teams (reviewed by <@7>).",
            interaction.response.edit_message.call_args.kwargs["content"],
        )

    async def test_score_command_does_not_save_when_any_line_is_invalid(self):
        scrim = self.make_scrim()
        progress_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=progress_message))

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.repository.replace_match_scores") as replace,
            patch("main.delete_command_message", new=AsyncMock()),
        ):
            await _record_match_scores(ctx, 1, "1 5\nbad")

        replace.assert_not_called()
        self.assertIn(
            "Nothing was saved.",
            progress_message.edit.call_args.kwargs["content"],
        )

    async def test_score_review_edit_opens_full_command_and_repreviews_changes(self):
        scrim = self.make_scrim()
        review = MatchScoreSubmissionReviewView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=4,
            scores=[MatchScore(4, 1, 5, 1)],
            raw_input="1 5",
        )
        review.message = SimpleNamespace(edit=AsyncMock())
        edit_button = next(
            button for button in review.children if button.label == "Edit"
        )
        edit_interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(
                send_modal=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
        ):
            await edit_button.callback(edit_interaction)

        modal = edit_interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, MatchScoreSubmissionEditModal)
        self.assertEqual(modal.command_input.default, "!resg4\n1 5")
        modal.command_input._value = "!resg4\n3 8\n1 2"
        updated_message = SimpleNamespace()
        modal_interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(send_message=AsyncMock()),
            original_response=AsyncMock(return_value=updated_message),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.repository.replace_match_scores") as replace,
        ):
            await modal.on_submit(modal_interaction)

        replace.assert_not_called()
        updated_review = modal_interaction.response.send_message.call_args.kwargs[
            "view"
        ]
        self.assertIsInstance(updated_review, MatchScoreSubmissionReviewView)
        self.assertEqual(
            list(updated_review.scores),
            [MatchScore(4, 3, 8, 1), MatchScore(4, 1, 2, 2)],
        )
        self.assertEqual(updated_review.message, updated_message)
        review.message.edit.assert_awaited_once()
        self.assertTrue(review.completed)

    async def test_score_review_rejects_confirmation_after_saved_scores_change(self):
        scrim = self.make_scrim()
        review = MatchScoreSubmissionReviewView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=4,
            scores=[MatchScore(4, 1, 5, 1)],
            raw_input="1 5",
        )
        scrim.match_scores[(4, 1)] = MatchScore(4, 1, 2, 1)
        confirm_button = next(
            button for button in review.children if button.label == "Confirm"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.repository.replace_match_scores") as replace,
        ):
            await confirm_button.callback(interaction)

        replace.assert_not_called()
        self.assertIn(
            "out of date",
            interaction.response.edit_message.call_args.kwargs["content"],
        )

    def test_score_submission_edit_accepts_the_full_command_prefix(self):
        self.assertEqual(
            normalize_score_submission_text(
                "!resg4\n1 5\n3 3",
                4,
            ),
            "1 5\n3 3",
        )
        self.assertEqual(
            normalize_score_submission_text("!resg4 1 5\n3 3", 4),
            "1 5\n3 3",
        )

    async def test_score_command_rejects_match_above_scrim_limit(self):
        scrim = self.make_scrim()
        scrim.max_matches = 3
        ctx = SimpleNamespace()

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.repository.replace_match_scores") as replace,
            patch("main.send_private_command_feedback", new=AsyncMock()) as feedback,
        ):
            await _record_match_scores(ctx, 4, "1 5 2")

        replace.assert_not_called()
        self.assertIn("match limit is 3", feedback.call_args.args[1])

    async def test_editres_command_shows_a_team_selector_for_the_match(self):
        scrim = self.make_scrim()
        sent_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel=SimpleNamespace(id=456),
            send=AsyncMock(return_value=sent_message),
        )

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.delete_command_message", new=AsyncMock()) as delete_command,
        ):
            await bot.get_command("editres").callback(ctx, "2")

        ctx.send.assert_awaited_once()
        self.assertIn("Match 2", ctx.send.call_args.args[0])
        view = ctx.send.call_args.kwargs["view"]
        self.assertIsInstance(view, MatchScoreCorrectionView)
        self.assertEqual(view.match_number, 2)
        self.assertEqual(
            [option.label for option in view.children[0].options],
            [
                "Slot 01 · Alpha",
                "Slot 02 · Bravo",
                "Slot 03 · Charlie",
            ],
        )
        delete_command.assert_awaited_once_with(ctx)

    async def test_editres_team_selection_opens_rank_and_kills_form(self):
        scrim = self.make_scrim()
        view = MatchScoreCorrectionView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=2,
            slots=list(scrim.slots.values()),
        )
        selector = view.children[0]
        selector._values = ["2"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(
                send_modal=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
        ):
            await selector.callback(interaction)

        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, MatchScoreCorrectionModal)
        self.assertEqual(modal.slot_number, 2)
        self.assertEqual(modal.placement_input.max_length, 3)
        self.assertEqual(modal.kills_input.max_length, 5)

    async def test_editres_modal_updates_only_the_selected_team(self):
        scrim = self.make_scrim()
        previous_score = MatchScore(2, 2, 4, 5)
        scrim.match_scores[(2, 2)] = previous_score
        view = MatchScoreCorrectionView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=2,
            slots=list(scrim.slots.values()),
        )
        modal = MatchScoreCorrectionModal(
            view,
            scrim.slots[2],
            default_score=previous_score,
            baseline_score=previous_score,
        )
        modal.placement_input._value = "3"
        modal.kills_input._value = "6"
        review_message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(send_message=AsyncMock()),
            original_response=AsyncMock(return_value=review_message),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.repository.upsert_match_scores") as upsert,
        ):
            await modal.on_submit(interaction)

        upsert.assert_not_called()
        review = interaction.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(review, MatchScoreCorrectionReviewView)
        self.assertEqual(
            interaction.response.send_message.call_args.kwargs["embed"].title,
            "Review score correction · Match 2",
        )
        self.assertEqual(review.previous_score, previous_score)
        self.assertEqual(review.proposed_score, MatchScore(2, 2, 6, 3))

        confirm_button = next(
            button for button in review.children if button.label == "Confirm"
        )
        confirm_interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )
        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.repository.upsert_match_scores", return_value=1) as upsert,
        ):
            await confirm_button.callback(confirm_interaction)

        upsert.assert_called_once_with(
            scrim.id,
            scrim.guild_id,
            2,
            [MatchScore(2, 2, 6, 3)],
        )
        confirm_interaction.response.edit_message.assert_awaited_once()
        self.assertIn(
            "rank 3, 6 kills",
            confirm_interaction.response.edit_message.call_args.kwargs["content"],
        )

    async def test_edit_button_reopens_form_prefilled_with_proposed_values(self):
        scrim = self.make_scrim()
        previous_score = MatchScore(2, 2, 4, 5)
        scrim.match_scores[(2, 2)] = previous_score
        correction_view = MatchScoreCorrectionView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=2,
            slots=list(scrim.slots.values()),
        )
        review = MatchScoreCorrectionReviewView(
            correction_view=correction_view,
            slot=scrim.slots[2],
            previous_score=previous_score,
            proposed_score=MatchScore(2, 2, 6, 3),
        )
        edit_button = next(
            button for button in review.children if button.label == "Edit"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
        ):
            await edit_button.callback(interaction)

        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, MatchScoreCorrectionModal)
        self.assertEqual(modal.placement_input.default, "3")
        self.assertEqual(modal.kills_input.default, "6")
        self.assertEqual(modal.baseline_score, previous_score)
        self.assertIs(modal.source_review, review)

    async def test_choose_another_team_returns_to_slot_selector(self):
        scrim = self.make_scrim()
        scrim.match_scores.pop((2, 2), None)
        correction_view = MatchScoreCorrectionView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=2,
            slots=list(scrim.slots.values()),
        )
        review = MatchScoreCorrectionReviewView(
            correction_view=correction_view,
            slot=scrim.slots[2],
            previous_score=None,
            proposed_score=MatchScore(2, 2, 6, 3),
        )
        choose_button = next(
            button
            for button in review.children
            if button.label == "Choose another team"
        )
        message = SimpleNamespace()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            message=message,
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
        ):
            await choose_button.callback(interaction)

        new_view = interaction.response.edit_message.call_args.kwargs["view"]
        self.assertIsInstance(new_view, MatchScoreCorrectionView)
        self.assertIs(new_view.message, message)
        self.assertTrue(review.completed)

    async def test_cancel_discards_review_without_saving(self):
        scrim = self.make_scrim()
        scrim.match_scores.pop((2, 2), None)
        correction_view = MatchScoreCorrectionView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=2,
            slots=list(scrim.slots.values()),
        )
        review = MatchScoreCorrectionReviewView(
            correction_view=correction_view,
            slot=scrim.slots[2],
            previous_score=None,
            proposed_score=MatchScore(2, 2, 6, 3),
        )
        cancel_button = next(
            button for button in review.children if button.label == "Cancel"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )

        with patch("main.repository.upsert_match_scores") as upsert:
            await cancel_button.callback(interaction)

        upsert.assert_not_called()
        self.assertTrue(review.completed)
        self.assertIn(
            "No score was changed",
            interaction.response.edit_message.call_args.kwargs["content"],
        )

    async def test_stale_score_review_cannot_overwrite_a_newer_result(self):
        scrim = self.make_scrim()
        scrim.match_scores.pop((2, 2), None)
        correction_view = MatchScoreCorrectionView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=2,
            slots=list(scrim.slots.values()),
        )
        review = MatchScoreCorrectionReviewView(
            correction_view=correction_view,
            slot=scrim.slots[2],
            previous_score=None,
            proposed_score=MatchScore(2, 2, 6, 3),
        )
        scrim.match_scores[(2, 2)] = MatchScore(2, 2, 2, 5)
        confirm_button = next(
            button for button in review.children if button.label == "Confirm"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.repository.upsert_match_scores") as upsert,
        ):
            await confirm_button.callback(interaction)

        upsert.assert_not_called()
        self.assertIn(
            "changed after the recap",
            interaction.response.edit_message.call_args.kwargs["content"],
        )

    async def test_editres_modal_rejects_negative_kills_without_saving(self):
        scrim = self.make_scrim()
        view = MatchScoreCorrectionView(
            owner_id=7,
            guild_id=123,
            channel_id=456,
            scrim=scrim,
            match_number=2,
            slots=list(scrim.slots.values()),
        )
        modal = MatchScoreCorrectionModal(view, scrim.slots[2])
        modal.placement_input._value = "1"
        modal.kills_input._value = "-2"
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=123),
            channel_id=456,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
            patch("main.repository.upsert_match_scores") as upsert,
        ):
            await modal.on_submit(interaction)

        upsert.assert_not_called()
        self.assertIn(
            "whole numbers",
            interaction.response.send_message.call_args.args[0],
        )

    def test_all_score_aliases_are_registered(self):
        for match_number in range(1, MAX_MATCHES + 1):
            self.assertIsNotNone(bot.get_command(f"resg{match_number}"))
        self.assertIsNone(bot.get_command(f"resg{MAX_MATCHES + 1}"))
        self.assertIsNotNone(bot.get_command("res"))
        self.assertIsNotNone(bot.get_command("leaderboard"))
        self.assertIsNotNone(bot.get_command("setres"))

    async def test_res_command_sends_final_png_directly(self):
        scrim = self.make_scrim()
        scrim.max_matches = 2
        progress_message = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        ctx = SimpleNamespace(
            send=AsyncMock(side_effect=[progress_message, SimpleNamespace()])
        )
        command = bot.get_command("res")

        with (
            patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)),
            patch("main.build_leaderboard_image", return_value=io.BytesIO(b"png")),
        ):
            await command.callback(ctx)

        self.assertEqual(ctx.send.await_count, 2)
        self.assertEqual(
            ctx.send.await_args_list[0].args[0],
            "⏳ Working on the leaderboard…",
        )
        progress_message.delete.assert_awaited_once()
        self.assertEqual(
            ctx.send.await_args_list[1].kwargs["file"].filename,
            "leaderboard.png",
        )
        self.assertIn(
            "V2 Scrim Results",
            ctx.send.await_args_list[1].kwargs["content"],
        )

    async def test_res_command_sends_an_empty_leaderboard_table(self):
        scrim = self.make_scrim()
        scrim.slots = {}
        scrim.match_scores = {}
        progress_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(
            send=AsyncMock(return_value=progress_message),
            author=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=scrim.guild_id),
            channel=SimpleNamespace(id=55),
        )
        command = bot.get_command("res")

        with patch("main.require_staff_scrim", new=AsyncMock(return_value=scrim)):
            await command.callback(ctx)

        self.assertEqual(ctx.send.await_count, 1)
        progress_message.edit.assert_awaited_once()
        self.assertIn(
            "Results appear incomplete",
            progress_message.edit.call_args.kwargs["content"],
        )
        self.assertIsNotNone(progress_message.edit.call_args.kwargs.get("view"))

    def test_vertical_image_uses_configured_count_and_canvas_dimensions(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 16
        scrim.leaderboard_header_height = 180
        scrim.leaderboard_footer_height = 120
        with patch("main.repository.get_server_license_type", return_value="Gold"):
            buffer = build_leaderboard_image(
                scrim,
                calculate_leaderboard(scrim),
            )
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

        with patch("main.repository.get_server_license_type", return_value="Gold"):
            rows = calculate_leaderboard(scrim)
            image_buffer = build_leaderboard_image(scrim, rows)

        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[-1].slot_number, 24)
        with Image.open(image_buffer) as rendered:
            self.assertEqual(
                rendered.size,
                leaderboard_canvas_dimensions(24, "vertical", 180, 120),
            )

    def test_standard_sparse_results_keep_full_rank_capacity_in_both_orientations(self):
        original_text = ImageDraw.ImageDraw.text

        for orientation, expected_row_count in (
            ("vertical", 20),
            ("horizontal", 10),
        ):
            with self.subTest(orientation=orientation):
                scrim = self.make_scrim()
                scrim.leaderboard_team_count = 24
                scrim.leaderboard_orientation = orientation
                scrim.slots = {
                    number: Slot(
                        number,
                        status=STATUS_CONFIRMED,
                        team_name=f"Team {number:02d}",
                    )
                    for number in range(1, 8)
                }
                text_calls = []

                def record_text(drawer, xy, text, *args, **kwargs):
                    text_calls.append((str(text), xy))
                    return original_text(drawer, xy, text, *args, **kwargs)

                with (
                    patch(
                        "main.repository.get_server_license_type",
                        return_value="Standard",
                    ),
                    patch.object(
                        ImageDraw.ImageDraw,
                        "text",
                        new=record_text,
                    ),
                ):
                    rows = calculate_leaderboard(scrim)
                    image_buffer = build_leaderboard_image(scrim, rows)

                columns = 2 if orientation == "horizontal" else 1
                canvas_width, _ = leaderboard_canvas_dimensions(20, orientation)
                column_gap = 24 if columns == 2 else 0
                column_width = (
                    canvas_width
                    - 2 * LEADERBOARD_OUTER_MARGIN
                    - column_gap * (columns - 1)
                ) // columns
                rank_field_ranges = []
                for column in range(columns):
                    column_left = (
                        LEADERBOARD_OUTER_MARGIN
                        + column * (column_width + column_gap)
                    )
                    rank_field_ranges.append(
                        _leaderboard_field_ranges(
                            column_left,
                            column_width,
                            horizontal=columns == 2,
                        )[0]
                    )
                rank_calls = [
                    (int(text), position)
                    for text, position in text_calls
                    if text.isdigit()
                    and 1 <= int(text) <= 20
                    and any(
                        left <= position[0] < right
                        for left, right in rank_field_ranges
                    )
                ]
                self.assertEqual(len(rows), 7)
                self.assertEqual(
                    [rank for rank, _ in rank_calls],
                    list(range(1, 21)),
                )
                self.assertEqual(
                    len({position[1] for _, position in rank_calls}),
                    expected_row_count,
                )
                with Image.open(image_buffer) as rendered:
                    self.assertEqual(
                        rendered.size,
                        leaderboard_canvas_dimensions(20, orientation),
                    )

    def test_renderer_generates_only_date_ranks_team_and_scores(self):
        original_text = ImageDraw.ImageDraw.text

        for orientation in ("vertical", "horizontal"):
            for team_count in LEADERBOARD_TEAM_COUNTS:
                with self.subTest(
                    orientation=orientation,
                    team_count=team_count,
                ):
                    scrim = self.make_scrim()
                    scrim.leaderboard_team_count = team_count
                    scrim.leaderboard_orientation = orientation
                    rows = [
                        LeaderboardRow(
                            number,
                            f"Team {number:02d}",
                            0,
                            0,
                            0,
                            0,
                        )
                        for number in range(1, team_count)
                    ]
                    text_calls = []

                    def record_text(drawer, xy, text, *args, **kwargs):
                        text_calls.append(
                            (
                                str(text),
                                xy,
                                kwargs.get("anchor"),
                                kwargs.get("font"),
                            )
                        )
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
                        patch.object(
                            Image.Image,
                            "alpha_composite",
                            side_effect=AssertionError(
                                "Runtime renderer must not composite artwork."
                            ),
                        ),
                        patch(
                            "main.repository.get_server_license_type",
                            return_value="Gold",
                        ),
                    ):
                        build_leaderboard_image(scrim, rows)

                    rendered_text = [
                        text for text, _, _, _ in text_calls
                    ]
                    self.assertRegex(
                        rendered_text[0],
                        r"^\d{2}/\d{2}/\d{4}$",
                    )
                    columns = 2 if orientation == "horizontal" else 1
                    per_column_capacity = (
                        (team_count + 1) // 2
                        if columns == 2
                        else team_count
                    )
                    column_gap = 24 if columns == 2 else 0
                    canvas_width, _ = leaderboard_canvas_dimensions(
                        team_count,
                        orientation,
                    )
                    column_width = (
                        canvas_width
                        - 2 * LEADERBOARD_OUTER_MARGIN
                        - column_gap * (columns - 1)
                    ) // columns
                    rank_field_ranges = []
                    for column in range(columns):
                        column_left = (
                            LEADERBOARD_OUTER_MARGIN
                            + column * (column_width + column_gap)
                        )
                        rank_field_ranges.append(
                            _leaderboard_field_ranges(
                                column_left,
                                column_width,
                                horizontal=columns == 2,
                            )[0]
                        )
                    rank_calls = [
                        (int(text), xy, anchor, font)
                        for text, xy, anchor, font in text_calls
                        if text.isdigit()
                        and 1 <= int(text) <= team_count
                        and any(
                            left <= xy[0] < right
                            for left, right in rank_field_ranges
                        )
                    ]
                    ranks = [str(rank) for rank, _, _, _ in rank_calls]
                    self.assertEqual(
                        ranks,
                        [
                            str(rank)
                            for rank in range(1, team_count + 1)
                        ],
                    )
                    row_top = 282
                    row_height = (
                        LEADERBOARD_HORIZONTAL_ROW_HEIGHT
                        if columns == 2
                        else LEADERBOARD_VERTICAL_ROW_HEIGHT
                    )
                    for rank, xy, anchor, font in rank_calls:
                        row_index = (
                            (rank - 1) % per_column_capacity
                            if columns == 2
                            else rank - 1
                        )
                        expected_y = (
                            row_top
                            + row_index * row_height
                            + row_height // 2
                        )
                        expected_y -= LEADERBOARD_ROW_TEXT_VERTICAL_OFFSET
                        self.assertEqual(xy[1], expected_y)
                        column = (
                            0
                            if columns == 1 or rank <= per_column_capacity
                            else 1
                        )
                        rank_left, rank_right = rank_field_ranges[column]
                        expected_ink_center = (rank_left + rank_right) / 2
                        if columns == 2:
                            expected_ink_center += (
                                LEADERBOARD_HORIZONTAL_RANK_CELL_CENTER_OFFSETS[
                                    column
                                ]
                            )
                        bbox = ImageDraw.Draw(
                            Image.new("RGB", (1, 1))
                        ).textbbox(
                            (0, 0),
                            str(rank),
                            font=font,
                            anchor=anchor,
                        )
                        actual_ink_center = xy[0] + (
                            bbox[0] + bbox[2]
                        ) / 2
                        self.assertAlmostEqual(
                            actual_ink_center,
                            expected_ink_center,
                        )
                    self.assertIn("Team 01", rendered_text)
                    self.assertEqual(rendered_text.count("Team 01"), 1)
                    self.assertNotIn("TEAM", rendered_text)
                    self.assertNotIn("WIN", rendered_text)
                    self.assertNotIn("KILL", rendered_text)
                    self.assertNotIn("PLACE", rendered_text)
                    self.assertNotIn("TOTAL", rendered_text)

    def test_date_is_top_right_and_table_text_matches_reference_alignment(self):
        original_text = ImageDraw.ImageDraw.text
        for orientation in ("vertical", "horizontal"):
            with self.subTest(orientation=orientation):
                scrim = self.make_scrim()
                scrim.leaderboard_team_count = 16
                scrim.leaderboard_orientation = orientation
                text_calls = []

                def record_text(drawer, xy, text, *args, **kwargs):
                    text_calls.append(
                        (
                            str(text),
                            xy,
                            kwargs.get("anchor"),
                            kwargs.get("font"),
                        )
                    )
                    return original_text(drawer, xy, text, *args, **kwargs)

                with (
                    patch.object(
                        ImageDraw.ImageDraw,
                        "text",
                        new=record_text,
                    ),
                    patch(
                        "main.repository.get_server_license_type",
                        return_value="Gold",
                    ),
                ):
                    build_leaderboard_image(
                        scrim,
                        [LeaderboardRow(1, "CENTER ME", 13, 27, 34, 74)],
                    )

                columns = 2 if orientation == "horizontal" else 1
                column_gap = 24 if columns == 2 else 0
                canvas_width, _ = leaderboard_canvas_dimensions(
                    16,
                    orientation,
                )
                self.assertEqual(
                    next(call for call in text_calls if "/" in call[0])[1:3],
                    (
                        (canvas_width - LEADERBOARD_OUTER_MARGIN, LEADERBOARD_OUTER_MARGIN),
                        "rt",
                    ),
                )
                date_call = next(call for call in text_calls if "/" in call[0])
                self.assertEqual(date_call[3].size, 36)
                column_width = (
                    canvas_width
                    - 2 * LEADERBOARD_OUTER_MARGIN
                    - column_gap * (columns - 1)
                ) // columns
                left = LEADERBOARD_OUTER_MARGIN
                ranges = _leaderboard_field_ranges(
                    left,
                    column_width,
                    horizontal=columns == 2,
                )
                centers = [(start + end) / 2 for start, end in ranges]
                expected_text_y = 302 if orientation == "vertical" else 312

                for text, field_index, expected_anchor, expected_x in (
                    (
                        "CENTER ME",
                        1,
                        "lm",
                        ranges[1][0]
                        + (
                            LEADERBOARD_HORIZONTAL_TEAM_NAME_LEFT_PADDING
                            if columns == 2
                            else LEADERBOARD_TEAM_NAME_LEFT_PADDING
                        ),
                    ),
                    ("13", 2, "mm", centers[2]),
                    ("34", 3, "mm", centers[3]),
                    ("27", 4, "mm", centers[4]),
                    ("74", 5, "mm", centers[5]),
                ):
                    with self.subTest(text=text):
                        call = next(
                            call for call in text_calls if call[0] == text
                        )
                        self.assertEqual(call[1][0], expected_x)
                        self.assertEqual(call[1][1], expected_text_y)
                        self.assertEqual(call[2], expected_anchor)
                        self.assertEqual(
                            call[3].size,
                            32 if orientation == "horizontal" else 21,
                        )

                if orientation == "horizontal":
                    right_column_left = (
                        LEADERBOARD_OUTER_MARGIN + column_width + column_gap
                    )
                    right_ranges = _leaderboard_field_ranges(
                        right_column_left,
                        column_width,
                        horizontal=True,
                    )
                    rank_ranges = (ranges[0], right_ranges[0])
                    expected_rank_centers = (
                        centers[0]
                        + LEADERBOARD_HORIZONTAL_RANK_CELL_CENTER_OFFSETS[0],
                        (right_ranges[0][0] + right_ranges[0][1]) / 2
                        + LEADERBOARD_HORIZONTAL_RANK_CELL_CENTER_OFFSETS[1],
                    )
                    self.assertEqual(expected_rank_centers, (69, 1015))
                    for rank, column in ((1, 0), (9, 1)):
                        rank_left, rank_right = rank_ranges[column]
                        call = next(
                            call
                            for call in text_calls
                            if call[0] == str(rank)
                            and rank_left <= call[1][0] < rank_right
                        )
                        bbox = ImageDraw.Draw(
                            Image.new("RGB", (1, 1))
                        ).textbbox(
                            (0, 0),
                            str(rank),
                            font=call[3],
                            anchor=call[2],
                        )
                        self.assertAlmostEqual(
                            call[1][0] + (bbox[0] + bbox[2]) / 2,
                            expected_rank_centers[column],
                        )
                        self.assertEqual(call[1][1], 312)
                        self.assertEqual(call[2], "mm")
                    self.assertEqual(
                        (
                            expected_rank_centers[0],
                            ranges[1][0]
                            + LEADERBOARD_HORIZONTAL_TEAM_NAME_LEFT_PADDING,
                            centers[2],
                            centers[3],
                            centers[4],
                            centers[5],
                        ),
                        (69, 132, 534, 650.5, 765, 880),
                    )
                else:
                    rank_call = next(
                        call for call in text_calls if call[0] == "1"
                    )
                    rank_bbox = ImageDraw.Draw(
                        Image.new("RGB", (1, 1))
                    ).textbbox(
                        (0, 0),
                        rank_call[0],
                        font=rank_call[3],
                        anchor=rank_call[2],
                    )
                    self.assertAlmostEqual(
                        rank_call[1][0]
                        + (rank_bbox[0] + rank_bbox[2]) / 2,
                        centers[0],
                    )
                    self.assertEqual(rank_call[1][1], 302)
                    self.assertEqual(rank_call[2], "mm")

    def test_long_team_name_shrinks_and_stays_inside_its_field(self):
        fitted_text, font = _fit_leaderboard_cell_text(
            "LONG-TEAM-NAME-" * 20,
            max_width=120,
            max_height=52,
            max_size=21,
            min_size=8,
        )

        bbox = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
            (0, 0),
            fitted_text,
            font=font,
        )
        self.assertLess(font.size, 21)
        self.assertLessEqual(bbox[2] - bbox[0], 120)
        self.assertLessEqual(bbox[3] - bbox[1], 52)

    def test_renderer_preserves_background_away_from_all_text_fields(self):
        for orientation in ("vertical", "horizontal"):
            for team_count in LEADERBOARD_TEAM_COUNTS:
                with self.subTest(
                    orientation=orientation,
                    team_count=team_count,
                ):
                    scrim = self.make_scrim()
                    scrim.leaderboard_team_count = team_count
                    scrim.leaderboard_orientation = orientation
                    dimensions = leaderboard_canvas_dimensions(
                        team_count,
                        orientation,
                    )
                    with tempfile.TemporaryDirectory() as directory:
                        background_path = Path(directory) / "solid-background.png"
                        background = Image.new("RGB", dimensions, (25, 80, 140))
                        background.save(background_path)
                        expected = background.copy()
                        with patch(
                            "main.repository.get_server_license_type",
                            return_value="Gold",
                        ):
                            rendered_buffer = build_leaderboard_image(
                                scrim,
                                [LeaderboardRow(1, "Alpha", 1, 2, 16, 18)],
                                background_path=background_path,
                            )
                        with Image.open(rendered_buffer) as rendered:
                            self.assertEqual(
                                rendered.crop(
                                    (0, 0, LEADERBOARD_OUTER_MARGIN, dimensions[1])
                                ).tobytes(),
                                expected.crop(
                                    (0, 0, LEADERBOARD_OUTER_MARGIN, dimensions[1])
                                ).tobytes(),
                            )

    def test_leaderboard_text_color_changes_generated_text_only(self):
        scrim = self.make_scrim()
        scrim.leaderboard_team_count = 20
        rows = [LeaderboardRow(1, "Alpha", 1, 2, 16, 18)]
        with tempfile.TemporaryDirectory() as directory:
            background_dir = Path(directory)
            Image.new("RGB", (320, 180), (25, 80, 140)).save(
                background_dir / f"{scrim.id}-vertical-20.png"
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

    def test_orientations_use_approved_row_pitch_and_440px_fixed_space(self):
        team_counts = (16, 18, 20, 22, 24)
        vertical_heights = (1400, 1520, 1640, 1760, 1880)
        horizontal_heights = (1080, 1160, 1240, 1320, 1400)

        self.assertEqual(LEADERBOARD_VERTICAL_ROW_HEIGHT, 60)
        self.assertEqual(LEADERBOARD_HORIZONTAL_ROW_HEIGHT, 80)
        self.assertEqual(LEADERBOARD_SECTION_GAP, 10)
        fixed_space = (
            2 * LEADERBOARD_OUTER_MARGIN
            + DEFAULT_LEADERBOARD_HEADER_HEIGHT
            + LEADERBOARD_TABLE_HEADER_HEIGHT
            + DEFAULT_LEADERBOARD_FOOTER_HEIGHT
            + 2 * LEADERBOARD_SECTION_GAP
        )
        self.assertEqual(fixed_space, 440)
        self.assertEqual(
            tuple(
                leaderboard_canvas_dimensions(count, "vertical", 180, 120)[1]
                for count in team_counts
            ),
            vertical_heights,
        )
        self.assertEqual(
            tuple(
                leaderboard_canvas_dimensions(count, "horizontal", 180, 120)[1]
                for count in team_counts
            ),
            horizontal_heights,
        )

    def test_first_slot_row_positions_reflect_orientation_text_offsets(self):
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

        self.assertEqual(team_positions["vertical"]["Team 01"], 302)
        self.assertEqual(team_positions["horizontal"]["Team 01"], 312)
        self.assertEqual(team_positions["horizontal"]["Team 09"], 312)

    def test_generated_default_background_does_not_need_static_assets(self):
        missing_background = Path("/missing/leaderboard-background.png")
        with patch("main.LEADERBOARD_BACKGROUND", missing_background):
            background = _load_leaderboard_background_canvas(
                missing_background,
                (320, 180),
            )
        self.assertEqual(background.mode, "RGBA")
        self.assertEqual(background.size, (320, 180))
        self.assertEqual(background.getpixel((0, 0)), (13, 18, 26, 255))

    def test_clean_leaderboard_background_is_the_default(self):
        self.assertEqual(LEADERBOARD_BACKGROUND.name, "leaderboard-background.png")
        self.assertFalse(LEADERBOARD_BACKGROUND.is_file())

    def test_scrim_name_is_rendered_as_title_on_the_default_background(self):
        scrim = self.make_scrim()
        drawn_text = []
        title_calls = []
        original_text = ImageDraw.ImageDraw.text

        def capture_text(draw, xy, text, *args, **kwargs):
            drawn_text.append(str(text))
            if str(text) == scrim.name:
                title_calls.append(
                    (xy, kwargs.get("anchor"), kwargs.get("font"))
                )
            return original_text(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", new=capture_text):
            build_leaderboard_image(scrim, [])

        self.assertIn(scrim.name, drawn_text)
        self.assertEqual(len(title_calls), 1)
        position, anchor, title_font = title_calls[0]
        self.assertEqual(
            position,
            (
                LEADERBOARD_OUTER_MARGIN,
                LEADERBOARD_OUTER_MARGIN
                + DEFAULT_LEADERBOARD_HEADER_HEIGHT // 2,
            ),
        )
        self.assertEqual(anchor, "lm")
        self.assertEqual(title_font.size, LEADERBOARD_TITLE_MAX_FONT_SIZE)

    def test_custom_background_does_not_render_scrim_name(self):
        scrim = self.make_scrim()
        unnamed_scrim = SimpleNamespace(**vars(scrim))
        unnamed_scrim.name = ""

        with tempfile.TemporaryDirectory() as directory:
            custom_background = Path(directory) / "custom-background.png"
            Image.new("RGB", (320, 180), (18, 30, 44)).save(custom_background)
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
        scrim.leaderboard_accent_color = "#123456"
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


class ResultPlacementValidationTests(unittest.TestCase):
    def make_scrim(self):
        return SimpleNamespace(
            max_matches=4,
            slots={
                number: SimpleNamespace(number=number, status=STATUS_CONFIRMED)
                for number in (1, 2, 3)
            },
            match_scores={},
        )

    def test_parser_rejects_duplicate_and_out_of_range_placements(self):
        scrim = self.make_scrim()
        valid = parse_match_score_lines(
            "1 5\n3 4",
            match_number=1,
            scrim=scrim,
        )
        self.assertEqual(
            valid,
            [
                MatchScore(1, 1, 5, 1),
                MatchScore(1, 3, 4, 2),
            ],
        )
        self.assertEqual(
            parse_match_score_lines(
                "1 5 4",
                match_number=1,
                scrim=scrim,
            ),
            [MatchScore(1, 1, 5, 4)],
        )
        for raw_input in ("1 5 0", "1 5 2\n3 4 2"):
            with self.subTest(raw_input=raw_input):
                with self.assertRaisesRegex(ValueError, "placement"):
                    parse_match_score_lines(
                        raw_input,
                        match_number=1,
                        scrim=scrim,
                    )

    def test_invalid_replacement_scores_do_not_change_saved_results(self):
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
                slot_end=2,
            )
            with repository.transaction():
                for number, name in ((1, "Alpha"), (2, "Bravo")):
                    scrim.slots[number] = Slot(
                        number,
                        status=STATUS_CONFIRMED,
                        team_name=name,
                        tag=name[0],
                        manager_id=100 + number,
                        captain_1_id=100 + number,
                    )

            existing = MatchScore(1, 1, 4, 2)
            repository.replace_match_scores(
                scrim.id,
                123,
                1,
                [existing],
            )
            before = repository.get_match_scores(scrim.id)
            invalid_sets = (
                [MatchScore(1, 99, 5, 1)],
                [MatchScore(1, 1, 4, 1), MatchScore(1, 2, 3, 1)],
                [MatchScore(1, 1, 4, 0)],
            )
            for scores in invalid_sets:
                with self.subTest(scores=scores):
                    with self.assertRaises(ValueError):
                        repository.replace_match_scores(
                            scrim.id,
                            123,
                            1,
                            scores,
                        )
                    self.assertEqual(
                        repository.get_match_scores(scrim.id),
                        before,
                    )


class StaffReviewHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_authorized_staff_member_can_review_submitted_scores(self):
        scrim = LeaderboardV2Tests().make_scrim()
        view = MatchScoreSubmissionReviewView(
            owner_id=7,
            guild_id=scrim.guild_id,
            channel_id=55,
            scrim=scrim,
            match_number=1,
            scores=[MatchScore(1, 1, 4, 1)],
            raw_input="1 4",
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            guild=SimpleNamespace(id=scrim.guild_id),
            channel_id=55,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with (
            patch("main.repository.get", return_value=scrim),
            patch("main.is_active", return_value=True),
            patch("main.member_is_staff", return_value=True),
        ):
            self.assertIs(await view.authorized_scrim(interaction), scrim)

        interaction.response.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()