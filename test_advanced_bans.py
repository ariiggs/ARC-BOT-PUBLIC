from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from arc_bot.commands.bans import (
    BanLaunchView,
    BanService,
    BanTeamSelectView,
    LiveBanDurationModal,
    ManualBanFormModal,
    ManualBanCaptainSelectView,
    StaleBanSelection,
    ban_expiry_for_scrim,
    parse_ban_duration_reply,
    parse_manual_ban_arguments,
    parse_unban_arguments,
    parse_weeks_days,
)
from arc_bot.commands.help import HELP_CATEGORIES, HELP_COPY_TEXT
from arc_bot.storage.ban_storage import (
    BanStorageError,
    BanStore,
    LEGACY_BAN_REASON,
    registration_match_key,
)
from scrim_state import (
    RegistrationRequest,
    Slot,
    STATUS_AVAILABLE,
    STATUS_CONFIRMED,
    _read_registration_requests,
)


def make_ban(
    ban_id: str,
    team_name: str,
    tag: str,
    *,
    expires_at: datetime | None = None,
    is_permanent: bool = False,
    scope: str = "guild",
    scrim_id: str = "scrim-1",
    captain_id: int = 123,
    reason: str = "Testing policy violation.",
) -> dict:
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return {
        "ban_id": ban_id,
        "team_name": team_name,
        "tag": tag,
        "reason": reason,
        "captain_id": captain_id,
        "created_by": 456,
        "created_at": now.isoformat(),
        "expires_at": (
            None
            if is_permanent
            else (expires_at or now + timedelta(days=30)).isoformat()
        ),
        "is_permanent": is_permanent,
        "scrim_id": scrim_id,
        "scope": scope,
    }


