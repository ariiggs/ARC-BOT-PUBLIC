"""Persistent, guild-scoped scrim state. No Discord API calls in this module."""
from __future__ import annotations

import asyncio
import copy
import math
import re
import secrets
import string
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from slot_storage import SlotStateStore, SlotStorageError

DEFAULT_SLOT_START = 3
DEFAULT_SLOT_END = 25
MIN_SLOT_NUMBER = 1
MAX_SLOT_NUMBER = 99
MAX_SLOT_COUNT = 25
SLOT_NUMBERS = range(DEFAULT_SLOT_START, DEFAULT_SLOT_END + 1)
STATUS_AVAILABLE = "Disponible"
STATUS_RESERVED = "En attente du manager"
STATUS_PENDING = "En attente"
STATUS_CONFIRMED = "Confirmé"
SLOT_STATUSES = (
    STATUS_AVAILABLE,
    STATUS_RESERVED,
    STATUS_PENDING,
    STATUS_CONFIRMED,
)
MAX_SCRIMS_PER_GUILD = 25
DEFAULT_IDPW_TIMEZONE = "UTC+01:00"
DEFAULT_MATCH_MAPS = ("Erangel", "Miramar", "Sanhok", "Vikendi")
MAX_MATCHES = 25
MAX_MAP_POOL = 25
PASSWORD_TYPES = ("fixed", "dynamic")
DEFAULT_EMOJI_AVAILABLE = "⚪"
DEFAULT_EMOJI_RESERVED = "🔵"
DEFAULT_EMOJI_PENDING = "🟠"
DEFAULT_EMOJI_CONFIRMED = "🟢"
DEFAULT_LICENSE_TYPE = "Standard"
LICENSE_TYPES = ("Standard", "Gold")
DEFAULT_KILL_POINTS_VALUE = 1
DEFAULT_PLACEMENT_POINTS_STRING = "10 6 5 4 3 2 1"
LEADERBOARD_LAYOUTS = ("1_col", "2_col")
DEFAULT_LEADERBOARD_BACKGROUND = "reference"
LEADERBOARD_BACKGROUNDS = ("reference", "legacy")
LEADERBOARD_TEAM_COUNTS = (16, 18, 20, 22, 24)
LEADERBOARD_ORIENTATIONS = ("vertical", "horizontal")
DEFAULT_LEADERBOARD_TEAM_COUNT = 24
DEFAULT_LEADERBOARD_ORIENTATION = "vertical"
DEFAULT_LEADERBOARD_HEADER_HEIGHT = 180
DEFAULT_LEADERBOARD_FOOTER_HEIGHT = 120
LEADERBOARD_HEADER_HEIGHTS = (DEFAULT_LEADERBOARD_HEADER_HEIGHT,)
LEADERBOARD_FOOTER_HEIGHTS = (DEFAULT_LEADERBOARD_FOOTER_HEIGHT,)
DEFAULT_LEADERBOARD_ACCENT_COLOR = "#FFFFFF"


def leaderboard_profile_key(orientation: str, team_count: int) -> str:
    if (
        orientation not in LEADERBOARD_ORIENTATIONS
        or type(team_count) is not int
        or team_count not in LEADERBOARD_TEAM_COUNTS
    ):
        raise ValueError("Invalid leaderboard profile.")
    return f"{orientation}:{team_count}"


def normalize_leaderboard_accent_colors(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Invalid leaderboard text color profiles.")
    valid_keys = {
        leaderboard_profile_key(orientation, team_count)
        for orientation in LEADERBOARD_ORIENTATIONS
        for team_count in LEADERBOARD_TEAM_COUNTS
    }
    normalized: dict[str, str] = {}
    for key, color_hex in value.items():
        if (
            not isinstance(key, str)
            or key not in valid_keys
            or not isinstance(color_hex, str)
            or re.fullmatch(r"#[0-9A-Fa-f]{6}", color_hex) is None
        ):
            raise ValueError("Invalid leaderboard text color profile.")
        normalized[key] = color_hex.upper()
    return normalized


EMOJI_FIELDS = (
    "emoji_available",
    "emoji_reserved",
    "emoji_pending",
    "emoji_confirmed",
)
DEFAULT_SCRIM_EMOJIS = {
    "emoji_available": DEFAULT_EMOJI_AVAILABLE,
    "emoji_reserved": DEFAULT_EMOJI_RESERVED,
    "emoji_pending": DEFAULT_EMOJI_PENDING,
    "emoji_confirmed": DEFAULT_EMOJI_CONFIRMED,
}
OPERATIONAL_MESSAGE_KEYS = (
    "open_registration",
    "close_registration",
    "open_slots",
    "close_slots",
    "publish_results",
)
OPERATIONAL_MESSAGE_CONTEXTS = ("registration", "slots")
DEFAULT_OPERATIONAL_MESSAGE_REFS: dict[str, dict[str, int]] = {}
DEFAULT_OPERATIONAL_MESSAGES = {
    "open_registration": "✅ Registrations are now open for **{scrim}**.",
    "close_registration": "🔒 Registrations are now closed for **{scrim}**.",
    "open_slots": "✅ Slot confirmations are now open for **{scrim}**.",
    "close_slots": "🔒 Slot confirmations are now closed for **{scrim}**.",
    "publish_results": (
        "🏆 **{scrim} Results** · {team_count} teams · {match_count} matches\n"
        "🥇 {top1_team} — {top1_points} pts\n"
        "🥈 {top2_team} — {top2_points} pts\n"
        "🥉 {top3_team} — {top3_points} pts"
    ),
}


def normalize_operational_messages(value: object) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Operational messages must be an object.")
    if any(key not in OPERATIONAL_MESSAGE_KEYS for key in value):
        raise ValueError("Operational messages contain an unknown key.")
    normalized: dict[str, str] = {}
    for key in OPERATIONAL_MESSAGE_KEYS:
        message = value.get(key, DEFAULT_OPERATIONAL_MESSAGES[key])
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 2000
            or "\x00" in message
        ):
            raise ValueError("Operational messages must be 1–2000 characters.")
        try:
            fields = {
                field_name
                for _, field_name, _, _ in string.Formatter().parse(message)
                if field_name is not None
            }
        except ValueError as error:
            raise ValueError(
                "Operational messages contain invalid placeholders."
            ) from error
        if key == "publish_results":
            allowed_fields = {"scrim", "team_count", "match_count"}
            allowed_fields.update(
                f"top{rank}_{stat}"
                for rank in range(1, 4)
                for stat in ("team", "points", "kills", "wins")
            )
        else:
            allowed_fields = {"scrim", "channel"}
        if not fields.issubset(allowed_fields):
            if key == "publish_results":
                raise ValueError(
                    "Results messages contain an unsupported placeholder."
                )
            raise ValueError(
                "Operational messages may only use {scrim} and {channel}."
            )
        normalized[key] = message.strip()
    return normalized


