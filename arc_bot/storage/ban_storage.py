"""Durable, guild-scoped storage and matching for team bans."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_VERSION = 4
DEFAULT_BANNED_EMOJI = "🔨"
LEGACY_BAN_REASON = "No reason recorded (legacy ban)."
_GUILD_ID = re.compile(r"[1-9]\d{0,19}\Z")
_BAN_ID = re.compile(r"[a-f0-9]{16}\Z")


class BanStorageError(RuntimeError):
    """Raised when the ban file cannot be trusted or saved."""


@dataclass(frozen=True)
class BanMatch:
    """An active exact or partial match for a submitted team identity."""

    kind: str
    ban_ids: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    @property
    def is_exact(self) -> bool:
        return self.kind == "exact"


def registration_match_key(value: str) -> str:
    """Use the same NFKC, whitespace, and case normalization as registrations."""
    return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field}.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid {field}.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _default_guild_state() -> dict[str, Any]:
    return {
        "bans_channel_id": None,
        "banned_emoji": DEFAULT_BANNED_EMOJI,
        "board_message_id": None,
        "board_page": 0,
        "punitive_role_id": None,
        "punitive_role_grants": {},
        "scrim_scopes": {},
        "bans": [],
    }


def _validate_id(value: object, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid {field}.")


def validate_ban_state(state: object) -> None:
    if not isinstance(state, dict) or set(state) != {"version", "guilds"}:
        raise ValueError("Invalid ban state root.")
    if type(state["version"]) is not int or state["version"] != STATE_VERSION:
        raise ValueError("Unsupported ban state version.")
    guilds = state["guilds"]
    if not isinstance(guilds, dict):
        raise ValueError("Invalid ban guild map.")
    for guild_id, config in guilds.items():
        if not isinstance(guild_id, str) or _GUILD_ID.fullmatch(guild_id) is None:
            raise ValueError("Invalid ban guild ID.")
        required_config = {
            "bans_channel_id",
            "banned_emoji",
            "board_message_id",
            "board_page",
            "punitive_role_id",
            "punitive_role_grants",
            "scrim_scopes",
            "bans",
        }
        if not isinstance(config, dict) or set(config) != required_config:
            raise ValueError("Invalid ban guild settings.")
        _validate_id(config["bans_channel_id"], "bans channel ID", optional=True)
        _validate_id(config["board_message_id"], "board message ID", optional=True)
        _validate_id(config["punitive_role_id"], "punitive role ID", optional=True)
        if (
            type(config["board_page"]) is not int
            or config["board_page"] < 0
        ):
            raise ValueError("Invalid ban board page.")
        emoji = config["banned_emoji"]
        if not isinstance(emoji, str) or not emoji.strip() or len(emoji) > 100:
            raise ValueError("Invalid banned reaction.")
        scrim_scopes = config["scrim_scopes"]
        if not isinstance(scrim_scopes, dict):
            raise ValueError("Invalid per-scrim ban scopes.")
        for scrim_id, scope in scrim_scopes.items():
            if (
                not isinstance(scrim_id, str)
                or not scrim_id.strip()
                or len(scrim_id) > 100
                or scope not in {"guild", "scrim"}
            ):
                raise ValueError("Invalid per-scrim ban scope.")
        role_grants = config["punitive_role_grants"]
        if not isinstance(role_grants, dict):
            raise ValueError("Invalid punitive role grants.")
        for captain_id, role_ids in role_grants.items():
            if not isinstance(captain_id, str) or _GUILD_ID.fullmatch(captain_id) is None:
                raise ValueError("Invalid punitive role captain ID.")
            if not isinstance(role_ids, list) or not role_ids:
                raise ValueError("Invalid punitive role grant list.")
            seen_role_ids: set[int] = set()
            for role_id in role_ids:
                _validate_id(role_id, "granted punitive role ID")
                if role_id in seen_role_ids:
                    raise ValueError("Duplicate punitive role grant.")
                seen_role_ids.add(role_id)
        bans = config["bans"]
        if not isinstance(bans, list):
            raise ValueError("Invalid ban list.")
        seen_ids: set[str] = set()
        seen_identities: set[tuple[str, str, str, str | None]] = set()
        for ban in bans:
            required_ban = {
                "ban_id",
                "team_name",
                "tag",
                "captain_id",
                "created_by",
                "created_at",
                "expires_at",
                "is_permanent",
                "scrim_id",
                "scope",
                "reason",
            }
            if not isinstance(ban, dict) or set(ban) != required_ban:
                raise ValueError("Invalid ban record.")
            if (
                not isinstance(ban["ban_id"], str)
                or _BAN_ID.fullmatch(ban["ban_id"]) is None
                or ban["ban_id"] in seen_ids
            ):
                raise ValueError("Invalid or duplicate ban ID.")
            seen_ids.add(ban["ban_id"])
            team_name = ban["team_name"]
            tag = ban["tag"]
            if (
                not isinstance(team_name, str)
                or not team_name.strip()
                or len(team_name) > 100
                or any(character in team_name for character in ("\r", "\n"))
                or not isinstance(tag, str)
                or not tag.strip()
                or len(tag) > 32
                or any(character in tag for character in ("\r", "\n"))
            ):
                raise ValueError("Invalid banned team identity.")
            reason = ban["reason"]
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 350
            ):
                raise ValueError("Invalid ban reason.")
            if ban["scope"] not in {"guild", "scrim"}:
                raise ValueError("Invalid ban scope.")
            if not isinstance(ban["scrim_id"], str) or not ban["scrim_id"]:
                raise ValueError("Invalid ban scrim ID.")
            identity = (
                registration_match_key(team_name),
                registration_match_key(tag),
                ban["scope"],
                ban["scrim_id"] if ban["scope"] == "scrim" else None,
            )
            if identity in seen_identities:
                raise ValueError("Duplicate banned team identity.")
            seen_identities.add(identity)
            _validate_id(ban["captain_id"], "ban captain ID")
            _validate_id(ban["created_by"], "ban creator ID")
            _parse_timestamp(ban["created_at"], "ban creation time")
            if type(ban["is_permanent"]) is not bool:
                raise ValueError("Invalid permanent-ban status.")
            if ban["is_permanent"]:
                if ban["expires_at"] is not None:
                    raise ValueError("Permanent bans must not have an expiry time.")
            else:
                _parse_timestamp(ban["expires_at"], "ban expiry time")


def _migrate_v1_state(state: object) -> dict[str, Any]:
    """Preserve legacy bans as server-wide while adding the v2 permanence flag."""
    if not isinstance(state, dict) or type(state.get("version")) is not int:
        raise ValueError("Invalid legacy ban state.")
    if state["version"] != 1:
        raise ValueError("Unsupported legacy ban state version.")
    migrated = copy.deepcopy(state)
    guilds = migrated.get("guilds")
    if not isinstance(guilds, dict):
        raise ValueError("Invalid legacy ban guild map.")
    for config in guilds.values():
        if not isinstance(config, dict) or not isinstance(config.get("bans"), list):
            raise ValueError("Invalid legacy ban guild settings.")
        for ban in config["bans"]:
            if not isinstance(ban, dict) or "is_permanent" in ban:
                raise ValueError("Invalid legacy ban record.")
            ban["is_permanent"] = False
            ban.setdefault("scope", "guild")
    migrated["version"] = 2
    return _migrate_v2_state(migrated)


def _migrate_v2_state(state: object) -> dict[str, Any]:
    """Preserve v2 bans while adding scrim scopes, role tracking, and reasons."""
    if not isinstance(state, dict) or type(state.get("version")) is not int:
        raise ValueError("Invalid v2 ban state.")
    if state["version"] != 2:
        raise ValueError("Unsupported v2 ban state version.")
    migrated = copy.deepcopy(state)
    guilds = migrated.get("guilds")
    if not isinstance(guilds, dict):
        raise ValueError("Invalid v2 ban guild map.")
    for config in guilds.values():
        if not isinstance(config, dict) or not isinstance(config.get("bans"), list):
            raise ValueError("Invalid v2 ban guild settings.")
        config.setdefault("punitive_role_id", None)
        config.setdefault("punitive_role_grants", {})
        config.setdefault("scrim_scopes", {})
        for ban in config["bans"]:
            if not isinstance(ban, dict):
                raise ValueError("Invalid v2 ban record.")
            ban.setdefault("scope", "guild")
    migrated["version"] = 3
    return _migrate_v3_state(migrated)


def _migrate_v3_state(state: object) -> dict[str, Any]:
    """Add a required reason field without losing existing v3 ban records."""
    if not isinstance(state, dict) or type(state.get("version")) is not int:
        raise ValueError("Invalid v3 ban state.")
    if state["version"] != 3:
        raise ValueError("Unsupported v3 ban state version.")
    migrated = copy.deepcopy(state)
    guilds = migrated.get("guilds")
    if not isinstance(guilds, dict):
        raise ValueError("Invalid v3 ban guild map.")
    for config in guilds.values():
        if not isinstance(config, dict) or not isinstance(config.get("bans"), list):
            raise ValueError("Invalid v3 ban guild settings.")
        for ban in config["bans"]:
            if not isinstance(ban, dict):
                raise ValueError("Invalid v3 ban record.")
            ban.setdefault("reason", LEGACY_BAN_REASON)
    migrated["version"] = STATE_VERSION
    validate_ban_state(migrated)
    return migrated


class BanStore:
    """Versioned JSON storage with atomic replacement and strict validation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": STATE_VERSION, "guilds": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and type(payload.get("version")) is int:
                if payload["version"] == 1:
                    payload = _migrate_v1_state(payload)
                    self._save(payload)
                elif payload["version"] == 2:
                    payload = _migrate_v2_state(payload)
                    self._save(payload)
                elif payload["version"] == 3:
                    payload = _migrate_v3_state(payload)
                    self._save(payload)
                else:
                    validate_ban_state(payload)
            else:
                validate_ban_state(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise BanStorageError(
                f"Ban state at {self.path} is unreadable or invalid; it was not reset."
            ) from error
        return payload

    def _save(self, candidate: dict[str, Any]) -> None:
        try:
            validate_ban_state(candidate)
            content = json.dumps(
                candidate,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        except (TypeError, ValueError) as error:
            raise BanStorageError("Ban state is not valid JSON state.") from error

        temporary_path: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except OSError as error:
            raise BanStorageError(
                f"Could not atomically save ban state at {self.path}."
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _change_guild(self, guild_id: int, update):
        if type(guild_id) is not int or guild_id <= 0:
            raise ValueError("Invalid guild ID.")
        candidate = copy.deepcopy(self._state)
        key = str(guild_id)
        config = candidate["guilds"].setdefault(key, _default_guild_state())
        result = update(config)
        self._save(candidate)
        self._state = candidate
        return result

    def guild_ids(self) -> tuple[int, ...]:
        return tuple(int(guild_id) for guild_id in self._state["guilds"])

    def config(self, guild_id: int) -> dict[str, Any]:
        config = self._state["guilds"].get(str(guild_id))
        return copy.deepcopy(config if config is not None else _default_guild_state())

    def bans(self, guild_id: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self.config(guild_id)["bans"])

    def set_config(self, guild_id: int, key: str, value: object) -> None:
        if key not in {
            "bans_channel_id",
            "banned_emoji",
            "board_message_id",
            "board_page",
            "punitive_role_id",
            "punitive_role_grants",
        }:
            raise ValueError("Unsupported ban setting.")

        def update(config):
            config[key] = value

        self._change_guild(guild_id, update)

    def scope_for_scrim(self, guild_id: int, scrim_id: str) -> str:
        config = self._state["guilds"].get(str(guild_id))
        if config is None:
            return "guild"
        return config["scrim_scopes"].get(scrim_id, "guild")

    def set_scope_for_scrim(
        self, guild_id: int, scrim_id: str, scope: str
    ) -> None:
        if not isinstance(scrim_id, str) or not scrim_id.strip() or len(scrim_id) > 100:
            raise ValueError("Invalid scrim ID.")
        if scope not in {"guild", "scrim"}:
            raise ValueError("Invalid ban scope.")

        def update(config):
            config["scrim_scopes"][scrim_id] = scope

        self._change_guild(guild_id, update)

    def reset_settings(self, guild_id: int, scrim_id: str) -> None:
        """Restore ban configuration defaults without deleting bans or role grants."""
        if not isinstance(scrim_id, str) or not scrim_id.strip() or len(scrim_id) > 100:
            raise ValueError("Invalid scrim ID.")

        def update(config):
            config["bans_channel_id"] = None
            config["banned_emoji"] = DEFAULT_BANNED_EMOJI
            config["board_message_id"] = None
            config["board_page"] = 0
            config["punitive_role_id"] = None
            # An omitted scope uses the established server-wide default.
            config["scrim_scopes"].pop(scrim_id, None)

        self._change_guild(guild_id, update)

    def active_bans_for_captain(
        self,
        guild_id: int,
        captain_id: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return [
            copy.deepcopy(ban)
            for ban in self._state["guilds"].get(str(guild_id), {}).get("bans", [])
            if ban["captain_id"] == captain_id
            and (
                ban["is_permanent"]
                or _parse_timestamp(ban["expires_at"], "ban expiry time") > now
            )
        ]

    def captain_ids(self, guild_id: int) -> set[int]:
        config = self._state["guilds"].get(str(guild_id), _default_guild_state())
        return {ban["captain_id"] for ban in config["bans"]} | {
            int(captain_id) for captain_id in config["punitive_role_grants"]
        }

    def set_punitive_role_grants(
        self,
        guild_id: int,
        captain_id: int,
        role_ids: list[int],
    ) -> None:
        if type(captain_id) is not int or captain_id <= 0:
            raise ValueError("Invalid punitive role captain ID.")
        normalized = sorted(set(role_ids))
        for role_id in normalized:
            _validate_id(role_id, "granted punitive role ID")

        def update(config):
            key = str(captain_id)
            if normalized:
                config["punitive_role_grants"][key] = normalized
            else:
                config["punitive_role_grants"].pop(key, None)

        self._change_guild(guild_id, update)

    def add_ban(self, guild_id: int, ban: dict[str, Any]) -> dict[str, Any] | None:
        def update(config):
            identity = (
                registration_match_key(ban["team_name"]),
                registration_match_key(ban["tag"]),
                ban["scope"],
                ban["scrim_id"] if ban["scope"] == "scrim" else None,
            )
            previous = None
            retained = []
            for existing in config["bans"]:
                existing_identity = (
                    registration_match_key(existing["team_name"]),
                    registration_match_key(existing["tag"]),
                    existing["scope"],
                    existing["scrim_id"]
                    if existing["scope"] == "scrim"
                    else None,
                )
                if existing_identity == identity:
                    previous = existing
                else:
                    retained.append(existing)
            config["bans"] = retained + [copy.deepcopy(ban)]
            return copy.deepcopy(previous)

        return self._change_guild(guild_id, update)

    def remove_ban(self, guild_id: int, team_name: str, tag: str) -> list[dict[str, Any]]:
        identity = (registration_match_key(team_name), registration_match_key(tag))

        def update(config):
            removed = []
            retained = []
            for ban in config["bans"]:
                current = (
                    registration_match_key(ban["team_name"]),
                    registration_match_key(ban["tag"]),
                )
                (removed if current == identity else retained).append(ban)
            config["bans"] = retained
            return copy.deepcopy(removed)

        return self._change_guild(guild_id, update)

    def expire(self, now: datetime | None = None) -> dict[int, list[dict[str, Any]]]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidate = copy.deepcopy(self._state)
        expired: dict[int, list[dict[str, Any]]] = {}
        for guild_id, config in candidate["guilds"].items():
            retained = []
            for ban in config["bans"]:
                if (
                    not ban["is_permanent"]
                    and _parse_timestamp(ban["expires_at"], "ban expiry time") <= now
                ):
                    expired.setdefault(int(guild_id), []).append(ban)
                else:
                    retained.append(ban)
            config["bans"] = retained
        if expired:
            self._save(candidate)
            self._state = candidate
        return expired

    def match(
        self,
        guild_id: int,
        team_name: str,
        tag: str,
        now: datetime | None = None,
        *,
        scrim_id: str | None = None,
    ) -> BanMatch | None:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        name_key = registration_match_key(team_name)
        tag_key = registration_match_key(tag)
        active = [
            ban
            for ban in self._state["guilds"].get(str(guild_id), {}).get("bans", [])
            if ban["is_permanent"]
            or _parse_timestamp(ban["expires_at"], "ban expiry time") > now
            if ban["scope"] == "guild"
            or (scrim_id is not None and ban["scrim_id"] == scrim_id)
        ]
        exact = [
            ban
            for ban in active
            if registration_match_key(ban["team_name"]) == name_key
            and registration_match_key(ban["tag"]) == tag_key
        ]
        if exact:
            return BanMatch(
                kind="exact",
                ban_ids=tuple(ban["ban_id"] for ban in exact),
            )

        reasons: list[str] = []
        ban_ids: list[str] = []
        for ban in active:
            name_matches = registration_match_key(ban["team_name"]) == name_key
            tag_matches = registration_match_key(ban["tag"]) == tag_key
            if name_matches:
                reasons.append(f"Name matches banned team “{ban['team_name']}” (tag {ban['tag']}).")
                ban_ids.append(ban["ban_id"])
            if tag_matches:
                reasons.append(f"Tag matches banned team “{ban['team_name']}” (tag {ban['tag']}).")
                ban_ids.append(ban["ban_id"])
        if not reasons:
            return None
        return BanMatch(
            kind="partial",
            ban_ids=tuple(dict.fromkeys(ban_ids)),
            reasons=tuple(dict.fromkeys(reasons)),
        )