class BanStorageTests(unittest.TestCase):
    def test_isolated_bans_match_only_their_scrim(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BanStore(Path(temporary) / "bans.json")
            store.add_ban(
                55,
                make_ban(
                    "a" * 16,
                    "Alpha",
                    "AAA",
                    scope="scrim",
                    scrim_id="scrim-a",
                ),
            )

            self.assertIsNone(
                store.match(55, "Alpha", "AAA", scrim_id="scrim-b")
            )
            match = store.match(55, "Alpha", "AAA", scrim_id="scrim-a")
            self.assertIsNotNone(match)
            self.assertTrue(match.is_exact)

    def test_same_team_can_have_bans_in_distinct_scopes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BanStore(Path(temporary) / "bans.json")
            store.add_ban(55, make_ban("a" * 16, "Alpha", "AAA"))
            store.add_ban(
                55,
                make_ban(
                    "b" * 16,
                    "Alpha",
                    "AAA",
                    scope="scrim",
                    scrim_id="scrim-a",
                ),
            )

            self.assertEqual(len(store.bans(55)), 2)
            self.assertTrue(store.match(55, "Alpha", "AAA", scrim_id="scrim-b").is_exact)

    def test_exact_match_precedes_multiple_partial_reasons(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BanStore(Path(temporary) / "bans.json")
            store.add_ban(55, make_ban("a" * 16, "Alpha Squad", "AAA"))
            store.add_ban(55, make_ban("b" * 16, "Bravo", "BBB"))

            now = datetime(2026, 9, 15, tzinfo=timezone.utc)
            exact = store.match(55, " alpha   squad ", "aaa", now=now)
            partial = store.match(55, "Alpha Squad", "BBB", now=now)

            self.assertIsNotNone(exact)
            self.assertTrue(exact.is_exact)
            self.assertEqual(partial.kind, "partial")
            self.assertEqual(len(partial.reasons), 2)
            self.assertTrue(any("Name matches" in reason for reason in partial.reasons))
            self.assertTrue(any("Tag matches" in reason for reason in partial.reasons))
            self.assertEqual(registration_match_key("  A  B "), "a b")

    def test_expired_records_do_not_match_and_survive_reload_until_sweep(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bans.json"
            store = BanStore(path)
            store.add_ban(
                55,
                make_ban(
                    "a" * 16,
                    "Alpha",
                    "AAA",
                    expires_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
                ),
            )
            now = datetime(2026, 9, 3, tzinfo=timezone.utc)

            self.assertIsNone(store.match(55, "Alpha", "AAA", now=now))
            self.assertEqual(len(BanStore(path).bans(55)), 1)
            expired = store.expire(now)
            self.assertEqual(len(expired[55]), 1)
            self.assertEqual(BanStore(path).bans(55), [])

    def test_permanent_bans_never_expire_and_are_saved_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bans.json"
            store = BanStore(path)
            store.add_ban(
                55,
                make_ban(
                    "c" * 16,
                    "Cheater",
                    "CHEAT",
                    is_permanent=True,
                ),
            )
            now = datetime(2040, 1, 1, tzinfo=timezone.utc)

            record = store.bans(55)[0]
            self.assertTrue(record["is_permanent"])
            self.assertIsNone(record["expires_at"])
            self.assertTrue(store.match(55, "Cheater", "CHEAT", now=now).is_exact)
            self.assertEqual(store.expire(now), {})
            self.assertTrue(BanStore(path).bans(55)[0]["is_permanent"])

    def test_v1_storage_migrates_existing_bans_as_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bans.json"
            original = BanStore(path)
            original.add_ban(55, make_ban("a" * 16, "Alpha", "AAA"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["version"] = 1
            for key in (
                "punitive_role_id",
                "punitive_role_grants",
                "scrim_scopes",
            ):
                payload["guilds"]["55"].pop(key)
            for record in payload["guilds"]["55"]["bans"]:
                record.pop("is_permanent")
                record.pop("scope")
                record.pop("reason")
            path.write_text(json.dumps(payload), encoding="utf-8")

            migrated = BanStore(path)

            self.assertFalse(migrated.bans(55)[0]["is_permanent"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 4)
            self.assertFalse(saved["guilds"]["55"]["bans"][0]["is_permanent"])
            self.assertEqual(saved["guilds"]["55"]["bans"][0]["scope"], "guild")
            self.assertEqual(
                saved["guilds"]["55"]["bans"][0]["reason"], LEGACY_BAN_REASON
            )

    def test_v2_storage_migrates_legacy_bans_as_server_wide(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bans.json"
            original = BanStore(path)
            original.add_ban(55, make_ban("a" * 16, "Alpha", "AAA"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["version"] = 2
            for key in (
                "punitive_role_id",
                "punitive_role_grants",
                "scrim_scopes",
            ):
                payload["guilds"]["55"].pop(key)
            payload["guilds"]["55"]["bans"][0].pop("scope")
            payload["guilds"]["55"]["bans"][0].pop("reason")
            path.write_text(json.dumps(payload), encoding="utf-8")

            migrated = BanStore(path)

            self.assertEqual(migrated.bans(55)[0]["scope"], "guild")
            self.assertEqual(migrated.bans(55)[0]["reason"], LEGACY_BAN_REASON)
            self.assertIsNone(migrated.config(55)["punitive_role_id"])
            self.assertEqual(migrated.config(55)["punitive_role_grants"], {})

    def test_v3_storage_migrates_bans_with_an_explicit_legacy_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bans.json"
            original = BanStore(path)
            original.add_ban(55, make_ban("a" * 16, "Alpha", "AAA"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["version"] = 3
            payload["guilds"]["55"]["bans"][0].pop("reason")
            path.write_text(json.dumps(payload), encoding="utf-8")

            migrated = BanStore(path)

            self.assertEqual(migrated.bans(55)[0]["reason"], LEGACY_BAN_REASON)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 4)

    def test_board_pins_permanent_then_sorts_longest_temporary_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                SimpleNamespace(),
                SimpleNamespace(),
                Path(temporary) / "bans.json",
            )
            now = datetime.now(timezone.utc)
            service.store.add_ban(
                55,
                make_ban(
                    "c" * 16,
                    "Permanent",
                    "PERM",
                    is_permanent=True,
                ),
            )
            service.store.add_ban(
                55,
                make_ban(
                    "b" * 16,
                    "Short",
                    "SHORT",
                    expires_at=now + timedelta(days=3),
                ),
            )
            service.store.add_ban(
                55,
                make_ban(
                    "a" * 16,
                    "Long",
                    "LONG",
                    expires_at=now + timedelta(days=30),
                ),
            )

            embed, _ = service._board_embed(55, 0)

        self.assertEqual(
            [field.name.split(" · ")[0] for field in embed.fields],
            ["Permanent", "Long", "Short"],
        )
        self.assertIn("🚨 **PERMANENT**", embed.fields[0].value)
        self.assertNotIn("<t:", embed.fields[0].value)
        self.assertIn("<t:", embed.fields[1].value)
        self.assertIn("Reason: Testing policy violation.", embed.fields[0].value)

    def test_invalid_file_is_not_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bans.json"
            path.write_text("{broken json", encoding="utf-8")

            with self.assertRaises(BanStorageError):
                BanStore(path)

            self.assertEqual(path.read_text(encoding="utf-8"), "{broken json")

    def test_reset_settings_restores_defaults_without_deleting_bans(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BanStore(Path(temporary) / "bans.json")
            existing_ban = make_ban("a" * 16, "Alpha", "AAA")
            store.add_ban(55, existing_ban)
            store.set_config(55, "bans_channel_id", 700)
            store.set_config(55, "board_message_id", 701)
            store.set_config(55, "board_page", 3)
            store.set_config(55, "punitive_role_id", 702)
            store.set_config(55, "banned_emoji", "🚫")
            store.set_scope_for_scrim(55, "scrim-1", "scrim")

            store.reset_settings(55, "scrim-1")

            config = store.config(55)
            self.assertIsNone(config["bans_channel_id"])
            self.assertIsNone(config["board_message_id"])
            self.assertEqual(config["board_page"], 0)
            self.assertIsNone(config["punitive_role_id"])
            self.assertEqual(config["banned_emoji"], "🔨")
            self.assertEqual(store.scope_for_scrim(55, "scrim-1"), "guild")
            self.assertEqual(store.bans(55), [existing_ban])

    def test_ban_service_reset_aborts_if_board_cannot_be_retired(self):
        async def check_reset():
            with tempfile.TemporaryDirectory() as temporary:
                service = BanService(
                    SimpleNamespace(),
                    SimpleNamespace(),
                    Path(temporary) / "bans.json",
                )
                service.store.add_ban(
                    55,
                    make_ban(
                        "a" * 16,
                        "Alpha",
                        "AAA",
                        captain_id=123,
                    ),
                )
                service.store.set_config(55, "bans_channel_id", 700)
                service.store.set_config(55, "board_message_id", 701)
                service.store.set_config(55, "punitive_role_id", 702)
                service.store.set_config(55, "banned_emoji", "🚫")
                service.store.set_scope_for_scrim(55, "scrim-1", "scrim")
                service.store.set_punitive_role_grants(55, 123, [702])
                service.retire_board = AsyncMock(return_value=False)
                service._sync_punitive_role_for_captain = AsyncMock(
                    return_value=True
                )

                reset, failures = await service.reset_settings_for_scrim(
                    55, "scrim-1"
                )
                self.assertFalse(reset)
                self.assertEqual(failures, [])
                self.assertEqual(service.store.config(55)["bans_channel_id"], 700)
                self.assertEqual(
                    service.store.scope_for_scrim(55, "scrim-1"), "scrim"
                )
                service._sync_punitive_role_for_captain.assert_not_awaited()

                service.retire_board.return_value = True
                reset, failures = await service.reset_settings_for_scrim(
                    55, "scrim-1"
                )
                self.assertTrue(reset)
                self.assertEqual(failures, [])
                service._sync_punitive_role_for_captain.assert_awaited_once_with(
                    55, 123
                )
                self.assertIsNone(service.store.config(55)["bans_channel_id"])
                self.assertEqual(
                    service.store.scope_for_scrim(55, "scrim-1"), "guild"
                )
                self.assertEqual(
                    len(service.store.bans(55)),
                    1,
                    "Resetting configuration must retain active ban records.",
                )

        asyncio.run(check_reset())

    def test_ban_request_metadata_round_trips_and_old_requests_still_load(self):
        slot = Slot(number=3, status=STATUS_AVAILABLE, assignment_id=0)
        request = RegistrationRequest(
            request_id="a" * 16,
            slot_number=3,
            team_name="Alpha",
            tag="AAA",
            manager_id=123,
            assignment_id=0,
            registration_message_id=789,
            ban_match_reason="Name matches banned team Alpha (tag OLD).",
        )
        with_reason = _read_registration_requests(
            [request.__dict__],
            {3: slot},
            3,
            3,
        )["a" * 16]
        legacy = request.__dict__.copy()
        legacy.pop("ban_match_reason")
        old_request = _read_registration_requests(
            [legacy],
            {3: slot},
            3,
            3,
        )["a" * 16]

        self.assertEqual(with_reason.ban_match_reason, request.ban_match_reason)
        self.assertIsNone(old_request.ban_match_reason)


class BanInputTests(unittest.TestCase):
    def test_duration_validation(self):
        self.assertEqual(parse_weeks_days("52", "7"), (52, 7))
        for values in (("0", "0"), ("53", "0"), ("0", "8"), ("-1", "2"), ("x", "1")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_weeks_days(*values)

    def test_duration_reply_accepts_temporary_and_permanent_answers(self):
        self.assertEqual(parse_ban_duration_reply("2 3"), (False, 2, 3))
        self.assertEqual(parse_ban_duration_reply(" PERM "), (True, 0, 0))
        self.assertEqual(parse_ban_duration_reply("Permanent"), (True, 0, 0))
        for value in ("0 0", "53 0", "perm 2", "later"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_ban_duration_reply(value)

    def test_scrim_timezone_controls_calendar_expiry(self):
        scrim = SimpleNamespace(timezone="Europe/Paris")
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)

        expiry = ban_expiry_for_scrim(scrim, 0, 1, now=now)

        # Paris moves to summer time on March 29; one local calendar day is
        # therefore 23 elapsed UTC hours.
        self.assertEqual(expiry, datetime(2026, 3, 29, 11, 0, tzinfo=timezone.utc))

    def test_manual_and_unban_syntax(self):
        self.assertEqual(
            parse_manual_ban_arguments('"Alpha Squad" "A A"'),
            ("Alpha Squad", "A A"),
        )
        self.assertEqual(
            parse_manual_ban_arguments("Alpha Squad / A A"),
            ("Alpha Squad", "A A"),
        )
        self.assertEqual(
            parse_manual_ban_arguments('"Alpha Squad" / "A A"'),
            ("Alpha Squad", "A A"),
        )
        self.assertEqual(
            parse_unban_arguments('"Alpha Squad" "A A"'),
            ("Alpha Squad", "A A"),
        )
        self.assertEqual(
            parse_unban_arguments("Alpha Squad / A A"),
            ("Alpha Squad", "A A"),
        )
        with self.assertRaises(ValueError):
            parse_manual_ban_arguments('Alpha "AAA" <@123> 0 0')
        with self.assertRaises(ValueError):
            parse_unban_arguments("Alpha Squad /")

    def test_ban_help_uses_slash_between_team_and_tag(self):
        help_text = HELP_CATEGORIES["Ban management"]["text"]
        self.assertIn("`!ban Team Name / TAG`", help_text)
        self.assertIn("`!unban Team Name / TAG`", help_text)
        self.assertIn("`!ban Team Name / TAG`", HELP_COPY_TEXT)
        self.assertIn("`!unban Team Name / TAG`", HELP_COPY_TEXT)

    def test_prefix_launcher_opens_manual_modal_and_deletes_its_message(self):
        owner = SimpleNamespace()
        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                SimpleNamespace(),
                owner,
                Path(temporary) / "bans.json",
            )
            launcher_message = SimpleNamespace(delete=AsyncMock())
            response = SimpleNamespace(send_modal=AsyncMock())
            interaction = SimpleNamespace(
                response=response,
                message=launcher_message,
            )
            view = BanLaunchView(
                service,
                owner_id=456,
                guild_id=55,
                scrim_id="scrim-1",
            )

            asyncio.run(view.open_manual_form(interaction))

        modal = response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, ManualBanFormModal)
        self.assertEqual(len(modal.children), 4)
        self.assertTrue(modal.reason_input.required)
        launcher_message.delete.assert_awaited_once()

    def test_live_selection_opens_modal_with_captured_slot_identity(self):
        scrim = SimpleNamespace(
            id="scrim-1",
            guild_id=55,
            name="Open scrim",
            is_open=True,
            staff_channel_id=700,
            state_lock=asyncio.Lock(),
            slots={
                3: Slot(
                    number=3,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha Squad",
                    tag="AAA",
                    manager_id=123,
                    captain_1_id=123,
                    assignment_id=8,
                )
            },
        )

        class Repository:
            def get(self, scrim_id):
                return scrim if scrim_id == scrim.id else None

        owner = SimpleNamespace(
            repository=Repository(),
            is_active=lambda selected: True,
            STATUS_AVAILABLE=STATUS_AVAILABLE,
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                SimpleNamespace(),
                owner,
                Path(temporary) / "bans.json",
            )
            menu_message = SimpleNamespace(delete=AsyncMock())
            response = SimpleNamespace(send_modal=AsyncMock())
            interaction = SimpleNamespace(
                channel_id=700,
                message=menu_message,
                response=response,
            )
            view = BanTeamSelectView(service, 456, scrim, message=menu_message)
            view.children[0]._values = ["3:8"]

            asyncio.run(view.select_team(interaction))

        modal = response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, LiveBanDurationModal)
        self.assertEqual(modal.team_name, "Alpha Squad")
        self.assertEqual(modal.tag, "AAA")
        self.assertEqual(modal.captain_id, 123)
        self.assertEqual(
            modal.expected_selection,
            ("scrim-1", 8, 3, "Alpha Squad", "AAA"),
        )
        self.assertEqual(len(modal.children), 2)
        menu_message.delete.assert_awaited_once()

    def test_manual_ban_modal_hands_off_to_private_captain_picker(self):
        scrim = SimpleNamespace(
            id="scrim-1",
            guild_id=55,
            is_open=True,
            staff_channel_id=700,
        )

        class Repository:
            def get(self, scrim_id):
                return scrim if scrim_id == scrim.id else None

            def list(self, guild_id):
                return []

        owner = SimpleNamespace(
            repository=Repository(),
            is_active=lambda selected: True,
            member_has_global_staff_role=lambda user, guild_id: True,
        )
        staff_user = SimpleNamespace(id=456, bot=False)
        member = SimpleNamespace(
            id=123,
            guild=SimpleNamespace(id=55),
            bot=False,
        )
        guild = SimpleNamespace(
            id=55,
            get_member=lambda member_id: member if member_id == member.id else None,
        )

        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                SimpleNamespace(),
                owner,
                Path(temporary) / "bans.json",
            )
            service.apply_form_ban = AsyncMock()
            modal = ManualBanFormModal(
                service,
                owner_id=456,
                guild_id=55,
                scrim_id=scrim.id,
            )
            modal.team_name_input._value = "Alpha Squad"
            modal.tag_input._value = "AAA"
            modal.duration_input._value = "perm"
            modal.reason_input._value = "Repeated rule violations"
            ephemeral_message = SimpleNamespace()
            response = SimpleNamespace(send_message=AsyncMock())
            modal_interaction = SimpleNamespace(
                user=staff_user,
                guild=guild,
                guild_id=55,
                channel_id=700,
                response=response,
                original_response=AsyncMock(return_value=ephemeral_message),
            )

            asyncio.run(modal.on_submit(modal_interaction))

            self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])
            view = response.send_message.await_args.kwargs["view"]
            self.assertIsInstance(view, ManualBanCaptainSelectView)
            self.assertIs(view.message, ephemeral_message)
            selector = view.children[0]
            self.assertIsInstance(selector, discord.ui.UserSelect)
            selector._values = [member]
            captain_interaction = SimpleNamespace(
                user=staff_user,
                guild=guild,
                channel_id=700,
                response=SimpleNamespace(
                    send_message=AsyncMock(),
                    edit_message=AsyncMock(),
                ),
            )

            asyncio.run(view.select_captain(captain_interaction))

        service.apply_form_ban.assert_awaited_once()
        self.assertEqual(
            service.apply_form_ban.await_args.kwargs["duration"],
            "perm",
        )
        self.assertEqual(
            service.apply_form_ban.await_args.kwargs["reason"],
            "Repeated rule violations",
        )
        self.assertEqual(service.apply_form_ban.await_args.kwargs["captain_id"], 123)

    def test_live_ban_modal_submits_reason_and_immutable_slot_snapshot(self):
        expected = ("scrim-1", 8, 3, "Alpha Squad", "AAA")
        service = SimpleNamespace(apply_form_ban=AsyncMock())
        modal = LiveBanDurationModal(
            service,
            owner_id=456,
            guild_id=55,
            scrim_id="scrim-1",
            team_name="Alpha Squad",
            tag="AAA",
            captain_id=123,
            expected_selection=expected,
        )
        modal.duration_input._value = "2 3"
        modal.reason_input._value = "Repeated rule violations"
        interaction = SimpleNamespace()

        asyncio.run(modal.on_submit(interaction))

        service.apply_form_ban.assert_awaited_once()
        self.assertEqual(
            service.apply_form_ban.await_args.kwargs["expected_selection"],
            expected,
        )
        self.assertEqual(
            service.apply_form_ban.await_args.kwargs["reason"],
            "Repeated rule violations",
        )


class BanPurgeTests(unittest.TestCase):
    def test_purges_exact_open_slots_and_pending_requests_only(self):
        open_scrim = SimpleNamespace(
            id="open",
            guild_id=55,
            name="Open",
            is_open=True,
            staff_channel_id=700,
            state_lock=asyncio.Lock(),
            slots={
                3: Slot(
                    number=3,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha",
                    tag="AAA",
                    manager_id=123,
                    captain_1_id=123,
                    assignment_id=1,
                ),
                4: Slot(
                    number=4,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha",
                    tag="OTHER",
                    manager_id=124,
                    captain_1_id=124,
                    assignment_id=2,
                ),
                5: Slot(
                    number=5,
                    status=STATUS_CONFIRMED,
                    team_name="Bravo",
                    tag="AAA",
                    manager_id=125,
                    captain_1_id=125,
                    assignment_id=3,
                ),
            },
            pending_registrations={
                "a" * 16: RegistrationRequest(
                    request_id="a" * 16,
                    slot_number=6,
                    team_name="Alpha",
                    tag="AAA",
                    manager_id=126,
                    assignment_id=0,
                    registration_message_id=900,
                ),
                "b" * 16: RegistrationRequest(
                    request_id="b" * 16,
                    slot_number=7,
                    team_name="Alpha",
                    tag="OTHER",
                    manager_id=127,
                    assignment_id=0,
                    registration_message_id=901,
                ),
            },
        )
        closed_scrim = SimpleNamespace(
            id="closed",
            guild_id=55,
            name="Closed",
            is_open=False,
            staff_channel_id=701,
            state_lock=asyncio.Lock(),
            slots={
                3: Slot(
                    number=3,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha",
                    tag="AAA",
                    manager_id=128,
                    captain_1_id=128,
                    assignment_id=1,
                )
            },
            pending_registrations={},
        )
        other_open_scrim = SimpleNamespace(
            id="other-open",
            guild_id=55,
            name="Other open",
            is_open=True,
            staff_channel_id=702,
            state_lock=asyncio.Lock(),
            slots={
                3: Slot(
                    number=3,
                    status=STATUS_CONFIRMED,
                    team_name="Alpha",
                    tag="AAA",
                    manager_id=129,
                    captain_1_id=129,
                    assignment_id=1,
                )
            },
            pending_registrations={},
        )

        class Repository:
            scrims = {
                "open": open_scrim,
                "other-open": other_open_scrim,
                "closed": closed_scrim,
            }

            def list(self, guild_id):
                return [scrim for scrim in self.scrims.values() if scrim.guild_id == guild_id]

            def get(self, scrim_id):
                return self.scrims.get(scrim_id)

            def transaction(self):
                return nullcontext()

        reactions = []

        async def reaction(scrim, request, *, approved, banned=False):
            reactions.append((scrim.id, request.request_id, approved, banned))
            return True

        owner = SimpleNamespace(
            repository=Repository(),
            is_active=lambda scrim: True,
            STATUS_AVAILABLE=STATUS_AVAILABLE,
            revoke_manager_access_if_unused=AsyncMock(return_value=True),
            update_registration_reaction=reaction,
            refresh_public_slots=AsyncMock(return_value=True),
            refresh_registration_queue=AsyncMock(return_value=True),
            send_scrim_log=AsyncMock(),
        )

        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                SimpleNamespace(),
                owner,
                Path(temporary) / "bans.json",
            )
            failures = asyncio.run(
                service._purge_active_scrims(
                    55, "Alpha", "AAA", scope="scrim", scrim_id="open"
                )
            )

        self.assertEqual(failures, [])
        self.assertEqual(open_scrim.slots[3].status, STATUS_AVAILABLE)
        self.assertEqual(open_scrim.slots[4].status, STATUS_CONFIRMED)
        self.assertEqual(open_scrim.slots[5].status, STATUS_CONFIRMED)
        self.assertEqual(set(open_scrim.pending_registrations), {"b" * 16})
        self.assertEqual(closed_scrim.slots[3].status, STATUS_CONFIRMED)
        self.assertEqual(
            other_open_scrim.slots[3].status, STATUS_CONFIRMED
        )
        self.assertEqual(reactions, [("open", "a" * 16, False, True)])

    def test_interactive_stale_slot_or_identity_never_creates_a_ban(self):
        scrim = SimpleNamespace(
            id="selected",
            guild_id=55,
            is_open=True,
            state_lock=asyncio.Lock(),
            slots={
                3: Slot(
                    number=3,
                    status=STATUS_CONFIRMED,
                    team_name="Bravo",
                    tag="BBB",
                    manager_id=123,
                    captain_1_id=123,
                    assignment_id=1,
                )
            },
        )

        class Repository:
            def get(self, scrim_id):
                return scrim if scrim_id == scrim.id else None

        owner = SimpleNamespace(
            repository=Repository(),
            is_active=lambda selected: True,
            STATUS_AVAILABLE=STATUS_AVAILABLE,
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                SimpleNamespace(),
                owner,
                Path(temporary) / "bans.json",
            )

            async def submit(expected_selection):
                await service.add_ban(
                    55,
                    team_name="Alpha",
                    tag="AAA",
                    reason="Confirmed cheating",
                    captain_id=123,
                    created_by=456,
                    scrim=scrim,
                    expires_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                    expected_selection=expected_selection,
                )

            for selection in (
                ("selected", 0, 3, "Alpha", "AAA"),
                ("selected", 1, 3, "Alpha", "AAA"),
            ):
                with self.subTest(selection=selection):
                    with self.assertRaises(StaleBanSelection):
                        asyncio.run(submit(selection))
            self.assertEqual(service.store.bans(55), [])

    def test_service_requires_and_persists_a_ban_reason(self):
        scrim = SimpleNamespace(
            id="selected",
            guild_id=55,
            is_open=True,
        )

        class Repository:
            def get(self, scrim_id):
                return scrim if scrim_id == scrim.id else None

        owner = SimpleNamespace(
            repository=Repository(),
            is_active=lambda selected: True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                SimpleNamespace(),
                owner,
                Path(temporary) / "bans.json",
            )
            service._sync_punitive_role_for_captain = AsyncMock(return_value=True)
            service._purge_active_scrims = AsyncMock(return_value=[])
            service.refresh_board = AsyncMock(return_value=True)

            async def add(reason):
                return await service.add_ban(
                    55,
                    team_name="Alpha",
                    tag="AAA",
                    reason=reason,
                    captain_id=123,
                    created_by=456,
                    scrim=scrim,
                    expires_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                )

            with self.assertRaises(ValueError):
                asyncio.run(add("  "))
            self.assertEqual(service.store.bans(55), [])

            previous, failures, board_ok = asyncio.run(
                add("Confirmed cheating")
            )

            saved = service.store.bans(55)[0]

        self.assertIsNone(previous)
        self.assertEqual(failures, [])
        self.assertTrue(board_ok)
        self.assertEqual(saved["reason"], "Confirmed cheating")


class BanPunitiveRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_stays_until_last_temporary_or_permanent_ban_ends(self):
        role = SimpleNamespace(
            id=700,
            position=1,
            managed=False,
            is_default=lambda: False,
        )

        class Member:
            def __init__(self, member_id):
                self.id = member_id
                self.roles = []

            async def add_roles(self, selected_role, **_kwargs):
                self.roles.append(selected_role)

            async def remove_roles(self, selected_role, **_kwargs):
                self.roles = [
                    current for current in self.roles if current.id != selected_role.id
                ]

        members = {123: Member(123), 124: Member(124)}
        guild = SimpleNamespace(
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=10),
            ),
            get_member=lambda member_id: members.get(member_id),
            get_role=lambda role_id: role if role_id == role.id else None,
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        now = datetime.now(timezone.utc)

        with tempfile.TemporaryDirectory() as temporary:
            service = BanService(
                bot,
                SimpleNamespace(),
                Path(temporary) / "bans.json",
            )
            service.store.add_ban(
                55,
                make_ban(
                    "a" * 16,
                    "Alpha",
                    "AAA",
                    captain_id=123,
                    expires_at=now + timedelta(hours=1),
                ),
            )
            service.store.add_ban(
                55,
                make_ban(
                    "b" * 16,
                    "Bravo",
                    "BBB",
                    captain_id=123,
                    expires_at=now + timedelta(hours=2),
                ),
            )
            service.store.add_ban(
                55,
                make_ban(
                    "c" * 16,
                    "Charlie",
                    "CCC",
                    captain_id=123,
                    expires_at=now + timedelta(hours=3),
                ),
            )
            service.store.add_ban(
                55,
                make_ban(
                    "d" * 16,
                    "Permanent",
                    "PERM",
                    captain_id=124,
                    is_permanent=True,
                ),
            )

            failures = await service.configure_punitive_role(55, role.id)
            self.assertEqual(failures, [])
            self.assertEqual([item.id for item in members[123].roles], [role.id])
            self.assertEqual([item.id for item in members[124].roles], [role.id])

            await service.expire_bans(now + timedelta(hours=1, minutes=30))
            self.assertEqual([item.id for item in members[123].roles], [role.id])
            self.assertEqual(len(service.store.active_bans_for_captain(55, 123)), 2)

            removed, _board_ok, role_failures = await service.remove_ban(
                55, "Bravo", "BBB"
            )
            self.assertEqual(len(removed), 1)
            self.assertEqual(role_failures, [])
            self.assertEqual([item.id for item in members[123].roles], [role.id])

            await service.expire_bans(now + timedelta(hours=4))
            self.assertEqual(members[123].roles, [])
            self.assertEqual([item.id for item in members[124].roles], [role.id])

            removed, _board_ok, role_failures = await service.remove_ban(
                55, "Permanent", "PERM"
            )
            self.assertEqual(len(removed), 1)
            self.assertEqual(role_failures, [])
            self.assertEqual(members[124].roles, [])


if __name__ == "__main__":
    unittest.main()