def normalize_operational_message_refs(
    value: object,
) -> dict[str, dict[str, int]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Operational message references must be an object.")
    if any(key not in OPERATIONAL_MESSAGE_CONTEXTS for key in value):
        raise ValueError("Operational message references contain an unknown key.")
    normalized: dict[str, dict[str, int]] = {}
    for context, reference in value.items():
        if (
            not isinstance(reference, dict)
            or set(reference) != {"message_id", "channel_id"}
            or type(reference["message_id"]) is not int
            or reference["message_id"] <= 0
            or type(reference["channel_id"]) is not int
            or reference["channel_id"] <= 0
        ):
            raise ValueError("Operational message references are invalid.")
        normalized[context] = {
            "message_id": reference["message_id"],
            "channel_id": reference["channel_id"],
        }
    return normalized


def normalize_map_pool(
    maps: list[str] | tuple[str, ...],
) -> list[str]:
    if not isinstance(maps, (list, tuple)):
        raise ValueError("Map pool must be a list of map names.")
    normalized = [str(game_map).strip() for game_map in maps]
    if (
        len(normalized) > MAX_MAP_POOL
        or any(
            not game_map
            or len(game_map) > 50
            or any(character in game_map for character in ("\r", "\n"))
            for game_map in normalized
        )
        or len({game_map.casefold() for game_map in normalized}) != len(normalized)
    ):
        raise ValueError(
            f"Choose between 0 and {MAX_MAP_POOL} unique non-empty map names."
        )
    return normalized


def normalize_match_configuration(
    max_matches: int,
    maps: list[str] | tuple[str, ...],
    match_maps: list[str] | tuple[str, ...] | None = None,
) -> tuple[int, list[str], list[str]]:
    if type(max_matches) is not int or not 1 <= max_matches <= MAX_MATCHES:
        raise ValueError(f"Number of matches must be between 1 and {MAX_MATCHES}.")
    map_pool = normalize_map_pool(maps)
    if match_maps is None:
        # Old snapshots stored one map per match. Treat that rotation as both
        # the reusable pool and the initial per-match assignment.
        assignments = list(map_pool) if len(map_pool) == max_matches else []
    else:
        assignments = [str(game_map).strip() for game_map in match_maps]
    if not assignments:
        return max_matches, map_pool, []
    if len(assignments) != max_matches:
        raise ValueError("Choose one map for every configured match.")
    if any(game_map not in map_pool for game_map in assignments):
        raise ValueError("Every match map must come from the configured map pool.")
    return max_matches, map_pool, assignments
IDPW_TIMEZONE_CHOICES = tuple(
    [(f"UTC-{hour:02d}:00", f"UTC-{hour:02d}:00") for hour in range(12, 0, -1)]
    + [("UTC", "UTC")]
    + [(f"UTC+{hour:02d}:00", f"UTC+{hour:02d}:00") for hour in range(1, 13)]
)
_UNSET = object()


@dataclass(frozen=True)
class SlotSnapshot:
    number: int
    status: str
    team_name: str
    tag: str
    manager_id: int | None
    assignment_id: int
    captain_1_id: int | None = None
    captain_2_id: int | None = None


@dataclass(frozen=True)
class RegistrationRequest:
    request_id: str
    slot_number: int
    team_name: str
    tag: str
    manager_id: int
    assignment_id: int
    registration_message_id: int | None = None


@dataclass(frozen=True)
class MatchScore:
    match_number: int
    slot_number: int
    kills: int
    placement: int


@dataclass
class Slot:
    number: int
    status: str = STATUS_AVAILABLE
    team_name: str = ""
    tag: str = ""
    manager_id: int | None = None
    assignment_id: int = 0
    captain_1_id: int | None = None
    captain_2_id: int | None = None

    def clear(self) -> None:
        self.assignment_id += 1
        self.status = STATUS_AVAILABLE
        self.team_name = ""
        self.tag = ""
        self.manager_id = None
        self.captain_1_id = None
        self.captain_2_id = None

    def snapshot(self) -> SlotSnapshot:
        return SlotSnapshot(**asdict(self))


def empty_slots(
    start: int = DEFAULT_SLOT_START,
    end: int = DEFAULT_SLOT_END,
) -> dict[int, Slot]:
    return {number: Slot(number) for number in range(start, end + 1)}


def validate_slot_range(start: int, end: int) -> tuple[int, int]:
    if (
        type(start) is not int
        or type(end) is not int
        or not MIN_SLOT_NUMBER <= start <= MAX_SLOT_NUMBER
        or not MIN_SLOT_NUMBER <= end <= MAX_SLOT_NUMBER
        or start > end
        or end - start + 1 > MAX_SLOT_COUNT
    ):
        if (
            type(start) is int
            and type(end) is int
            and start <= end
            and end - start + 1 > MAX_SLOT_COUNT
        ):
            raise ValueError(f"A scrim can have at most {MAX_SLOT_COUNT} slots.")
        raise ValueError(
            f"Slot range must be between {MIN_SLOT_NUMBER:02d} and "
            f"{MAX_SLOT_NUMBER:02d}, with the first slot no greater than the last."
        )
    return start, end


@dataclass
class Scrim:
    id: str
    guild_id: int
    name: str
    public_channel_id: int
    staff_channel_id: int
    staff_role_id: int | None = None
    pending_role_id: int | None = None
    confirmed_role_id: int | None = None
    cap_channel_id: int | None = None
    logs_channel_id: int | None = None
    history_channel_id: int | None = None
    registration_channel_id: int | None = None
    registration_role_id: int | None = None
    registration_auto_accept: bool = False
    emoji_available: str = DEFAULT_EMOJI_AVAILABLE
    emoji_reserved: str = DEFAULT_EMOJI_RESERVED
    emoji_pending: str = DEFAULT_EMOJI_PENDING
    emoji_confirmed: str = DEFAULT_EMOJI_CONFIRMED
    public_message_id: int | None = None
    staff_message_id: int | None = None
    is_open: bool = True
    registration_open: bool = True
    operational_messages: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_OPERATIONAL_MESSAGES)
    )
    operational_message_refs: dict[str, dict[str, int]] = field(
        default_factory=lambda: dict(DEFAULT_OPERATIONAL_MESSAGE_REFS)
    )
    slot_start: int = DEFAULT_SLOT_START
    slot_end: int = DEFAULT_SLOT_END
    slots: dict[int, Slot] = field(default_factory=empty_slots)
    timezone: str = DEFAULT_IDPW_TIMEZONE
    maps: list[str] = field(default_factory=lambda: list(DEFAULT_MATCH_MAPS))
    max_matches: int = len(DEFAULT_MATCH_MAPS)
    match_maps: list[str] = field(default_factory=lambda: list(DEFAULT_MATCH_MAPS))
    pw_type: str = "fixed"
    fixed_pw: str = ""
    current_match_counter: int = 1
    kill_points_value: int = DEFAULT_KILL_POINTS_VALUE
    placement_points_string: str = DEFAULT_PLACEMENT_POINTS_STRING
    leaderboard_layout: str = "1_col"
    leaderboard_background: str = DEFAULT_LEADERBOARD_BACKGROUND
    leaderboard_accent_color: str = DEFAULT_LEADERBOARD_ACCENT_COLOR
    leaderboard_accent_colors: dict[str, str] = field(default_factory=dict)
    leaderboard_team_count: int = DEFAULT_LEADERBOARD_TEAM_COUNT
    leaderboard_orientation: str = DEFAULT_LEADERBOARD_ORIENTATION
    leaderboard_header_height: int = DEFAULT_LEADERBOARD_HEADER_HEIGHT
    leaderboard_footer_height: int = DEFAULT_LEADERBOARD_FOOTER_HEIGHT
    match_scores: dict[tuple[int, int], MatchScore] = field(default_factory=dict)
    pending_registrations: dict[str, RegistrationRequest] = field(
        default_factory=dict
    )
    # These references are process-local; only durable values are serialized.
    runtime_message: object | None = field(default=None, repr=False)
    runtime_staff_message: object | None = field(default=None, repr=False)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    board_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    deleted: bool = False

    def payload(self) -> dict:
        return {
            "id": self.id, "guild_id": self.guild_id, "name": self.name,
            "public_channel_id": self.public_channel_id,
            "staff_channel_id": self.staff_channel_id,
            "staff_role_id": self.staff_role_id,
            "pending_role_id": self.pending_role_id,
            "confirmed_role_id": self.confirmed_role_id,
            "cap_channel_id": self.cap_channel_id,
            "logs_channel_id": self.logs_channel_id,
            "history_channel_id": self.history_channel_id,
            "registration_channel_id": self.registration_channel_id,
            "registration_role_id": self.registration_role_id,
            "registration_auto_accept": self.registration_auto_accept,
            "emoji_available": self.emoji_available,
            "emoji_reserved": self.emoji_reserved,
            "emoji_pending": self.emoji_pending,
            "emoji_confirmed": self.emoji_confirmed,
            "public_message_id": self.public_message_id,
            "staff_message_id": self.staff_message_id,
            "is_open": self.is_open,
            "registration_open": self.registration_open,
            "operational_messages": dict(self.operational_messages),
            "operational_message_refs": {
                context: dict(reference)
                for context, reference in self.operational_message_refs.items()
            },
            "slot_start": self.slot_start,
            "slot_end": self.slot_end,
            "slots": [asdict(slot) for slot in self.slots.values()],
            "timezone": self.timezone,
            "maps": list(self.maps),
            "max_matches": self.max_matches,
            "match_maps": list(self.match_maps),
            "pw_type": self.pw_type,
            "fixed_pw": self.fixed_pw,
            "current_match_counter": self.current_match_counter,
            "kill_points_value": self.kill_points_value,
            "placement_points_string": self.placement_points_string,
            "leaderboard_layout": self.leaderboard_layout,
            "leaderboard_background": self.leaderboard_background,
            "leaderboard_accent_color": self.leaderboard_accent_color,
            "leaderboard_accent_colors": dict(self.leaderboard_accent_colors),
            "leaderboard_team_count": self.leaderboard_team_count,
            "leaderboard_orientation": self.leaderboard_orientation,
            "leaderboard_header_height": self.leaderboard_header_height,
            "leaderboard_footer_height": self.leaderboard_footer_height,
            "match_scores": [
                asdict(score)
                for _, score in sorted(self.match_scores.items())
            ],
            "pending_registrations": [
                asdict(request) for request in self.pending_registrations.values()
            ],
        }


@dataclass
class ServerConfig:
    guild_id: int
    head_staff_role_id: int | None
    staff_role_id: int | None
    logs_channel_id: int | None
    license_type: str = DEFAULT_LICENSE_TYPE

    def payload(self) -> dict:
        return {
            "guild_id": self.guild_id,
            "head_staff_role_id": self.head_staff_role_id,
            "staff_role_id": self.staff_role_id,
            "logs_channel_id": self.logs_channel_id,
            "license_type": self.license_type,
        }


@dataclass(frozen=True)
class IdPwConfig:
    scrim_id: str
    target_channel_id: int
    fixed_password: str
    timezone_name: str = DEFAULT_IDPW_TIMEZONE
    announcement_message_id: int | None = None

    def payload(self) -> dict:
        return {
            "scrim_id": self.scrim_id,
            "target_channel_id": self.target_channel_id,
            "fixed_password": self.fixed_password,
            "timezone_name": self.timezone_name,
            "announcement_message_id": self.announcement_message_id,
        }


def _positive_id(value) -> bool:
    return type(value) is int and value > 0


def timezone_for_name(value: str):
    if value == "UTC":
        return timezone.utc
    match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", value)
    if match:
        hours = int(match.group(2))
        minutes = int(match.group(3))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ValueError("Invalid UTC offset.")
        offset = timedelta(hours=hours, minutes=minutes)
        return timezone(offset if match.group(1) == "+" else -offset)
    return ZoneInfo(value)


def valid_timezone_name(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 100:
        return False
    try:
        timezone_for_name(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def normalize_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Scrim name must be text.")
    name = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(name) <= 80 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("Use a scrim name of 1–80 characters without control characters.")
    return name


def _read_slots(
    entries,
    slot_start: int = DEFAULT_SLOT_START,
    slot_end: int = DEFAULT_SLOT_END,
) -> dict[int, Slot]:
    validate_slot_range(slot_start, slot_end)
    if not isinstance(entries, list):
        raise ValueError("Invalid slot list.")
    result = []
    for entry in entries:
        values = dict(entry)
        # Older snapshots could persist a released assignment as a fifth
        # status. It is now loaded directly as an available slot.
        if values.get("status") == "Annulé":
            values.update(
                status=STATUS_AVAILABLE,
                team_name="",
                tag="",
                manager_id=None,
                captain_1_id=None,
                captain_2_id=None,
            )
        if values.get("captain_1_id") is None and values.get("manager_id") is not None:
            values["captain_1_id"] = values["manager_id"]
        if values.get("manager_id") is None and values.get("captain_1_id") is not None:
            values["manager_id"] = values["captain_1_id"]
        values.setdefault("captain_1_id", None)
        values.setdefault("captain_2_id", None)
        result.append(Slot(**values))
    expected_numbers = list(range(slot_start, slot_end + 1))
    if [slot.number for slot in result] != expected_numbers:
        raise ValueError(
            f"Slots must run from {slot_start:02d} to {slot_end:02d} in order."
        )
    for slot in result:
        if (
            type(slot.number) is not int
            or slot.status not in SLOT_STATUSES
            or type(slot.assignment_id) is not int or slot.assignment_id < 0
            or not isinstance(slot.team_name, str)
            or not isinstance(slot.tag, str)
            or (slot.manager_id is not None and not _positive_id(slot.manager_id))
            or (
                slot.captain_1_id is not None
                and not _positive_id(slot.captain_1_id)
            )
            or (
                slot.captain_2_id is not None
                and not _positive_id(slot.captain_2_id)
            )
            or slot.manager_id != slot.captain_1_id
            or (
                slot.captain_2_id is not None
                and slot.captain_2_id == slot.captain_1_id
            )
            or (slot.status != STATUS_AVAILABLE and (
                not slot.team_name
                or not slot.tag
                or slot.captain_1_id is None
            ))
            or (slot.status == STATUS_AVAILABLE and (
                slot.team_name
                or slot.tag
                or slot.manager_id is not None
                or slot.captain_1_id is not None
                or slot.captain_2_id is not None
            ))
        ):
            raise ValueError("Invalid slot assignment.")
    return {slot.number: slot for slot in result}


def _read_registration_requests(
    entries,
    slots: dict[int, Slot],
    slot_start: int,
    slot_end: int,
) -> dict[str, RegistrationRequest]:
    if not isinstance(entries, list):
        raise ValueError("Invalid registration request list.")
    result: dict[str, RegistrationRequest] = {}
    requested_slots: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "request_id",
            "slot_number",
            "team_name",
            "tag",
            "manager_id",
            "assignment_id",
            "registration_message_id",
        }:
            raise ValueError("Invalid registration request fields.")
        request = RegistrationRequest(**entry)
        if (
            not isinstance(request.request_id, str)
            or not re.fullmatch(r"[a-f0-9]{16}", request.request_id)
            or request.request_id in result
            or type(request.slot_number) is not int
            or not slot_start <= request.slot_number <= slot_end
            or request.slot_number in requested_slots
            or not isinstance(request.team_name, str)
            or not request.team_name.strip()
            or len(request.team_name) > 100
            or any(character in request.team_name for character in ("\r", "\n"))
            or not isinstance(request.tag, str)
            or not request.tag.strip()
            or len(request.tag) > 32
            or any(character in request.tag for character in ("\r", "\n"))
            or not _positive_id(request.manager_id)
            or type(request.assignment_id) is not int
            or request.assignment_id < 0
            or (
                request.registration_message_id is not None
                and not _positive_id(request.registration_message_id)
            )
        ):
            raise ValueError("Invalid registration request.")
        slot = slots[request.slot_number]
        if (
            slot.status != STATUS_AVAILABLE
            or slot.assignment_id != request.assignment_id
        ):
            raise ValueError("Registration request does not match its slot.")
        requested_slots.add(request.slot_number)
        result[request.request_id] = request
    return result


def parse_placement_points(value: str) -> list[int]:
    if not isinstance(value, str):
        raise ValueError("Placement points must be text.")
    parts = value.split()
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError("Placement points must be space-separated whole numbers.")
    points = [int(part) for part in parts]
    if any(point < 0 for point in points):
        raise ValueError("Placement points cannot be negative.")
    return points


def _read_match_scores(entries, slots, max_matches: int) -> dict[tuple[int, int], MatchScore]:
    if not isinstance(entries, list):
        raise ValueError("Invalid match score list.")
    result: dict[tuple[int, int], MatchScore] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "match_number", "slot_number", "kills", "placement"
        }:
            raise ValueError("Invalid match score fields.")
        score = MatchScore(**entry)
        if (
            type(score.match_number) is not int
            or not 1 <= score.match_number <= max_matches
            or type(score.slot_number) is not int
            or score.slot_number not in slots
            or type(score.kills) is not int
            or score.kills < 0
            or type(score.placement) is not int
            or score.placement < 1
        ):
            raise ValueError("Invalid match score.")
        key = (score.match_number, score.slot_number)
        if key in result:
            raise ValueError("Duplicate match score.")
        result[key] = score
    return result


def _legacy_payload(payload) -> dict:
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Invalid legacy snapshot.")
    _read_slots(payload["slots"])
    board = payload["public_board"]
    if board is not None and (
        not isinstance(board, dict)
        or set(board) != {"channel_id", "message_id"}
        or not all(_positive_id(value) for value in board.values())
    ):
        raise ValueError("Invalid legacy board reference.")
    return copy.deepcopy(payload)


class ScrimRepository:
    """Atomic whole-repository snapshots, retaining old data until safely linked.

    transaction() must contain no awaits. Callers use per-scrim locks around
    asynchronous work and revalidate get(id) is the captured object.
    """

    def __init__(self, store: SlotStateStore):
        self.store = store
        self.scrims: dict[str, Scrim] = {}
        self.legacy: dict | None = None
        self.server_configs: dict[int, ServerConfig] = {}
        self.idpw_configs: dict[str, IdPwConfig] = {}
        self.authorized_guild_ids: set[int] = set()
        self.authorized_guild_expires_at: dict[int, datetime] = {}
        self.authorized_guild_duration_days: dict[int, int] = {}
        self.authorized_guild_license_types: dict[int, str] = {}
        self.authorized_admin_ids: set[int] = set()

    def get(self, scrim_id: str) -> Scrim | None:
        scrim = self.scrims.get(scrim_id)
        return scrim if scrim is not None and not scrim.deleted else None

    def list(self, guild_id: int) -> list[Scrim]:
        return sorted(
            (s for s in self.scrims.values() if s.guild_id == guild_id and not s.deleted),
            key=lambda s: (s.name.casefold(), s.id),
        )

    def is_guild_authorized(self, guild_id: int) -> bool:
        if guild_id not in self.authorized_guild_ids:
            return False
        expires_at = self.authorized_guild_expires_at.get(guild_id)
        return expires_at is None or expires_at > datetime.now(timezone.utc)

    def get_server_license_type(self, guild_id: int) -> str:
        """Return the active authorization tier, falling back to legacy config."""
        authorized_license = self.authorized_guild_license_types.get(guild_id)
        if authorized_license is not None:
            return (
                authorized_license
                if self.is_guild_authorized(guild_id)
                else DEFAULT_LICENSE_TYPE
            )
        config = self.server_configs.get(guild_id)
        return config.license_type if config is not None else DEFAULT_LICENSE_TYPE

    def get_guild_subscription(
        self, guild_id: int
    ) -> tuple[bool, datetime | None, int | None]:
        """Return durable subscription state, including expired authorizations."""
        if not _positive_id(guild_id):
            raise ValueError("The guild ID must be a positive integer.")
        if guild_id not in self.authorized_guild_ids:
            return False, None, None
        return (
            True,
            self.authorized_guild_expires_at.get(guild_id),
            self.authorized_guild_duration_days.get(guild_id, 0),
        )

    def list_authorized_guild_ids(self) -> list[int]:
        return [guild_id for guild_id, _, _ in self.list_authorizations()]

    def list_authorizations(
        self,
        *,
        include_expired: bool = False,
        license_type: str | None = None,
    ) -> list[tuple[int, datetime | None, int]]:
        if license_type is not None and license_type not in LICENSE_TYPES:
            raise ValueError("Choose a Standard or Gold license.")
        now = datetime.now(timezone.utc)
        authorizations = []
        for guild_id in sorted(self.authorized_guild_ids):
            expires_at = self.authorized_guild_expires_at.get(guild_id)
            stored_license_type = self.authorized_guild_license_types.get(guild_id)
            if stored_license_type is None:
                config = self.server_configs.get(guild_id)
                stored_license_type = (
                    config.license_type if config is not None else DEFAULT_LICENSE_TYPE
                )
            if license_type is not None and stored_license_type != license_type:
                continue
            if (
                not include_expired
                and expires_at is not None
                and expires_at <= now
            ):
                continue
            authorizations.append(
                (
                    guild_id,
                    expires_at,
                    self.authorized_guild_duration_days.get(guild_id, 0),
                )
            )
        return authorizations

    def authorize_guild(
        self,
        guild_id: int,
        days: int | None = None,
        *,
        license_type: str | None = None,
    ) -> bool:
        if not _positive_id(guild_id):
            raise ValueError("The guild ID must be a positive integer.")
        if days is not None and (type(days) is not int or days < 0):
            raise ValueError("The authorization duration must be zero or more days.")
        if license_type is None:
            license_type = self.get_server_license_type(guild_id)
        if license_type not in LICENSE_TYPES:
            raise ValueError("Choose a Standard or Gold license.")
        duration_days = 0 if days is None else days
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=duration_days)
            if duration_days > 0
            else None
        )
        was_authorized = self.is_guild_authorized(guild_id)
        with self.transaction():
            self.authorized_guild_ids.add(guild_id)
            self.authorized_guild_duration_days[guild_id] = duration_days
            self.authorized_guild_license_types[guild_id] = license_type
            if expires_at is None:
                self.authorized_guild_expires_at.pop(guild_id, None)
            else:
                self.authorized_guild_expires_at[guild_id] = expires_at
            current_config = self.server_configs.get(guild_id)
            if current_config is not None:
                self.server_configs[guild_id] = ServerConfig(
                    guild_id,
                    current_config.head_staff_role_id,
                    current_config.staff_role_id,
                    current_config.logs_channel_id,
                    license_type,
                )
        return not was_authorized

    def revoke_guild(self, guild_id: int) -> bool:
        if not _positive_id(guild_id):
            raise ValueError("The guild ID must be a positive integer.")
        if guild_id not in self.authorized_guild_ids:
            return False
        with self.transaction():
            self.authorized_guild_ids.remove(guild_id)
            self.authorized_guild_expires_at.pop(guild_id, None)
            self.authorized_guild_duration_days.pop(guild_id, None)
            self.authorized_guild_license_types.pop(guild_id, None)
            current_config = self.server_configs.get(guild_id)
            if current_config is not None:
                self.server_configs[guild_id] = ServerConfig(
                    guild_id,
                    current_config.head_staff_role_id,
                    current_config.staff_role_id,
                    current_config.logs_channel_id,
                    DEFAULT_LICENSE_TYPE,
                )
        return True

    def is_admin_authorized(self, user_id: int) -> bool:
        return user_id in self.authorized_admin_ids

    def list_authorized_admin_ids(self) -> list[int]:
        return sorted(self.authorized_admin_ids)

    def authorize_admin(self, user_id: int) -> bool:
        if not _positive_id(user_id):
            raise ValueError("The user ID must be a positive integer.")
        if user_id in self.authorized_admin_ids:
            return False
        with self.transaction():
            self.authorized_admin_ids.add(user_id)
        return True

    def revoke_admin(self, user_id: int) -> bool:
        if not _positive_id(user_id):
            raise ValueError("The user ID must be a positive integer.")
        if user_id not in self.authorized_admin_ids:
            return False
        with self.transaction():
            self.authorized_admin_ids.remove(user_id)
        return True

    def payload(self) -> dict:
        return {
            "version": 31,
            "scrims": [s.payload() for s in self.scrims.values()],
            "server_configs": [
                config.payload() for config in self.server_configs.values()
            ],
            "idpw_configs": [
                config.payload() for config in self.idpw_configs.values()
            ],
            "authorized_guild_ids": sorted(self.authorized_guild_ids),
            "authorized_guild_expires_at": {
                str(guild_id): expires_at.isoformat()
                for guild_id, expires_at in sorted(
                    self.authorized_guild_expires_at.items()
                )
            },
            "authorized_guild_duration_days": {
                str(guild_id): duration_days
                for guild_id, duration_days in sorted(
                    self.authorized_guild_duration_days.items()
                )
            },
            "authorized_guild_license_types": {
                str(guild_id): license_type
                for guild_id, license_type in sorted(
                    self.authorized_guild_license_types.items()
                )
            },
            "authorized_admin_ids": sorted(self.authorized_admin_ids),
            "legacy": copy.deepcopy(self.legacy),
        }

    @staticmethod
    def _decode(
        payload: dict,
    ) -> tuple[
        dict[str, Scrim],
        dict | None,
        dict[int, ServerConfig],
            dict[str, IdPwConfig],
        set[int],
            dict[int, datetime],
            dict[int, int],
        dict[int, str],
        set[int],
    ]:
        try:
            version = payload.get("version")
            if type(version) is not int or version not in (
                2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31
            ):
                raise ValueError("Unsupported snapshot version.")
            if not isinstance(payload["scrims"], list):
                raise ValueError("Invalid scrim list.")
            raw_authorized_guild_ids = payload.get("authorized_guild_ids", [])
            if not isinstance(raw_authorized_guild_ids, list):
                raise ValueError("Invalid authorized guild list.")
            if any(
                type(guild_id) is not int or not _positive_id(guild_id)
                for guild_id in raw_authorized_guild_ids
            ):
                raise ValueError("Invalid authorized guild ID.")
            authorized_guild_ids = set(raw_authorized_guild_ids)
            if len(authorized_guild_ids) != len(raw_authorized_guild_ids):
                raise ValueError("Duplicate authorized guild ID.")
            raw_authorized_guild_expires_at = payload.get(
                "authorized_guild_expires_at", {}
            )
            if not isinstance(raw_authorized_guild_expires_at, dict):
                raise ValueError("Invalid authorized guild expiration map.")
            authorized_guild_expires_at: dict[int, datetime] = {}
            for raw_guild_id, raw_expires_at in (
                raw_authorized_guild_expires_at.items()
            ):
                if (
                    not isinstance(raw_guild_id, str)
                    or not raw_guild_id.isdigit()
                    or not _positive_id(int(raw_guild_id))
                    or int(raw_guild_id) not in authorized_guild_ids
                    or not isinstance(raw_expires_at, str)
                ):
                    raise ValueError("Invalid authorized guild expiration.")
                try:
                    expires_at = datetime.fromisoformat(raw_expires_at)
                except ValueError as error:
                    raise ValueError("Invalid authorized guild expiration.") from error
                if expires_at.tzinfo is None:
                    raise ValueError("Authorized guild expiration must include a timezone.")
                authorized_guild_expires_at[int(raw_guild_id)] = (
                    expires_at.astimezone(timezone.utc)
                )
            raw_authorized_guild_duration_days = payload.get(
                "authorized_guild_duration_days", {}
            )
            if not isinstance(raw_authorized_guild_duration_days, dict):
                raise ValueError("Invalid authorized guild duration map.")
            authorized_guild_duration_days: dict[int, int] = {}
            now = datetime.now(timezone.utc)
            for raw_guild_id, raw_duration_days in (
                raw_authorized_guild_duration_days.items()
            ):
                if (
                    not isinstance(raw_guild_id, str)
                    or not raw_guild_id.isdigit()
                    or not _positive_id(int(raw_guild_id))
                    or int(raw_guild_id) not in authorized_guild_ids
                    or type(raw_duration_days) is not int
                    or raw_duration_days < 0
                ):
                    raise ValueError("Invalid authorized guild duration.")
                authorized_guild_duration_days[int(raw_guild_id)] = raw_duration_days
            for guild_id in authorized_guild_ids:
                if guild_id in authorized_guild_duration_days:
                    continue
                expires_at = authorized_guild_expires_at.get(guild_id)
                if expires_at is None:
                    authorized_guild_duration_days[guild_id] = 0
                else:
                    remaining = (expires_at - now).total_seconds()
                    authorized_guild_duration_days[guild_id] = max(
                        1, math.ceil(remaining / timedelta(days=1).total_seconds())
                    )
            raw_authorized_guild_license_types = payload.get(
                "authorized_guild_license_types", {}
            )
            if not isinstance(raw_authorized_guild_license_types, dict):
                raise ValueError("Invalid authorized guild license map.")
            authorized_guild_license_types: dict[int, str] = {}
            for raw_guild_id, raw_license_type in (
                raw_authorized_guild_license_types.items()
            ):
                if (
                    not isinstance(raw_guild_id, str)
                    or not raw_guild_id.isdigit()
                    or not _positive_id(int(raw_guild_id))
                    or int(raw_guild_id) not in authorized_guild_ids
                    or raw_license_type not in LICENSE_TYPES
                ):
                    raise ValueError("Invalid authorized guild license.")
                authorized_guild_license_types[int(raw_guild_id)] = (
                    raw_license_type
                )
            raw_authorized_admin_ids = payload.get("authorized_admin_ids", [])
            if not isinstance(raw_authorized_admin_ids, list):
                raise ValueError("Invalid authorized admin list.")
            if any(
                type(user_id) is not int or not _positive_id(user_id)
                for user_id in raw_authorized_admin_ids
            ):
                raise ValueError("Invalid authorized admin ID.")
            authorized_admin_ids = set(raw_authorized_admin_ids)
            if len(authorized_admin_ids) != len(raw_authorized_admin_ids):
                raise ValueError("Duplicate authorized admin ID.")
            legacy_server_emojis: dict[int, dict[str, str]] = {}
            if payload["version"] < 9:
                raw_legacy_configs = payload.get("server_configs", [])
                if not isinstance(raw_legacy_configs, list):
                    raise ValueError("Invalid server configuration list.")
                for legacy_entry in raw_legacy_configs:
                    if not isinstance(legacy_entry, dict):
                        continue
                    guild_id = legacy_entry.get("guild_id")
                    legacy_server_emojis[guild_id] = {
                        field_name: legacy_entry.get(
                            field_name, default
                        )
                        for field_name, default in DEFAULT_SCRIM_EMOJIS.items()
                    }
            result: dict[str, Scrim] = {}
            names: set[tuple[int, str]] = set()
            channels: set[tuple[int, int]] = set()
            counts: dict[int, int] = {}
            for entry in payload["scrims"]:
                values = dict(entry)
                if payload["version"] == 2:
                    values.setdefault("staff_role_id", None)
                    values.setdefault("manager_role_id", None)
                    values.setdefault("cap_channel_id", None)
                    values.setdefault("logs_channel_id", None)
                    values.setdefault("history_channel_id", None)
                    values.setdefault("staff_message_id", None)
                    values.setdefault("is_open", True)
                if payload["version"] < 5:
                    values.setdefault("slot_start", DEFAULT_SLOT_START)
                    values.setdefault("slot_end", DEFAULT_SLOT_END)
                if payload["version"] < 7:
                    # The former per-scrim role was used for both concepts.
                    # Preserve it as the manager role; the server Staff role
                    # is copied into staff_role_id after configs are decoded.
                    values.setdefault("manager_role_id", values.get("staff_role_id"))
                if payload["version"] < 16:
                    legacy_role_id = values.pop("manager_role_id", None)
                    values.setdefault("pending_role_id", legacy_role_id)
                    values.setdefault("confirmed_role_id", None)
                if payload["version"] < 17:
                    values.setdefault("timezone", DEFAULT_IDPW_TIMEZONE)
                    values.setdefault("maps", list(DEFAULT_MATCH_MAPS))
                    values.setdefault("max_matches", len(DEFAULT_MATCH_MAPS))
                    values.setdefault("pw_type", "fixed")
                    values.setdefault("fixed_pw", "")
                    values.setdefault("current_match_counter", 1)
                if payload["version"] < 18:
                    values.setdefault("match_maps", list(values.get("maps", [])))
                if payload["version"] < 19:
                    values.setdefault("registration_channel_id", None)
                    values.setdefault("registration_role_id", None)
                    if type(values.get("registration_auto_accept")) is not bool:
                        values["registration_auto_accept"] = False
                if payload["version"] < 20:
                    values.setdefault("pending_registrations", [])
                if payload["version"] < 21:
                    for request in values.get("pending_registrations", []):
                        if isinstance(request, dict):
                            request.setdefault("registration_message_id", None)
                if payload["version"] < 22:
                    values.setdefault("registration_open", True)
                if payload["version"] < 25:
                    values.setdefault(
                        "operational_messages",
                        dict(DEFAULT_OPERATIONAL_MESSAGES),
                    )
                if payload["version"] < 26:
                    values.setdefault(
                        "operational_message_refs",
                        dict(DEFAULT_OPERATIONAL_MESSAGE_REFS),
                    )
                if payload["version"] < 23:
                    values.setdefault("kill_points_value", DEFAULT_KILL_POINTS_VALUE)
                    values.setdefault(
                        "placement_points_string",
                        DEFAULT_PLACEMENT_POINTS_STRING,
                    )
                    values.setdefault("leaderboard_layout", "1_col")
                    values.setdefault("match_scores", [])
                if payload["version"] < 24:
                    values.setdefault(
                        "leaderboard_background",
                        DEFAULT_LEADERBOARD_BACKGROUND,
                    )
                if payload["version"] < 27:
                    values.setdefault(
                        "leaderboard_team_count",
                        DEFAULT_LEADERBOARD_TEAM_COUNT,
                    )
                    values.setdefault(
                        "leaderboard_orientation",
                        "horizontal"
                        if values.get("leaderboard_layout") == "2_col"
                        else DEFAULT_LEADERBOARD_ORIENTATION,
                    )
                    values.setdefault(
                        "leaderboard_header_height",
                        DEFAULT_LEADERBOARD_HEADER_HEIGHT,
                    )
                    values.setdefault(
                        "leaderboard_footer_height",
                        DEFAULT_LEADERBOARD_FOOTER_HEIGHT,
                    )
                if payload["version"] < 28:
                    values.setdefault(
                        "leaderboard_accent_color",
                        DEFAULT_LEADERBOARD_ACCENT_COLOR,
                    )
                if payload["version"] < 29:
                    accent_colors = values.get("leaderboard_accent_colors", {})
                    if not isinstance(accent_colors, dict):
                        raise ValueError("Invalid leaderboard text color profiles.")
                    legacy_accent = values.get(
                        "leaderboard_accent_color",
                        DEFAULT_LEADERBOARD_ACCENT_COLOR,
                    )
                    if legacy_accent != DEFAULT_LEADERBOARD_ACCENT_COLOR:
                        profile_key = leaderboard_profile_key(
                            values.get(
                                "leaderboard_orientation",
                                DEFAULT_LEADERBOARD_ORIENTATION,
                            ),
                            values.get(
                                "leaderboard_team_count",
                                DEFAULT_LEADERBOARD_TEAM_COUNT,
                            ),
                        )
                        accent_colors.setdefault(profile_key, legacy_accent)
                    if isinstance(legacy_accent, str):
                        values["leaderboard_accent_color"] = legacy_accent.upper()
                    values["leaderboard_accent_colors"] = accent_colors
                if payload["version"] < 31:
                    values["leaderboard_header_height"] = (
                        DEFAULT_LEADERBOARD_HEADER_HEIGHT
                    )
                    values["leaderboard_footer_height"] = (
                        DEFAULT_LEADERBOARD_FOOTER_HEIGHT
                    )
                values["leaderboard_accent_colors"] = (
                    normalize_leaderboard_accent_colors(
                        values.get("leaderboard_accent_colors", {})
                    )
                )
                # Match selectors now support at most 25 games. Keep older
                # snapshots usable by trimming only the newly unsupported tail.
                if type(values.get("max_matches")) is int and values["max_matches"] > MAX_MATCHES:
                    values["max_matches"] = MAX_MATCHES
                    if type(values.get("current_match_counter")) is int:
                        values["current_match_counter"] = min(
                            values["current_match_counter"], MAX_MATCHES
                        )
                if isinstance(values.get("maps"), list) and len(values["maps"]) > MAX_MAP_POOL:
                    values["maps"] = values["maps"][:MAX_MAP_POOL]
                if isinstance(values.get("match_maps"), list):
                    values["match_maps"] = values["match_maps"][: values.get("max_matches", MAX_MATCHES)]
                    if any(
                        game_map not in values.get("maps", [])
                        for game_map in values["match_maps"]
                    ):
                        values["match_maps"] = []
                if payload["version"] < 15:
                    values.setdefault("cap_channel_id", None)
                if payload["version"] < 9:
                    legacy_emojis = legacy_server_emojis.get(
                        values.get("guild_id"), DEFAULT_SCRIM_EMOJIS
                    )
                    for field_name, default in DEFAULT_SCRIM_EMOJIS.items():
                        values.setdefault(
                            field_name, legacy_emojis.get(field_name, default)
                        )
                values["slots"] = _read_slots(
                    values["slots"],
                    values["slot_start"],
                    values["slot_end"],
                )
                values["pending_registrations"] = _read_registration_requests(
                    values["pending_registrations"],
                    values["slots"],
                    values["slot_start"],
                    values["slot_end"],
                )
                values["match_scores"] = _read_match_scores(
                    values["match_scores"],
                    values["slots"],
                    values["max_matches"],
                )
                values["operational_messages"] = normalize_operational_messages(
                    values.get("operational_messages")
                )
                values["operational_message_refs"] = normalize_operational_message_refs(
                    values.get("operational_message_refs")
                )
                # Runtime-only fields must never be read from disk.
                if set(values) != {
                    "id", "guild_id", "name", "public_channel_id",
                    "staff_channel_id", "staff_role_id", "logs_channel_id",
                    "pending_role_id", "confirmed_role_id",
                    "cap_channel_id",
                    "history_channel_id", "registration_channel_id",
                    "registration_role_id", "registration_auto_accept",
                    "public_message_id", "staff_message_id",
                    "emoji_available", "emoji_reserved", "emoji_pending",
                     "emoji_confirmed", "is_open", "registration_open",
                     "operational_messages",
                     "operational_message_refs",
                     "slot_start", "slot_end", "slots",
                    "timezone", "maps", "max_matches", "match_maps", "pw_type", "fixed_pw",
                     "current_match_counter", "kill_points_value",
                     "placement_points_string", "leaderboard_layout",
                     "leaderboard_background",
                      "leaderboard_accent_color",
                      "leaderboard_accent_colors",
                     "leaderboard_team_count", "leaderboard_orientation",
                     "leaderboard_header_height", "leaderboard_footer_height",
                     "match_scores", "pending_registrations",
                }:
                    raise ValueError("Invalid scrim fields.")
                scrim = Scrim(**values)
                scrim.leaderboard_accent_colors = (
                    normalize_leaderboard_accent_colors(
                        scrim.leaderboard_accent_colors
                    )
                )
                try:
                    (
                        scrim.max_matches,
                        scrim.maps,
                        scrim.match_maps,
                    ) = normalize_match_configuration(
                        scrim.max_matches,
                        scrim.maps,
                        scrim.match_maps,
                    )
                except ValueError as error:
                    raise ValueError("Invalid scrim map configuration.") from error
                if (
                    not isinstance(scrim.id, str)
                    or not re.fullmatch(r"[a-f0-9]{16}", scrim.id)
                    or scrim.id in result
                    or not _positive_id(scrim.guild_id)
                    or not _positive_id(scrim.public_channel_id)
                    or not _positive_id(scrim.staff_channel_id)
                    or (
                        scrim.staff_role_id is not None
                        and not _positive_id(scrim.staff_role_id)
                    )
                    or (
                        scrim.pending_role_id is not None
                        and not _positive_id(scrim.pending_role_id)
                    )
                    or (
                        scrim.cap_channel_id is not None
                        and not _positive_id(scrim.cap_channel_id)
                    )
                    or (
                        payload["version"] >= 7
                        and scrim.staff_role_id is not None
                        and scrim.pending_role_id is not None
                        and scrim.staff_role_id == scrim.pending_role_id
                    )
                    or (
                        scrim.confirmed_role_id is not None
                        and not _positive_id(scrim.confirmed_role_id)
                    )
                    or (
                        scrim.staff_role_id is not None
                        and scrim.confirmed_role_id is not None
                        and scrim.staff_role_id == scrim.confirmed_role_id
                    )
                    or (
                        scrim.pending_role_id is not None
                        and scrim.confirmed_role_id is not None
                        and scrim.pending_role_id == scrim.confirmed_role_id
                    )
                    or (
                        scrim.logs_channel_id is not None
                        and not _positive_id(scrim.logs_channel_id)
                    )
                    or (
                        scrim.history_channel_id is not None
                        and not _positive_id(scrim.history_channel_id)
                    )
                    or (
                        scrim.registration_channel_id is not None
                        and not _positive_id(scrim.registration_channel_id)
                    )
                    or (
                        scrim.registration_role_id is not None
                        and not _positive_id(scrim.registration_role_id)
                    )
                    or type(scrim.registration_auto_accept) is not bool
                    or (scrim.public_message_id is not None and not _positive_id(scrim.public_message_id))
                    or (scrim.staff_message_id is not None and not _positive_id(scrim.staff_message_id))
                    or any(
                        not isinstance(getattr(scrim, field_name), str)
                        or not getattr(scrim, field_name).strip()
                        or len(getattr(scrim, field_name)) > 100
                        or any(
                            character in getattr(scrim, field_name)
                            for character in ("\r", "\n")
                        )
                        for field_name in EMOJI_FIELDS
                    )
                    or type(scrim.is_open) is not bool
                    or validate_slot_range(scrim.slot_start, scrim.slot_end)
                    != (scrim.slot_start, scrim.slot_end)
                    or normalize_name(scrim.name) != scrim.name
                    or not valid_timezone_name(scrim.timezone)
                    or type(scrim.max_matches) is not int
                    or not 1 <= scrim.max_matches <= MAX_MATCHES
                    or scrim.pw_type not in PASSWORD_TYPES
                    or not isinstance(scrim.fixed_pw, str)
                    or len(scrim.fixed_pw) > 100
                    or any(character in scrim.fixed_pw for character in ("\r", "\n"))
                    or type(scrim.current_match_counter) is not int
                    or not 1 <= scrim.current_match_counter <= scrim.max_matches
                    or type(scrim.kill_points_value) is not int
                    or scrim.kill_points_value < 0
                    or (
                        not isinstance(scrim.placement_points_string, str)
                        or not parse_placement_points(scrim.placement_points_string)
                    )
                    or scrim.leaderboard_layout not in LEADERBOARD_LAYOUTS
                    or scrim.leaderboard_background not in LEADERBOARD_BACKGROUNDS
                    or not isinstance(scrim.leaderboard_accent_color, str)
                    or re.fullmatch(
                        r"#[0-9A-Fa-f]{6}",
                        scrim.leaderboard_accent_color,
                    ) is None
                    or normalize_leaderboard_accent_colors(
                        scrim.leaderboard_accent_colors
                    )
                    != scrim.leaderboard_accent_colors
                    or scrim.leaderboard_team_count not in LEADERBOARD_TEAM_COUNTS
                    or scrim.leaderboard_orientation not in LEADERBOARD_ORIENTATIONS
                    or scrim.leaderboard_header_height not in LEADERBOARD_HEADER_HEIGHTS
                    or scrim.leaderboard_footer_height not in LEADERBOARD_FOOTER_HEIGHTS
                ):
                    raise ValueError("Invalid scrim identity or channel.")
                name_key = (scrim.guild_id, scrim.name.casefold())
                if name_key in names:
                    raise ValueError("Scrim names must be unique within a server.")
                names.add(name_key)
                configured_channels = (
                    scrim.public_channel_id,
                    scrim.staff_channel_id,
                    scrim.cap_channel_id,
                    scrim.logs_channel_id,
                    scrim.history_channel_id,
                    scrim.registration_channel_id,
                )
                for channel_id in filter(None, configured_channels):
                    channel_key = (scrim.guild_id, channel_id)
                    if channel_key in channels:
                        raise ValueError("Each scrim must have its own distinct public and staff channels.")
                    channels.add(channel_key)
                counts[scrim.guild_id] = counts.get(scrim.guild_id, 0) + 1
                if counts[scrim.guild_id] > MAX_SCRIMS_PER_GUILD:
                    raise ValueError("A server can have at most 25 scrims.")
                result[scrim.id] = scrim
            raw_configs = payload.get("server_configs", [])
            if not isinstance(raw_configs, list):
                raise ValueError("Invalid server configuration list.")
            configs: dict[int, ServerConfig] = {}
            for entry in raw_configs:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid server configuration fields.")
                config_entry = dict(entry)
                if payload["version"] < 9:
                    for field_name in EMOJI_FIELDS:
                        config_entry.pop(field_name, None)
                config_entry.setdefault("license_type", DEFAULT_LICENSE_TYPE)
                if set(config_entry) != {
                    "guild_id",
                    "head_staff_role_id",
                    "staff_role_id",
                    "logs_channel_id",
                    "license_type",
                }:
                    raise ValueError("Invalid server configuration fields.")
                config = ServerConfig(**config_entry)
                if (
                    not _positive_id(config.guild_id)
                    or (
                        config.head_staff_role_id is not None
                        and not _positive_id(config.head_staff_role_id)
                    )
                    or (
                        config.staff_role_id is not None
                        and not _positive_id(config.staff_role_id)
                    )
                    or (
                        config.logs_channel_id is not None
                        and not _positive_id(config.logs_channel_id)
                    )
                    or (
                        config.head_staff_role_id is not None
                        and config.head_staff_role_id == config.staff_role_id
                    )
                    or config.license_type not in LICENSE_TYPES
                    or config.guild_id in configs
                ):
                    raise ValueError("Invalid server configuration.")
                configs[config.guild_id] = config
            for guild_id in authorized_guild_ids:
                if guild_id not in authorized_guild_license_types:
                    config = configs.get(guild_id)
                    authorized_guild_license_types[guild_id] = (
                        config.license_type
                        if config is not None
                        else DEFAULT_LICENSE_TYPE
                    )
            if payload["version"] < 7:
                for scrim in result.values():
                    server_config = configs.get(scrim.guild_id)
                    if server_config is not None:
                        scrim.staff_role_id = server_config.staff_role_id
                    if scrim.staff_role_id == scrim.pending_role_id:
                        # The legacy snapshot cannot tell which one was
                        # intended. Preserve command permissions and require
                        # the Manager role to be selected explicitly.
                        scrim.pending_role_id = None
            raw_idpw_configs = payload.get("idpw_configs", [])
            if not isinstance(raw_idpw_configs, list):
                raise ValueError("Invalid ID/password configuration list.")
            idpw_configs: dict[str, IdPwConfig] = {}
            for entry in raw_idpw_configs:
                if payload["version"] < 6:
                    if not isinstance(entry, dict) or set(entry) != {
                        "guild_id",
                        "target_channel_id",
                        "fixed_password",
                    }:
                        raise ValueError("Invalid ID/password configuration fields.")
                    guild_id = entry["guild_id"]
                    candidates = [
                        scrim for scrim in result.values()
                        if scrim.guild_id == guild_id
                    ]
                    # A former guild-wide configuration is safe to migrate only
                    # when that guild has exactly one scrim. With multiple scrims,
                    # the administrator must choose the target explicitly.
                    if len(candidates) != 1:
                        continue
                    config = IdPwConfig(
                        candidates[0].id,
                        entry["target_channel_id"],
                        entry["fixed_password"],
                    )
                else:
                    legacy_fields = {
                        "scrim_id",
                        "target_channel_id",
                        "fixed_password",
                        "announcement_message_id",
                    }
                    current_fields = legacy_fields | {"timezone_name"}
                    if (
                        not isinstance(entry, dict)
                        or set(entry) not in (legacy_fields, current_fields)
                    ):
                        raise ValueError("Invalid ID/password configuration fields.")
                    config_data = dict(entry)
                    config_data.setdefault("timezone_name", DEFAULT_IDPW_TIMEZONE)
                    config = IdPwConfig(**config_data)
                if (
                    not isinstance(config.scrim_id, str)
                    or config.scrim_id not in result
                    or not _positive_id(config.target_channel_id)
                    or not isinstance(config.fixed_password, str)
                    or (
                        result[config.scrim_id].pw_type == "fixed"
                        and not config.fixed_password.strip()
                    )
                    or len(config.fixed_password) > 100
                    or not valid_timezone_name(config.timezone_name)
                    or (
                        config.announcement_message_id is not None
                        and not _positive_id(config.announcement_message_id)
                    )
                    or config.scrim_id in idpw_configs
                ):
                    raise ValueError("Invalid ID/password configuration.")
                idpw_configs[config.scrim_id] = config
            if payload["version"] < 17:
                for scrim_id, config in idpw_configs.items():
                    scrim = result.get(scrim_id)
                    if scrim is not None:
                        scrim.timezone = config.timezone_name
                        scrim.fixed_pw = config.fixed_password
                        scrim.pw_type = "fixed"
            legacy = payload.get("legacy")
            return (
                result,
                _legacy_payload(legacy) if legacy is not None else None,
                configs,
                idpw_configs,
                authorized_guild_ids,
                authorized_guild_expires_at,
                authorized_guild_duration_days,
                authorized_guild_license_types,
                authorized_admin_ids,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SlotStorageError(
                f"Invalid scrim snapshot; existing data was not reset: {error}"
            ) from error

    def load(self) -> None:
        payload = self.store.load()
        if payload is None:
            self.store.save(self.payload())
            return
        if payload.get("version") == 1:
            try:
                legacy = _legacy_payload(payload)
            except (KeyError, TypeError, ValueError) as error:
                raise SlotStorageError("Invalid legacy snapshot; migration was stopped.") from error
            new_payload = {
                "version": 31,
                "scrims": [],
                "server_configs": [],
                "idpw_configs": [],
                "authorized_guild_ids": [],
                "authorized_guild_expires_at": {},
                "authorized_guild_duration_days": {},
                "authorized_guild_license_types": {},
                "authorized_admin_ids": [],
                "legacy": legacy,
            }
            self.store.save(new_payload)  # Preserve all v1 content before replacing runtime state.
            (
                self.scrims,
                self.legacy,
                self.server_configs,
                self.idpw_configs,
                self.authorized_guild_ids,
                self.authorized_guild_expires_at,
                self.authorized_guild_duration_days,
                self.authorized_guild_license_types,
                self.authorized_admin_ids,
            ) = self._decode(new_payload)
            return
        (
            restored,
            legacy,
            configs,
            idpw_configs,
            authorized_guild_ids,
            authorized_guild_expires_at,
            authorized_guild_duration_days,
            authorized_guild_license_types,
            authorized_admin_ids,
        ) = self._decode(payload)
        self.scrims, self.legacy, self.server_configs, self.idpw_configs = (
            restored,
            legacy,
            configs,
            idpw_configs,
        )
        self.authorized_guild_ids = authorized_guild_ids
        self.authorized_guild_expires_at = authorized_guild_expires_at
        self.authorized_guild_duration_days = authorized_guild_duration_days
        self.authorized_guild_license_types = authorized_guild_license_types
        self.authorized_admin_ids = authorized_admin_ids
        if payload.get("version", 0) < 31:
            self.store.save(self.payload())

    @contextmanager
    def transaction(self):
        before = self.payload()
        objects = self.scrims.copy()
        config_objects = self.server_configs.copy()
        idpw_config_objects = self.idpw_configs.copy()
        authorized_guild_ids = self.authorized_guild_ids.copy()
        authorized_guild_expires_at = self.authorized_guild_expires_at.copy()
        authorized_guild_duration_days = self.authorized_guild_duration_days.copy()
        authorized_guild_license_types = self.authorized_guild_license_types.copy()
        authorized_admin_ids = self.authorized_admin_ids.copy()
        try:
            yield
            payload = self.payload()
            self._decode(payload)
            self.store.save(payload)
        except Exception:
            (
                restored,
                legacy,
                configs,
                idpw_configs,
                restored_authorized_guild_ids,
                restored_authorized_guild_expires_at,
                restored_authorized_guild_duration_days,
                restored_authorized_guild_license_types,
                restored_authorized_admin_ids,
            ) = self._decode(before)
            for scrim_id, scrim in self.scrims.items():
                if scrim_id not in objects:
                    scrim.deleted = True
            for scrim_id, saved in restored.items():
                target = objects[scrim_id]
                for name in (
                    "guild_id",
                    "name",
                    "public_channel_id",
                    "staff_channel_id",
                    "staff_role_id",
                    "pending_role_id",
                    "confirmed_role_id",
                    "logs_channel_id",
                    "history_channel_id",
                    "registration_channel_id",
                    "registration_role_id",
                    "registration_auto_accept",
                    "emoji_available",
                    "emoji_reserved",
                    "emoji_pending",
                    "emoji_confirmed",
                    "public_message_id",
                    "staff_message_id",
                    "is_open",
                    "registration_open",
                    "operational_messages",
                    "operational_message_refs",
                    "slot_start",
                    "slot_end",
                    "kill_points_value",
                    "placement_points_string",
                    "leaderboard_layout",
                    "leaderboard_background",
                    "leaderboard_team_count",
                    "leaderboard_orientation",
                    "leaderboard_header_height",
                    "leaderboard_footer_height",
                    "match_scores",
                    "pending_registrations",
                ):
                    setattr(target, name, getattr(saved, name))
                target.slots = {
                    number: target.slots.get(number, Slot(number))
                    for number in saved.slots
                }
                for number, slot in saved.slots.items():
                    target.slots[number].__dict__.update(vars(slot))
                target.deleted = False
            self.scrims = objects
            self.legacy = legacy
            self.server_configs = config_objects
            self.idpw_configs = idpw_config_objects
            self.authorized_guild_ids = authorized_guild_ids
            self.authorized_guild_expires_at = authorized_guild_expires_at
            self.authorized_guild_duration_days = authorized_guild_duration_days
            self.authorized_guild_license_types = (
                restored_authorized_guild_license_types
            )
            self.authorized_admin_ids = authorized_admin_ids
            raise

    def get_server_config(self, guild_id: int) -> ServerConfig | None:
        return self.server_configs.get(guild_id)

    def save_server_config(
        self,
        guild_id: int,
        *,
        head_staff_role_id: int,
        staff_role_id: int,
        logs_channel_id: int,
        license_type: str | None = None,
    ) -> ServerConfig:
        if license_type is None:
            license_type = self.get_server_license_type(guild_id)
        config = ServerConfig(
            guild_id,
            head_staff_role_id,
            staff_role_id,
            logs_channel_id,
            license_type,
        )
        if (
            not _positive_id(guild_id)
            or not _positive_id(head_staff_role_id)
            or not _positive_id(staff_role_id)
            or not _positive_id(logs_channel_id)
            or head_staff_role_id == staff_role_id
            or license_type not in LICENSE_TYPES
        ):
            raise ValueError("Invalid server configuration.")
        with self.transaction():
            self.server_configs[guild_id] = config
        return config

    def save_scrims_staff_role(self, guild_id: int, staff_role_id: int) -> ServerConfig:
        """Persist the global role required for scrim administration commands."""
        if not _positive_id(guild_id) or not _positive_id(staff_role_id):
            raise ValueError("The scrims Staff role ID must be a positive integer.")
        current = self.get_server_config(guild_id)
        config = ServerConfig(
            guild_id,
            current.head_staff_role_id if current is not None else None,
            staff_role_id,
            current.logs_channel_id if current is not None else None,
            current.license_type if current is not None else DEFAULT_LICENSE_TYPE,
        )
        with self.transaction():
            self.server_configs[guild_id] = config
        return config

    def save_scrim_emoji(
        self, scrim_id: str, guild_id: int, field_name: str, emoji: str
    ) -> Scrim:
        if field_name not in EMOJI_FIELDS:
            raise ValueError("Invalid scrim emoji field.")
        if (
            not isinstance(emoji, str)
            or not emoji.strip()
            or len(emoji) > 100
            or any(character in emoji for character in ("\r", "\n"))
        ):
            raise ValueError("Invalid scrim emoji.")
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        with self.transaction():
            setattr(scrim, field_name, emoji)
        return scrim

    def reset_scrim_emojis(self, scrim_id: str, guild_id: int) -> Scrim:
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        with self.transaction():
            for field_name, emoji in DEFAULT_SCRIM_EMOJIS.items():
                setattr(scrim, field_name, emoji)
        return scrim

    def update_operational_message(
        self,
        scrim_id: str,
        guild_id: int,
        key: str,
        message: str,
    ) -> Scrim:
        return self.update_operational_messages(
            scrim_id,
            guild_id,
            {key: message},
        )

    def update_operational_messages(
        self,
        scrim_id: str,
        guild_id: int,
        messages: dict[str, str],
    ) -> Scrim:
        if not isinstance(messages, dict) or any(
            key not in OPERATIONAL_MESSAGE_KEYS for key in messages
        ):
            raise ValueError("Unknown operational message.")
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        normalized = normalize_operational_messages(
            {**scrim.operational_messages, **messages}
        )
        with self.transaction():
            scrim.operational_messages = normalized
        return scrim

    def set_operational_message_reference(
        self,
        scrim_id: str,
        guild_id: int,
        context: str,
        message_id: int,
        channel_id: int,
    ) -> Scrim:
        if context not in OPERATIONAL_MESSAGE_CONTEXTS:
            raise ValueError("Unknown operational message context.")
        refs = normalize_operational_message_refs(
            {context: {"message_id": message_id, "channel_id": channel_id}}
        )
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        updated = dict(scrim.operational_message_refs)
        updated[context] = refs[context]
        with self.transaction():
            scrim.operational_message_refs = normalize_operational_message_refs(
                updated
            )
        return scrim

    def clear_operational_message_reference(
        self,
        scrim_id: str,
        guild_id: int,
        context: str,
    ) -> Scrim:
        if context not in OPERATIONAL_MESSAGE_CONTEXTS:
            raise ValueError("Unknown operational message context.")
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        updated = dict(scrim.operational_message_refs)
        updated.pop(context, None)
        with self.transaction():
            scrim.operational_message_refs = normalize_operational_message_refs(
                updated
            )
        return scrim

    def reset_operational_message(
        self,
        scrim_id: str,
        guild_id: int,
        key: str,
    ) -> Scrim:
        if key not in OPERATIONAL_MESSAGE_KEYS:
            raise ValueError("Unknown operational message.")
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        updated = dict(scrim.operational_messages)
        updated[key] = DEFAULT_OPERATIONAL_MESSAGES[key]
        with self.transaction():
            scrim.operational_messages = normalize_operational_messages(updated)
        return scrim

    def update_leaderboard_settings(
        self,
        scrim_id: str,
        guild_id: int,
        *,
        kill_points_value: int | object = _UNSET,
        placement_points_string: str | object = _UNSET,
        leaderboard_layout: str | object = _UNSET,
        leaderboard_background: str | object = _UNSET,
        leaderboard_accent_color: str | object = _UNSET,
        leaderboard_team_count: int | object = _UNSET,
        leaderboard_orientation: str | object = _UNSET,
        leaderboard_header_height: int | object = _UNSET,
        leaderboard_footer_height: int | object = _UNSET,
    ) -> Scrim:
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        next_kill_points = (
            scrim.kill_points_value
            if kill_points_value is _UNSET
            else kill_points_value
        )
        next_placement_points = (
            scrim.placement_points_string
            if placement_points_string is _UNSET
            else placement_points_string
        )
        next_layout = (
            scrim.leaderboard_layout
            if leaderboard_layout is _UNSET
            else leaderboard_layout
        )
        next_background = (
            scrim.leaderboard_background
            if leaderboard_background is _UNSET
            else leaderboard_background
        )
        next_accent_color = (
            scrim.leaderboard_accent_color
            if leaderboard_accent_color is _UNSET
            else leaderboard_accent_color
        )
        if isinstance(next_accent_color, str):
            next_accent_color = next_accent_color.upper()
        next_accent_colors = normalize_leaderboard_accent_colors(
            scrim.leaderboard_accent_colors
        )
        next_team_count = (
            scrim.leaderboard_team_count
            if leaderboard_team_count is _UNSET
            else leaderboard_team_count
        )
        if leaderboard_orientation is _UNSET:
            next_orientation = scrim.leaderboard_orientation
            if leaderboard_layout is not _UNSET:
                next_orientation = (
                    "horizontal" if next_layout == "2_col" else "vertical"
                )
        else:
            next_orientation = leaderboard_orientation
        if (
            leaderboard_header_height is not _UNSET
            and leaderboard_header_height != DEFAULT_LEADERBOARD_HEADER_HEIGHT
        ) or (
            leaderboard_footer_height is not _UNSET
            and leaderboard_footer_height != DEFAULT_LEADERBOARD_FOOTER_HEIGHT
        ):
            raise ValueError(
                "Leaderboard header and footer dimensions are fixed."
            )
        next_header_height = DEFAULT_LEADERBOARD_HEADER_HEIGHT
        next_footer_height = DEFAULT_LEADERBOARD_FOOTER_HEIGHT
        if (
            type(next_kill_points) is not int
            or next_kill_points < 0
            or not parse_placement_points(next_placement_points)
            or next_layout not in LEADERBOARD_LAYOUTS
            or next_background not in LEADERBOARD_BACKGROUNDS
            or not isinstance(next_accent_color, str)
            or re.fullmatch(r"#[0-9A-F]{6}", next_accent_color) is None
            or next_team_count not in LEADERBOARD_TEAM_COUNTS
            or next_orientation not in LEADERBOARD_ORIENTATIONS
            or next_header_height not in LEADERBOARD_HEADER_HEIGHTS
            or next_footer_height not in LEADERBOARD_FOOTER_HEIGHTS
        ):
            raise ValueError("Invalid leaderboard configuration.")
        is_gold = self.get_server_license_type(guild_id) == "Gold"
        if (
            not is_gold
            and leaderboard_team_count is not _UNSET
            and next_team_count != 20
        ):
            raise ValueError(
                "Standard licenses are locked to 20 teams."
            )
        profile_team_count = next_team_count if is_gold else 20
        active_profile_key = leaderboard_profile_key(
            next_orientation,
            profile_team_count,
        )
        if leaderboard_accent_color is not _UNSET:
            if next_accent_color == DEFAULT_LEADERBOARD_ACCENT_COLOR:
                next_accent_colors.pop(active_profile_key, None)
            else:
                next_accent_colors[active_profile_key] = next_accent_color
        next_accent_color = next_accent_colors.get(
            active_profile_key,
            DEFAULT_LEADERBOARD_ACCENT_COLOR,
        )
        with self.transaction():
            scrim.kill_points_value = next_kill_points
            scrim.placement_points_string = next_placement_points
            scrim.leaderboard_layout = (
                "2_col" if next_orientation == "horizontal" else "1_col"
            )
            scrim.leaderboard_background = next_background
            scrim.leaderboard_accent_color = next_accent_color
            scrim.leaderboard_accent_colors = next_accent_colors
            scrim.leaderboard_team_count = next_team_count
            scrim.leaderboard_orientation = next_orientation
            scrim.leaderboard_header_height = next_header_height
            scrim.leaderboard_footer_height = next_footer_height
        return scrim

    def get_match_scores(self, scrim_id: str) -> list[MatchScore]:
        scrim = self.get(scrim_id)
        if scrim is None:
            return []
        return [
            score
            for _, score in sorted(scrim.match_scores.items())
        ]

    def upsert_match_scores(
        self,
        scrim_id: str,
        guild_id: int,
        match_number: int,
        scores: list[MatchScore],
    ) -> int:
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        if (
            type(match_number) is not int
            or not 1 <= match_number <= scrim.max_matches
        ):
            raise ValueError(
                f"Match number must be between 1 and {scrim.max_matches}."
            )
        for score in scores:
            if (
                not isinstance(score, MatchScore)
                or score.match_number != match_number
                or score.slot_number not in scrim.slots
                or scrim.slots[score.slot_number].status == STATUS_AVAILABLE
                or type(score.kills) is not int
                or score.kills < 0
                or type(score.placement) is not int
                or score.placement < 1
            ):
                raise ValueError("Invalid match score.")
        with self.transaction():
            for score in scores:
                scrim.match_scores[(score.match_number, score.slot_number)] = score
        return len(scores)

    def get_idpw_config(self, scrim_id: str) -> IdPwConfig | None:
        return self.idpw_configs.get(scrim_id)

    def save_idpw_config(
        self,
        scrim_id: str,
        *,
        target_channel_id: int,
        fixed_password: str,
        timezone_name: str = DEFAULT_IDPW_TIMEZONE,
        password_type: str = "fixed",
        announcement_message_id: int | None = None,
    ) -> IdPwConfig:
        scrim = self.get(scrim_id)
        if scrim is None:
            raise ValueError("This scrim does not exist.")
        config = IdPwConfig(
            scrim_id,
            target_channel_id,
            fixed_password,
            timezone_name,
            announcement_message_id,
        )
        if (
            not _positive_id(target_channel_id)
            or not isinstance(fixed_password, str)
            or password_type not in PASSWORD_TYPES
            or (password_type == "fixed" and not fixed_password.strip())
            or len(fixed_password) > 100
            or not valid_timezone_name(timezone_name)
            or (
                announcement_message_id is not None
                and not _positive_id(announcement_message_id)
            )
        ):
            raise ValueError("Invalid ID/password configuration.")
        with self.transaction():
            scrim.timezone = timezone_name
            scrim.fixed_pw = fixed_password
            scrim.pw_type = password_type
            self.idpw_configs[scrim_id] = config
        return config

    def set_idpw_announcement(
        self, scrim_id: str, announcement_message_id: int | None
    ) -> IdPwConfig:
        current = self.get_idpw_config(scrim_id)
        if current is None:
            raise ValueError("This scrim has no ID/password configuration.")
        if (
            announcement_message_id is not None
            and not _positive_id(announcement_message_id)
        ):
            raise ValueError("Invalid ID/password announcement message.")
        updated = IdPwConfig(
            current.scrim_id,
            current.target_channel_id,
            current.fixed_password,
            current.timezone_name,
            announcement_message_id,
        )
        with self.transaction():
            self.idpw_configs[scrim_id] = updated
        return updated

    def _new_scrim(
        self,
        guild_id,
        name,
        public_channel_id,
        staff_channel_id,
        *,
        staff_role_id: int | None = None,
        pending_role_id: int | None = None,
        confirmed_role_id: int | None = None,
        cap_channel_id: int | None = None,
        logs_channel_id: int | None = None,
        history_channel_id: int | None = None,
        registration_channel_id: int | None = None,
        registration_role_id: int | None = None,
        registration_auto_accept: bool = False,
        is_open: bool = True,
        slot_start: int = DEFAULT_SLOT_START,
        slot_end: int = DEFAULT_SLOT_END,
        max_matches: int = len(DEFAULT_MATCH_MAPS),
        maps: list[str] | tuple[str, ...] = DEFAULT_MATCH_MAPS,
        match_maps: list[str] | tuple[str, ...] | None = None,
        kill_points_value: int = DEFAULT_KILL_POINTS_VALUE,
        placement_points_string: str = DEFAULT_PLACEMENT_POINTS_STRING,
        leaderboard_layout: str = "1_col",
        leaderboard_background: str = DEFAULT_LEADERBOARD_BACKGROUND,
    ) -> Scrim:
        name = normalize_name(name)
        validate_slot_range(slot_start, slot_end)
        max_matches, maps, match_maps = normalize_match_configuration(
            max_matches, maps, match_maps
        )
        if not all(_positive_id(value) for value in (guild_id, public_channel_id, staff_channel_id)):
            raise ValueError("Server and channel IDs must be positive integers.")
        if any(
            value is not None and not _positive_id(value)
            for value in (
                staff_role_id,
                pending_role_id,
                confirmed_role_id,
                cap_channel_id,
                logs_channel_id,
                history_channel_id,
                registration_channel_id,
                registration_role_id,
            )
        ):
            raise ValueError("Configured role and channel IDs must be positive integers.")
        if type(registration_auto_accept) is not bool:
            raise ValueError("Registration auto-acceptance must be a boolean.")
        if (
            type(kill_points_value) is not int
            or kill_points_value < 0
            or not parse_placement_points(placement_points_string)
            or leaderboard_layout not in LEADERBOARD_LAYOUTS
            or leaderboard_background not in LEADERBOARD_BACKGROUNDS
        ):
            raise ValueError("Invalid leaderboard configuration.")
        if (
            staff_role_id is not None
            and pending_role_id is not None
            and staff_role_id == pending_role_id
        ):
            raise ValueError("Staff and Pending Captain roles must be different.")
        configured_channels = tuple(
            value
            for value in (
                public_channel_id,
                staff_channel_id,
                cap_channel_id,
                logs_channel_id,
                history_channel_id,
                registration_channel_id,
            )
            if value is not None
        )
        if len(set(configured_channels)) != len(configured_channels):
            raise ValueError("Configured channels must be different.")
        if type(is_open) is not bool:
            raise ValueError("The scrim opening state must be boolean.")
        existing = self.list(guild_id)
        if len(existing) >= MAX_SCRIMS_PER_GUILD:
            raise ValueError("This server already has 25 scrims.")
        if any(s.name.casefold() == name.casefold() for s in existing):
            raise ValueError("A scrim with this name already exists on this server.")
        requested = set(configured_channels)
        if any(
            requested.intersection(
                set(
                    filter(
                        None,
                        (
                            s.public_channel_id,
                            s.staff_channel_id,
                            s.cap_channel_id,
                            s.logs_channel_id,
                            s.history_channel_id,
                            s.registration_channel_id,
                        ),
                    )
                )
            )
            for s in existing
        ):
            raise ValueError("One of these channels is already assigned to another scrim.")
        scrim_id = secrets.token_hex(8)
        while scrim_id in self.scrims:
            scrim_id = secrets.token_hex(8)
        if (
            confirmed_role_id is not None
            and staff_role_id is not None
            and staff_role_id == confirmed_role_id
        ):
            raise ValueError("Staff and Confirmed Captain roles must be different.")
        if (
            pending_role_id is not None
            and confirmed_role_id is not None
            and pending_role_id == confirmed_role_id
        ):
            raise ValueError("Pending and Confirmed Captain roles must be different.")
        return Scrim(
            scrim_id,
            guild_id,
            name,
            public_channel_id,
            staff_channel_id,
            staff_role_id=staff_role_id,
            pending_role_id=pending_role_id,
            confirmed_role_id=confirmed_role_id,
            cap_channel_id=cap_channel_id,
            logs_channel_id=logs_channel_id,
            history_channel_id=history_channel_id,
            registration_channel_id=registration_channel_id,
            registration_role_id=registration_role_id,
            registration_auto_accept=registration_auto_accept,
            is_open=is_open,
            slot_start=slot_start,
            slot_end=slot_end,
            slots=empty_slots(slot_start, slot_end),
            max_matches=max_matches,
            maps=maps,
            match_maps=match_maps,
            kill_points_value=kill_points_value,
            placement_points_string=placement_points_string,
            leaderboard_layout=leaderboard_layout,
            leaderboard_background=leaderboard_background,
        )

    def create(
        self,
        guild_id: int,
        name: str,
        public_channel_id: int,
        staff_channel_id: int,
        *,
        staff_role_id: int | None = None,
        pending_role_id: int | None = None,
        confirmed_role_id: int | None = None,
        cap_channel_id: int | None = None,
        logs_channel_id: int | None = None,
        history_channel_id: int | None = None,
        registration_channel_id: int | None = None,
        registration_role_id: int | None = None,
        registration_auto_accept: bool = False,
        is_open: bool = True,
        slot_start: int = DEFAULT_SLOT_START,
        slot_end: int = DEFAULT_SLOT_END,
        max_matches: int = len(DEFAULT_MATCH_MAPS),
        maps: list[str] | tuple[str, ...] = DEFAULT_MATCH_MAPS,
        match_maps: list[str] | tuple[str, ...] | None = None,
        kill_points_value: int = DEFAULT_KILL_POINTS_VALUE,
        placement_points_string: str = DEFAULT_PLACEMENT_POINTS_STRING,
        leaderboard_layout: str = "1_col",
        leaderboard_background: str = DEFAULT_LEADERBOARD_BACKGROUND,
    ) -> Scrim:
        # The caller validates that selected Discord channels belong to this guild.
        # Reusing the original public channel adopts rather than overwrites v1 data.
        if (
            self.legacy is not None
            and self.legacy["public_board"] is not None
            and self.legacy["public_board"]["channel_id"] == public_channel_id
        ):
            return self.claim_legacy(
                guild_id,
                staff_channel_id,
                name,
                cap_channel_id=cap_channel_id,
                max_matches=max_matches,
                maps=maps,
                match_maps=match_maps,
            )
        scrim = self._new_scrim(
            guild_id,
            name,
            public_channel_id,
            staff_channel_id,
            staff_role_id=staff_role_id,
            pending_role_id=pending_role_id,
            confirmed_role_id=confirmed_role_id,
            cap_channel_id=cap_channel_id,
            logs_channel_id=logs_channel_id,
            history_channel_id=history_channel_id,
            registration_channel_id=registration_channel_id,
            registration_role_id=registration_role_id,
            registration_auto_accept=registration_auto_accept,
            is_open=is_open,
            slot_start=slot_start,
            slot_end=slot_end,
            max_matches=max_matches,
            maps=maps,
            match_maps=match_maps,
            kill_points_value=kill_points_value,
            placement_points_string=placement_points_string,
            leaderboard_layout=leaderboard_layout,
            leaderboard_background=leaderboard_background,
        )
        with self.transaction():
            self.scrims[scrim.id] = scrim
        return scrim

    def update_scrim(
        self,
        scrim_id: str,
        guild_id: int,
        *,
        name: str | object = _UNSET,
        public_channel_id: int | object = _UNSET,
        staff_channel_id: int | object = _UNSET,
        staff_role_id: int | None | object = _UNSET,
        pending_role_id: int | None | object = _UNSET,
        confirmed_role_id: int | None | object = _UNSET,
        cap_channel_id: int | None | object = _UNSET,
        logs_channel_id: int | None | object = _UNSET,
        history_channel_id: int | None | object = _UNSET,
        registration_channel_id: int | None | object = _UNSET,
        registration_role_id: int | None | object = _UNSET,
        registration_auto_accept: bool | object = _UNSET,
        slot_start: int | object = _UNSET,
        slot_end: int | object = _UNSET,
        max_matches: int | object = _UNSET,
        maps: list[str] | tuple[str, ...] | object = _UNSET,
        match_maps: list[str] | tuple[str, ...] | object = _UNSET,
    ) -> Scrim:
        """Update selected scrim settings while preserving all other state."""
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")

        next_name = scrim.name if name is _UNSET else normalize_name(name)
        next_public = (
            scrim.public_channel_id
            if public_channel_id is _UNSET
            else public_channel_id
        )
        next_staff = (
            scrim.staff_channel_id
            if staff_channel_id is _UNSET
            else staff_channel_id
        )
        next_staff_role = (
            scrim.staff_role_id if staff_role_id is _UNSET else staff_role_id
        )
        next_pending_role = (
            scrim.pending_role_id
            if pending_role_id is _UNSET
            else pending_role_id
        )
        next_confirmed_role = (
            scrim.confirmed_role_id
            if confirmed_role_id is _UNSET
            else confirmed_role_id
        )
        next_logs = (
            scrim.logs_channel_id
            if logs_channel_id is _UNSET
            else logs_channel_id
        )
        next_cap = (
            scrim.cap_channel_id
            if cap_channel_id is _UNSET
            else cap_channel_id
        )
        next_history = (
            scrim.history_channel_id
            if history_channel_id is _UNSET
            else history_channel_id
        )
        next_registration_channel = (
            scrim.registration_channel_id
            if registration_channel_id is _UNSET
            else registration_channel_id
        )
        next_registration_role = (
            scrim.registration_role_id
            if registration_role_id is _UNSET
            else registration_role_id
        )
        next_registration_auto_accept = (
            scrim.registration_auto_accept
            if registration_auto_accept is _UNSET
            else registration_auto_accept
        )
        if next_registration_auto_accept is None:
            next_registration_auto_accept = False
        next_start = scrim.slot_start if slot_start is _UNSET else slot_start
        next_end = scrim.slot_end if slot_end is _UNSET else slot_end
        validate_slot_range(next_start, next_end)
        next_max_matches = (
            scrim.max_matches if max_matches is _UNSET else max_matches
        )
        next_maps = scrim.maps if maps is _UNSET else maps
        next_match_maps = (
            scrim.match_maps if match_maps is _UNSET else match_maps
        )
        next_max_matches, next_maps, next_match_maps = normalize_match_configuration(
            next_max_matches, next_maps, next_match_maps
        )

        if not all(_positive_id(value) for value in (next_public, next_staff)):
            raise ValueError("Public and staff channel IDs must be positive integers.")
        if any(
            value is not None and not _positive_id(value)
            for value in (
                next_staff_role,
                next_pending_role,
                next_confirmed_role,
                next_cap,
                next_logs,
                next_history,
                next_registration_channel,
                next_registration_role,
            )
        ):
            raise ValueError("Configured role and channel IDs must be positive integers.")
        if type(next_registration_auto_accept) is not bool:
            raise ValueError("Registration auto-acceptance must be a boolean.")
        if (
            next_staff_role is not None
            and next_pending_role is not None
            and next_staff_role == next_pending_role
        ):
            raise ValueError("Staff and Pending Captain roles must be different.")
        if (
            next_staff_role is not None
            and next_confirmed_role is not None
            and next_staff_role == next_confirmed_role
        ):
            raise ValueError("Staff and Confirmed Captain roles must be different.")
        if (
            next_pending_role is not None
            and next_confirmed_role is not None
            and next_pending_role == next_confirmed_role
        ):
            raise ValueError("Pending and Confirmed Captain roles must be different.")
        configured_channels = tuple(
            value
            for value in (
                next_public,
                next_staff,
                next_cap,
                next_logs,
                next_history,
                next_registration_channel,
            )
            if value is not None
        )
        if len(set(configured_channels)) != len(configured_channels):
            raise ValueError("Configured channels must be different.")

        others = [other for other in self.list(guild_id) if other.id != scrim_id]
        if any(other.name.casefold() == next_name.casefold() for other in others):
            raise ValueError("A scrim with this name already exists on this server.")
        requested_channels = set(configured_channels)
        if any(
            requested_channels.intersection(
                set(
                    filter(
                        None,
                        (
                            other.public_channel_id,
                            other.staff_channel_id,
                            other.cap_channel_id,
                            other.logs_channel_id,
                            other.history_channel_id,
                            other.registration_channel_id,
                        ),
                    )
                )
            )
            for other in others
        ):
            raise ValueError("One of these channels is already assigned to another scrim.")

        with self.transaction():
            scrim.name = next_name
            scrim.public_channel_id = next_public
            scrim.staff_channel_id = next_staff
            scrim.staff_role_id = next_staff_role
            scrim.pending_role_id = next_pending_role
            scrim.confirmed_role_id = next_confirmed_role
            scrim.cap_channel_id = next_cap
            scrim.logs_channel_id = next_logs
            scrim.history_channel_id = next_history
            scrim.registration_channel_id = next_registration_channel
            scrim.registration_role_id = next_registration_role
            scrim.registration_auto_accept = next_registration_auto_accept
            if (next_start, next_end) != (scrim.slot_start, scrim.slot_end):
                occupied_outside = [
                    slot.number
                    for number, slot in scrim.slots.items()
                    if not next_start <= number <= next_end
                    and slot.status != STATUS_AVAILABLE
                ]
                if occupied_outside:
                    numbers = ", ".join(f"{number:02d}" for number in occupied_outside)
                    raise ValueError(
                        f"Cannot shrink the range while slots {numbers} are occupied."
                    )
                scrim.slots = {
                    number: scrim.slots.get(number, Slot(number))
                    for number in range(next_start, next_end + 1)
                }
                scrim.slot_start = next_start
                scrim.slot_end = next_end
            scrim.max_matches = next_max_matches
            scrim.maps = next_maps
            scrim.match_maps = next_match_maps
            if scrim.current_match_counter > next_max_matches:
                scrim.current_match_counter = 1
        return scrim

    def delete(self, scrim_id: str, guild_id: int) -> Scrim:
        scrim = self.get(scrim_id)
        if scrim is None or scrim.guild_id != guild_id:
            raise ValueError("This scrim does not exist on this server.")
        with self.transaction():
            del self.scrims[scrim_id]
            self.idpw_configs.pop(scrim_id, None)
            scrim.deleted = True
        return scrim

    def claim_legacy(
        self,
        guild_id: int,
        staff_channel_id: int,
        name="Imported Scrim",
        *,
        cap_channel_id: int | None = None,
        max_matches: int = len(DEFAULT_MATCH_MAPS),
        maps: list[str] | tuple[str, ...] = DEFAULT_MATCH_MAPS,
    ) -> Scrim:
        if self.legacy is None or self.legacy["public_board"] is None:
            raise ValueError("No legacy board can be linked. Existing unlinked data is preserved.")
        board = self.legacy["public_board"]
        scrim = self._new_scrim(
            guild_id,
            name,
            board["channel_id"],
            staff_channel_id,
            cap_channel_id=cap_channel_id,
            max_matches=max_matches,
            maps=maps,
        )
        scrim.slots = _read_slots(self.legacy["slots"])
        scrim.public_message_id = board["message_id"]
        with self.transaction():
            self.scrims[scrim.id] = scrim
            self.legacy = None
        return scrim