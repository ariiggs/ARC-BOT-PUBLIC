"""Staff commands and interactive controls for advanced team bans."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord

from arc_bot.storage.ban_storage import (
    DEFAULT_BANNED_EMOJI,
    BanMatch,
    BanStorageError,
    BanStore,
    registration_match_key,
)
from scrim_state import timezone_for_name


logger = logging.getLogger("pung-scrim-bot")
_CHANNEL_MENTION = re.compile(r"<#([1-9]\d{0,19})>\Z")
_PAGE_FOOTER = re.compile(r"Page (\d+)/(\d+)\Z")
_BOARD_PAGE_SIZE = 15
_BAN_VIEW_TIMEOUT = 180
_MAX_BAN_REASON_LENGTH = 350
class RegistrationBlockedByBan(RuntimeError):
    """Raised by registration when a current exact ban is found."""

    def __init__(self, match: BanMatch) -> None:
        super().__init__("This team is actively banned.")
        self.match = match


class StaleBanSelection(RuntimeError):
    """Raised when the selected scrim or slot changes during a ban flow."""


def parse_weeks_days(weeks_value: str, days_value: str) -> tuple[int, int]:
    try:
        weeks = int(weeks_value.strip())
        days = int(days_value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError("Weeks and Days must be whole numbers.") from error
    if not 0 <= weeks <= 52:
        raise ValueError("Weeks must be between 0 and 52.")
    if not 0 <= days <= 7:
        raise ValueError("Days must be between 0 and 7.")
    if weeks == 0 and days == 0:
        raise ValueError("The ban duration must be greater than zero.")
    return weeks, days


def parse_ban_duration_reply(value: str) -> tuple[bool, int, int]:
    """Return (is_permanent, weeks, days) for a duration form field."""
    normalized = value.strip().casefold()
    if normalized in {"perm", "permanent"}:
        return True, 0, 0
    parts = normalized.split()
    if len(parts) != 2:
        raise ValueError(
            "Enter two numbers as `<weeks> <days>`, or type `perm` for a permanent ban."
        )
    weeks, days = parse_weeks_days(*parts)
    return False, weeks, days


def validate_team_identity(team_name: str, tag: str) -> tuple[str, str]:
    team_name = team_name.strip() if isinstance(team_name, str) else ""
    tag = tag.strip() if isinstance(tag, str) else ""
    if (
        not team_name
        or len(team_name) > 100
        or any(character in team_name for character in ("\r", "\n"))
    ):
        raise ValueError("Team Name must be 1–100 characters on one line.")
    if (
        not tag
        or len(tag) > 32
        or any(character in tag for character in ("\r", "\n"))
    ):
        raise ValueError("TAG must be 1–32 characters on one line.")
    return team_name, tag


def validate_ban_reason(reason: str) -> str:
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > _MAX_BAN_REASON_LENGTH:
        raise ValueError(
            f"Reason is required and must be at most {_MAX_BAN_REASON_LENGTH} characters."
        )
    return reason


def ban_expiry_for_scrim(
    scrim: object,
    weeks: int,
    days: int,
    *,
    now: datetime | None = None,
) -> datetime:
    """Add calendar weeks/days in the scrim timezone and return an aware UTC time."""
    if not 0 <= weeks <= 52 or not 0 <= days <= 7 or (weeks == 0 and days == 0):
        raise ValueError("Choose a duration from 1 day to 52 weeks and 7 days.")
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("The current time must include a timezone.")
    local_now = now_utc.astimezone(timezone_for_name(scrim.timezone))
    local_expiry = local_now + timedelta(weeks=weeks, days=days)
    return local_expiry.astimezone(timezone.utc)


def _parse_team_tag_arguments(
    arguments: str,
    *,
    command: str,
) -> tuple[str, str]:
    usage = (
        f"Use `{command} Team Name / TAG` "
        f"(or the quoted form `{command} \"Team Name\" \"TAG\")."
    )
    try:
        parts = shlex.split(arguments)
    except ValueError as error:
        raise ValueError(usage) from error

    separators = [index for index, part in enumerate(parts) if part == "/"]
    if len(separators) == 1:
        separator = separators[0]
        if separator == 0 or separator == len(parts) - 1:
            raise ValueError(usage)
        team_name = " ".join(parts[:separator])
        tag = " ".join(parts[separator + 1 :])
    elif not separators and len(parts) == 2:
        # Keep accepting the original quoted two-argument format.
        team_name, tag = parts
    else:
        raise ValueError(usage)

    return validate_team_identity(team_name, tag)


def parse_manual_ban_arguments(arguments: str) -> tuple[str, str]:
    """Parse `!ban Team Name / TAG` and the legacy quoted identity format."""
    return _parse_team_tag_arguments(arguments, command="!ban")


def parse_unban_arguments(arguments: str) -> tuple[str, str]:
    """Parse `!unban Team Name / TAG` and the legacy quoted identity format."""
    return _parse_team_tag_arguments(arguments, command="!unban")


def _escape(value: object) -> str:
    text = str(value)
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def _slot_primary_captain(slot: object) -> int | None:
    return getattr(slot, "captain_1_id", None) or getattr(slot, "manager_id", None)


class BanService:
    """Coordinates ban persistence, registration filtering, purge, and the board."""

    def __init__(self, bot: object, owner: object, state_path: str | Path) -> None:
        self.bot = bot
        self.owner = owner
        self.state_path = Path(state_path)
        self.store: BanStore | None = None
        self.storage_error: BanStorageError | None = None
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._board_locks: dict[int, asyncio.Lock] = {}
        self._expiry_task: asyncio.Task | None = None
        try:
            self.store = BanStore(self.state_path)
        except BanStorageError as error:
            self.storage_error = error
            logger.exception("Advanced Ban System storage could not be loaded.")

    def _dep(self, name: str):
        return getattr(self.owner, name)

    def _require_store(self) -> BanStore:
        if self.store is None:
            raise BanStorageError(
                "Ban data is unavailable; registrations cannot be checked safely."
            ) from self.storage_error
        return self.store

    def guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    def _board_lock(self, guild_id: int) -> asyncio.Lock:
        return self._board_locks.setdefault(guild_id, asyncio.Lock())

    def match(
        self,
        guild_id: int,
        team_name: str,
        tag: str,
        *,
        scrim_id: str | None = None,
    ) -> BanMatch | None:
        return self._require_store().match(
            guild_id, team_name, tag, scrim_id=scrim_id
        )

    def scope_for_scrim(self, guild_id: int, scrim_id: str) -> str:
        return self._require_store().scope_for_scrim(guild_id, scrim_id)

    async def configure_scope_for_scrim(
        self, guild_id: int, scrim_id: str, scope: str
    ) -> None:
        async with self.guild_lock(guild_id):
            self._require_store().set_scope_for_scrim(guild_id, scrim_id, scope)

    async def configure_punitive_role(
        self, guild_id: int, role_id: int | None
    ) -> list[int]:
        if role_id is not None and (type(role_id) is not int or role_id <= 0):
            raise ValueError("Invalid punitive role.")
        store = self._require_store()
        failed_captains: list[int] = []
        async with self.guild_lock(guild_id):
            store.set_config(guild_id, "punitive_role_id", role_id)
            for captain_id in store.captain_ids(guild_id):
                if not await self._sync_punitive_role_for_captain(
                    guild_id, captain_id
                ):
                    failed_captains.append(captain_id)
        return failed_captains

    async def reset_settings_for_scrim(
        self, guild_id: int, scrim_id: str
    ) -> tuple[bool, list[int]]:
        """Reset guild ban controls and this scrim's scope, retaining ban records."""
        store = self._require_store()
        async with self.guild_lock(guild_id):
            if not await self.retire_board(guild_id):
                return False, []
            store.reset_settings(guild_id, scrim_id)
            failed_captains = [
                captain_id
                for captain_id in store.captain_ids(guild_id)
                if not await self._sync_punitive_role_for_captain(
                    guild_id, captain_id
                )
            ]
        return True, failed_captains

    async def _sync_punitive_role_for_captain(
        self, guild_id: int, captain_id: int
    ) -> bool:
        """Keep the configured role while any team ban for this captain is active."""
        store = self._require_store()
        config = store.config(guild_id)
        has_active_bans = bool(store.active_bans_for_captain(guild_id, captain_id))
        target_role_id = config["punitive_role_id"] if has_active_bans else None
        captain_key = str(captain_id)
        granted_role_ids = set(config["punitive_role_grants"].get(captain_key, []))
        if target_role_id is None and not granted_role_ids:
            return True

        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(guild_id) if get_guild is not None else None
        if guild is None:
            return False

        member = guild.get_member(captain_id)
        if member is None:
            fetch_member = getattr(guild, "fetch_member", None)
            if fetch_member is not None:
                try:
                    member = await fetch_member(captain_id)
                except discord.NotFound:
                    if not has_active_bans:
                        store.set_punitive_role_grants(guild_id, captain_id, [])
                        return True
                    return False
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Could not fetch banned captain %s in guild %s.",
                        captain_id,
                        guild_id,
                    )
                    return False
        if member is None:
            if not has_active_bans:
                store.set_punitive_role_grants(guild_id, captain_id, [])
                return True
            return False

        required_role_ids = granted_role_ids | (
            {target_role_id} if target_role_id is not None else set()
        )
        role_map = {
            role_id: guild.get_role(role_id)
            for role_id in required_role_ids
        }
        if any(role is None for role in role_map.values()):
            fetch_roles = getattr(guild, "fetch_roles", None)
            if fetch_roles is not None:
                try:
                    fetched_roles = await fetch_roles()
                    role_map.update(
                        {
                            role_id: role
                            for role_id in required_role_ids
                            for role in fetched_roles
                            if getattr(role, "id", None) == role_id
                        }
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Could not fetch punitive roles in guild %s.", guild_id
                    )

        bot_member = getattr(guild, "me", None)
        permissions = getattr(bot_member, "guild_permissions", None)
        top_role = getattr(bot_member, "top_role", None)
        if target_role_id is not None:
            target_role = role_map.get(target_role_id)
            if target_role is None:
                return False
            if (
                getattr(target_role, "managed", False)
                or getattr(target_role, "is_default", lambda: False)()
                or (
                    permissions is not None
                    and not getattr(permissions, "manage_roles", False)
                )
                or (
                    top_role is not None
                    and getattr(target_role, "position", 0)
                    >= getattr(top_role, "position", 0)
                )
            ):
                return False

        member_role_ids = {
            getattr(role, "id", None) for role in getattr(member, "roles", ())
        }
        updated_grants = set(granted_role_ids)
        all_ok = True
        for role_id in tuple(granted_role_ids):
            if role_id == target_role_id:
                continue
            role = role_map.get(role_id)
            if role is None or role_id not in member_role_ids:
                updated_grants.discard(role_id)
                continue
            if (
                permissions is not None
                and not getattr(permissions, "manage_roles", False)
            ) or (
                top_role is not None
                and getattr(role, "position", 0) >= getattr(top_role, "position", 0)
            ):
                all_ok = False
                continue
            try:
                await member.remove_roles(role, reason="Punitive role no longer applies")
                member_role_ids.discard(role_id)
                updated_grants.discard(role_id)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not remove obsolete punitive role %s from captain %s.",
                    role_id,
                    captain_id,
                )
                all_ok = False

        if target_role_id is not None and target_role_id not in member_role_ids:
            target_role = role_map[target_role_id]
            try:
                await member.add_roles(target_role, reason="Active team ban")
                member_role_ids.add(target_role_id)
                updated_grants.add(target_role_id)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not apply punitive role %s to captain %s.",
                    target_role_id,
                    captain_id,
                )
                all_ok = False

        store.set_punitive_role_grants(
            guild_id, captain_id, sorted(updated_grants)
        )
        return all_ok

    def banned_emoji(self, guild_id: int, guild: object | None = None) -> str:
        store = self._require_store()
        configured = store.config(guild_id)["banned_emoji"]
        if guild is not None and not self._dep("emoji_is_available_in_guild")(
            configured, guild
        ):
            return DEFAULT_BANNED_EMOJI
        return configured

    async def add_banned_reaction(self, message: object, guild_id: int) -> bool:
        add_reaction = getattr(message, "add_reaction", None)
        if add_reaction is None:
            return False
        try:
            guild = getattr(message, "guild", None)
            await add_reaction(self.banned_emoji(guild_id, guild))
            return True
        except (BanStorageError, discord.HTTPException, AttributeError):
            logger.exception("Could not add the blocked-registration reaction.")
            return False

    async def start(self) -> None:
        if self.store is None:
            return
        for guild_id in self.store.guild_ids():
            config = self.store.config(guild_id)
            if config["board_message_id"] is None:
                continue
            try:
                self.bot.add_view(
                    self._make_board_view(guild_id, config["board_page"]),
                    message_id=config["board_message_id"],
                )
            except (AttributeError, ValueError):
                logger.exception(
                    "Could not restore the ban board controls for guild %s.",
                    guild_id,
                )
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self._expiry_loop())

    async def stop(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            self._expiry_task = None

    async def _expiry_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
            await self._reconcile_all_punitive_roles()
            while not self.bot.is_closed():
                try:
                    await self.expire_bans()
                except (BanStorageError, discord.HTTPException):
                    logger.exception("Advanced Ban System expiry sweep failed.")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    async def expire_bans(
        self, now: datetime | None = None
    ) -> dict[int, list[dict[str, Any]]]:
        store = self._require_store()
        expired = store.expire(now)
        for guild_id, removed_bans in expired.items():
            async with self.guild_lock(guild_id):
                for captain_id in {ban["captain_id"] for ban in removed_bans}:
                    if not await self._sync_punitive_role_for_captain(
                        guild_id, captain_id
                    ):
                        logger.warning(
                            "Could not reconcile punitive role for captain %s in guild %s after ban expiry.",
                            captain_id,
                            guild_id,
                        )
                if not await self.refresh_board(guild_id):
                    logger.warning(
                        "Expired bans for guild %s, but its board could not be refreshed.",
                        guild_id,
                    )
        return expired

    async def _reconcile_all_punitive_roles(self) -> None:
        store = self._require_store()
        for guild_id in store.guild_ids():
            async with self.guild_lock(guild_id):
                for captain_id in store.captain_ids(guild_id):
                    if not await self._sync_punitive_role_for_captain(
                        guild_id, captain_id
                    ):
                        logger.warning(
                            "Could not restore punitive role state for captain %s in guild %s.",
                            captain_id,
                            guild_id,
                        )

    def active_scrims(self, guild_id: int) -> list[object]:
        return sorted(
            (
                scrim
                for scrim in self._dep("repository").list(guild_id)
                if getattr(scrim, "is_open", False) is True
                and self._dep("is_active")(scrim)
            ),
            key=lambda scrim: (
                (getattr(scrim, "name", None) or "").casefold(),
                scrim.id,
            ),
        )

    def known_staff_channel_ids(self, guild_id: int) -> set[int]:
        return {
            scrim.staff_channel_id
            for scrim in self._dep("repository").list(guild_id)
            if scrim.staff_channel_id is not None
        }

    def occupied_slots(self, scrim: object) -> list[object]:
        return sorted(
            (
                slot
                for slot in scrim.slots.values()
                if slot.status != self._dep("STATUS_AVAILABLE")
            ),
            key=lambda slot: slot.number,
        )

    async def _resolve_channel(self, guild_id: int, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        guild = getattr(channel, "guild", None)
        if guild is None or guild.id != guild_id:
            raise ValueError("The configured bans channel is not in this server.")
        return channel

    def _active_bans(self, guild_id: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        active = []
        for ban in self._require_store().bans(guild_id):
            if ban["is_permanent"]:
                active.append(ban)
                continue
            expiry = datetime.fromisoformat(
                ban["expires_at"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if expiry > now:
                active.append(ban)
        return sorted(
            active,
            key=lambda ban: (
                not ban["is_permanent"],
                (
                    -datetime.fromisoformat(
                        ban["expires_at"].replace("Z", "+00:00")
                    ).astimezone(timezone.utc).timestamp()
                    if not ban["is_permanent"]
                    else 0
                ),
                registration_match_key(ban["team_name"]),
                registration_match_key(ban["tag"]),
            ),
        )

    def _board_embed(self, guild_id: int, page: int) -> tuple[discord.Embed, int]:
        bans = self._active_bans(guild_id)
        page_count = max(1, math.ceil(len(bans) / _BOARD_PAGE_SIZE))
        page = max(0, min(page, page_count - 1))
        embed = discord.Embed(
            title="Active Team Bans",
            description=(
                "Teams listed here cannot register until a temporary ban expires "
                "or staff removes a permanent ban."
                if bans
                else "There are no active team bans."
            ),
            color=discord.Color.dark_red(),
        )
        for ban in bans[page * _BOARD_PAGE_SIZE:(page + 1) * _BOARD_PAGE_SIZE]:
            captain = f"<@{ban['captain_id']}>"
            duration_display = (
                "🚨 **PERMANENT**"
                if ban["is_permanent"]
                else "Expires <t:"
                f"{int(datetime.fromisoformat(ban['expires_at'].replace('Z', '+00:00')).astimezone(timezone.utc).timestamp())}:R>"
            )
            if ban["scope"] == "guild":
                scope_display = "Server-wide"
            else:
                selected_scrim = self._dep("repository").get(ban["scrim_id"])
                scope_name = (
                    getattr(selected_scrim, "name", None) or ban["scrim_id"]
                )
                scope_display = f"Only {_escape(scope_name)}"
            embed.add_field(
                name=f"{_escape(ban['team_name'])} · {_escape(ban['tag'])}"[:256],
                value=(
                    f"Primary Captain: {captain}\n"
                    f"Scope: {scope_display}\n"
                    f"{duration_display}\n"
                    f"Reason: {_escape(ban['reason'])}"
                )[:1024],
                inline=False,
            )
        embed.set_footer(text=f"Page {page + 1}/{page_count}")
        return embed, page_count

    def _make_board_view(
        self,
        guild_id: int,
        page: int,
    ) -> "BanBoardView":
        _, page_count = self._board_embed(guild_id, page)
        return BanBoardView(self, guild_id, page, page_count)

    async def refresh_board(self, guild_id: int) -> bool:
        store = self._require_store()
        config = store.config(guild_id)
        channel_id = config["bans_channel_id"]
        if channel_id is None:
            return False
        async with self._board_lock(guild_id):
            try:
                channel = await self._resolve_channel(guild_id, channel_id)
                message = None
                if config["board_message_id"] is not None:
                    try:
                        message = await channel.fetch_message(
                            config["board_message_id"]
                        )
                    except discord.NotFound:
                        store.set_config(guild_id, "board_message_id", None)
                store.set_config(guild_id, "board_page", 0)
                embed, _ = self._board_embed(guild_id, 0)
                view = self._make_board_view(guild_id, 0)
                if message is None:
                    message = await channel.send(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    try:
                        store.set_config(
                            guild_id, "board_message_id", message.id
                        )
                    except BanStorageError:
                        try:
                            await message.delete()
                        except discord.HTTPException:
                            pass
                        raise
                    self.bot.add_view(view, message_id=message.id)
                else:
                    await message.edit(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return True
            except (
                BanStorageError,
                discord.Forbidden,
                discord.HTTPException,
                ValueError,
                AttributeError,
            ):
                logger.exception(
                    "Could not refresh the public ban board for guild %s.",
                    guild_id,
                )
                return False

    async def change_board_page(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        direction: int,
    ) -> None:
        try:
            store = self._require_store()
            config = store.config(guild_id)
            if (
                interaction.guild_id != guild_id
                or interaction.message is None
                or interaction.message.id != config["board_message_id"]
            ):
                await interaction.response.send_message(
                    "This ban board is no longer active.", ephemeral=True
                )
                return
            current_page = 0
            if interaction.message.embeds:
                footer = interaction.message.embeds[0].footer.text or ""
                match = _PAGE_FOOTER.fullmatch(footer)
                if match:
                    current_page = int(match.group(1)) - 1
            embed, page_count = self._board_embed(
                guild_id, current_page + direction
            )
            next_page = int((embed.footer.text or "Page 1/1").split("/")[0][5:]) - 1
            async with self.guild_lock(guild_id):
                store.set_config(guild_id, "board_page", next_page)
            view = BanBoardView(self, guild_id, next_page, page_count)
            await interaction.response.edit_message(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (BanStorageError, discord.HTTPException):
            logger.exception("Could not change a ban board page.")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The ban board could not be updated. Please try again.",
                    ephemeral=True,
                )

    async def _purge_active_scrims(
        self,
        guild_id: int,
        team_name: str,
        tag: str,
        *,
        scope: str = "guild",
        scrim_id: str | None = None,
    ) -> list[str]:
        failures: list[str] = []
        name_key = registration_match_key(team_name)
        tag_key = registration_match_key(tag)
        for scrim in self.active_scrims(guild_id):
            if scope == "scrim" and scrim.id != scrim_id:
                continue
            removed_slots = []
            removed_requests = []
            async with scrim.state_lock:
                slot_matches = [
                    slot
                    for slot in scrim.slots.values()
                    if slot.status != self._dep("STATUS_AVAILABLE")
                    and registration_match_key(slot.team_name) == name_key
                    and registration_match_key(slot.tag) == tag_key
                ]
                request_matches = [
                    request
                    for request in scrim.pending_registrations.values()
                    if registration_match_key(request.team_name) == name_key
                    and registration_match_key(request.tag) == tag_key
                ]
                if slot_matches or request_matches:
                    with self._dep("repository").transaction():
                        removed_slots = [slot.snapshot() for slot in slot_matches]
                        for slot in slot_matches:
                            slot.clear()
                        removed_requests = list(request_matches)
                        for request in request_matches:
                            scrim.pending_registrations.pop(request.request_id, None)
            for captain_id in {
                captain_id
                for slot in removed_slots
                for captain_id in (slot.captain_1_id, slot.captain_2_id)
                if captain_id is not None
            }:
                try:
                    if not await self._dep("revoke_manager_access_if_unused")(
                        scrim, captain_id
                    ):
                        failures.append(
                            f"{scrim.name or scrim.id}: captain access for <@{captain_id}>"
                        )
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    logger.exception(
                        "Could not revoke captain access after a ban purge."
                    )
                    failures.append(
                        f"{scrim.name or scrim.id}: captain access for <@{captain_id}>"
                    )
            for request in removed_requests:
                try:
                    if not await self._dep("update_registration_reaction")(
                        scrim, request, approved=False, banned=True
                    ):
                        failures.append(
                            f"{scrim.name or scrim.id}: reaction for slot {request.slot_number:02d}"
                        )
                except (discord.HTTPException, AttributeError):
                    logger.exception("Could not update a purged registration reaction.")
                    failures.append(
                        f"{scrim.name or scrim.id}: reaction for slot {request.slot_number:02d}"
                    )
            if removed_slots and not await self._dep("refresh_public_slots")(scrim):
                failures.append(f"{scrim.name or scrim.id}: slot board")
            if removed_requests and not await self._dep("refresh_registration_queue")(
                scrim
            ):
                failures.append(f"{scrim.name or scrim.id}: registration review queue")
            if removed_slots or removed_requests:
                summary = ", ".join(
                    f"slot {slot.number:02d} · {slot.team_name}/{slot.tag}"
                    for slot in removed_slots
                )
                if removed_requests:
                    request_summary = ", ".join(
                        f"pending slot {request.slot_number:02d} · "
                        f"{request.team_name}/{request.tag}"
                        for request in removed_requests
                    )
                    summary = " · ".join(part for part in (summary, request_summary) if part)
                try:
                    await self._dep("send_scrim_log")(
                        scrim, "TEAM BAN PURGE", summary
                    )
                except (discord.HTTPException, AttributeError):
                    logger.exception("Could not log a ban purge.")
                    failures.append(f"{scrim.name or scrim.id}: audit log")
        return failures

    async def add_ban(
        self,
        guild_id: int,
        *,
        team_name: str,
        tag: str,
        reason: str,
        captain_id: int,
        created_by: int,
        scrim: object,
        expires_at: datetime | None,
        is_permanent: bool = False,
        expected_selection: tuple[int, int, int, str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, list[str], bool]:
        store = self._require_store()
        team_name, tag = validate_team_identity(team_name, tag)
        reason = validate_ban_reason(reason)
        if type(is_permanent) is not bool or (not is_permanent and expires_at is None):
            raise ValueError("Temporary bans require an expiry time.")
        if is_permanent and expires_at is not None:
            raise ValueError("Permanent bans must not have an expiry time.")
        record = {
            "ban_id": secrets.token_hex(8),
            "team_name": team_name,
            "tag": tag,
            "reason": reason,
            "captain_id": captain_id,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (
                None
                if is_permanent
                else expires_at.astimezone(timezone.utc).isoformat()
            ),
            "is_permanent": is_permanent,
            "scrim_id": scrim.id,
            "scope": store.scope_for_scrim(guild_id, scrim.id),
        }
        async with self.guild_lock(guild_id):
            if expected_selection is not None:
                (
                    selected_scrim_id,
                    expected_assignment_id,
                    slot_number,
                    expected_name,
                    expected_tag,
                ) = expected_selection
                selected_scrim = self._dep("repository").get(selected_scrim_id)
                if selected_scrim is None or not self._dep("is_active")(selected_scrim):
                    raise StaleBanSelection
                async with selected_scrim.state_lock:
                    slot = selected_scrim.slots.get(slot_number)
                    if (
                        not getattr(selected_scrim, "is_open", False)
                        or slot is None
                        or slot.status == self._dep("STATUS_AVAILABLE")
                        or slot.assignment_id != expected_assignment_id
                    ):
                        raise StaleBanSelection
                    if (
                        registration_match_key(slot.team_name)
                        != registration_match_key(expected_name)
                        or registration_match_key(slot.tag)
                        != registration_match_key(expected_tag)
                        or slot.team_name != team_name
                        or slot.tag != tag
                        or _slot_primary_captain(slot) != captain_id
                    ):
                        raise StaleBanSelection
                    previous = store.add_ban(guild_id, record)
            else:
                current_scrim = self._dep("repository").get(scrim.id)
                if (
                    current_scrim is None
                    or current_scrim.guild_id != guild_id
                    or not getattr(current_scrim, "is_open", False)
                    or not self._dep("is_active")(current_scrim)
                ):
                    raise StaleBanSelection
                previous = store.add_ban(guild_id, record)
            role_failures: list[str] = []
            affected_captains = {captain_id}
            if previous is not None:
                affected_captains.add(previous["captain_id"])
            for affected_captain_id in affected_captains:
                if not await self._sync_punitive_role_for_captain(
                    guild_id, affected_captain_id
                ):
                    role_failures.append(
                        f"punitive role for captain <@{affected_captain_id}>"
                    )
            try:
                failures = await self._purge_active_scrims(
                    guild_id,
                    team_name,
                    tag,
                    scope=record["scope"],
                    scrim_id=scrim.id,
                )
            except Exception:
                logger.exception(
                    "The ban for %s/%s was saved, but scrim cleanup did not finish.",
                    team_name,
                    tag,
                )
                failures = ["scrim cleanup (check logs and active scrims)"]
            failures.extend(role_failures)
            try:
                board_ok = await self.refresh_board(guild_id)
            except Exception:
                logger.exception(
                    "The ban for %s/%s was saved, but its board refresh failed.",
                    team_name,
                    tag,
                )
                board_ok = False
        return previous, failures, board_ok

    async def apply_form_ban(
        self,
        interaction: discord.Interaction,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        team_name: str,
        tag: str,
        captain_id: int,
        duration: str,
        reason: str,
        expected_selection: tuple[int, int, int, str, str] | None = None,
    ) -> None:
        """Validate a submitted form, then persist and apply the ban."""

        async def respond(content: str) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        try:
            team_name, tag = validate_team_identity(team_name, tag)
            reason = validate_ban_reason(reason)
            is_permanent, weeks, days = parse_ban_duration_reply(duration)
        except ValueError as error:
            await respond(str(error))
            return

        if (
            interaction.user.id != owner_id
            or interaction.guild_id != guild_id
            or not self._dep("member_has_global_staff_role")(
                interaction.user, guild_id
            )
        ):
            await respond("Ban cancelled because your Staff role is no longer active.")
            return

        scrim = self._dep("repository").get(scrim_id)
        if (
            scrim is None
            or scrim.guild_id != guild_id
            or not getattr(scrim, "is_open", False)
            or not self._dep("is_active")(scrim)
            or interaction.channel_id != scrim.staff_channel_id
        ):
            await respond(
                "The selected scrim is no longer open in this staff channel. "
                "No changes were made; run `!ban` again."
            )
            return

        try:
            expires_at = (
                None
                if is_permanent
                else ban_expiry_for_scrim(scrim, weeks, days)
            )
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            previous, failures, board_ok = await self.add_ban(
                guild_id,
                team_name=team_name,
                tag=tag,
                reason=reason,
                captain_id=captain_id,
                created_by=owner_id,
                scrim=scrim,
                expires_at=expires_at,
                is_permanent=is_permanent,
                expected_selection=expected_selection,
            )
            duration_text = (
                "🚨 **PERMANENT**"
                if is_permanent
                else f"Expires <t:{int(expires_at.timestamp())}:F>."
            )
            result = (
                f"✅ {'Updated' if previous else 'Added'} the ban for "
                f"**{_escape(team_name)} / {_escape(tag)}**. "
                f"Primary Captain: <@{captain_id}>. {duration_text}\n"
                f"Reason: {_escape(reason)}"
            )
            if failures:
                result += " ⚠️ Some slot, role, reaction, or queue updates failed: "
                result += "; ".join(failures[:5]) + "."
            if not board_ok:
                result += " ⚠️ The public ban board could not be refreshed."
            await interaction.followup.send(
                result,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except StaleBanSelection:
            await respond(
                "The selected scrim or slot changed before the ban was submitted. "
                "Nothing was banned; run `!ban` again."
            )
        except (BanStorageError, ValueError):
            logger.exception("Could not apply the requested team ban.")
            await respond(
                "The ban could not be saved safely. No success was reported; contact staff."
            )
        except Exception:
            logger.exception("Ban form processing did not complete cleanly.")
            await respond(
                "The ban may have been saved, but cleanup did not finish. "
                "Check the ban board and active scrims."
            )

    async def remove_ban(
        self,
        guild_id: int,
        team_name: str,
        tag: str,
    ) -> tuple[list[dict[str, Any]], bool, list[int]]:
        async with self.guild_lock(guild_id):
            removed = self._require_store().remove_ban(guild_id, team_name, tag)
            failed_captains = []
            for captain_id in {
                ban["captain_id"] for ban in removed
            }:
                if not await self._sync_punitive_role_for_captain(
                    guild_id, captain_id
                ):
                    failed_captains.append(captain_id)
            board_ok = await self.refresh_board(guild_id)
        return removed, board_ok, failed_captains

    async def configure_board_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> tuple[bool, bool]:
        store = self._require_store()
        old = store.config(guild_id)
        if old["bans_channel_id"] == channel_id:
            async with self.guild_lock(guild_id):
                return await self.refresh_board(guild_id), True
        old_board_retired = True
        if (
            old["bans_channel_id"] is not None
            and old["board_message_id"] is not None
            and old["bans_channel_id"] != channel_id
        ):
            try:
                old_channel = await self._resolve_channel(
                    guild_id, old["bans_channel_id"]
                )
                old_message = await old_channel.fetch_message(old["board_message_id"])
                await old_message.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException, ValueError):
                old_board_retired = False
                logger.exception("Could not retire the previous ban board.")
        async with self.guild_lock(guild_id):
            store.set_config(guild_id, "bans_channel_id", channel_id)
            store.set_config(guild_id, "board_message_id", None)
            store.set_config(guild_id, "board_page", 0)
            refreshed = await self.refresh_board(guild_id)
        return refreshed, old_board_retired

    async def retire_board(self, guild_id: int) -> bool:
        store = self._require_store()
        config = store.config(guild_id)
        if config["bans_channel_id"] is None or config["board_message_id"] is None:
            return True
        try:
            channel = await self._resolve_channel(
                guild_id, config["bans_channel_id"]
            )
            message = await channel.fetch_message(config["board_message_id"])
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException, ValueError):
            logger.exception("Could not remove the previous public ban board.")
            return False

    async def change_board_emoji(self, guild_id: int, emoji: str) -> bool:
        async with self.guild_lock(guild_id):
            self._require_store().set_config(guild_id, "banned_emoji", emoji)
            return await self.refresh_board(guild_id)


class BanBoardView(discord.ui.View):
    """Public, persistent page controls for the guild's ban board."""

    def __init__(
        self,
        service: BanService,
        guild_id: int,
        page: int,
        page_count: int,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.guild_id = guild_id
        previous = discord.ui.Button(
            label="Previous",
            emoji="◀️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"arc:banboard:{guild_id}:prev",
            disabled=page_count <= 1 or page <= 0,
        )
        next_page = discord.ui.Button(
            label="Next",
            emoji="▶️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"arc:banboard:{guild_id}:next",
            disabled=page_count <= 1 or page >= page_count - 1,
        )
        previous.callback = self.previous
        next_page.callback = self.next
        self.add_item(previous)
        self.add_item(next_page)

    async def previous(self, interaction: discord.Interaction) -> None:
        await self.service.change_board_page(interaction, self.guild_id, -1)

    async def next(self, interaction: discord.Interaction) -> None:
        await self.service.change_board_page(interaction, self.guild_id, 1)


class _OwnerView(discord.ui.View):
    def __init__(self, service: BanService, owner_id: int) -> None:
        super().__init__(timeout=_BAN_VIEW_TIMEOUT)
        self.service = service
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This selection belongs to another staff member.",
                ephemeral=True,
            )
            return False
        expected_guild_id = getattr(self, "guild_id", None)
        if (
            interaction.guild is None
            or (
                expected_guild_id is not None
                and interaction.guild.id != expected_guild_id
            )
            or not self.service._dep("member_has_global_staff_role")(
                interaction.user, interaction.guild.id
            )
        ):
            await interaction.response.send_message(
                "You need the configured server-wide Staff role to use this control.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


async def _delete_public_launcher(interaction: discord.Interaction) -> None:
    message = interaction.message
    if message is None:
        return
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class BanLaunchView(_OwnerView):
    """Short-lived prefix-command launcher for private ban interactions."""

    def __init__(
        self,
        service: BanService,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        team_name: str = "",
        tag: str = "",
        allow_live: bool = True,
    ) -> None:
        super().__init__(service, owner_id)
        self.guild_id = guild_id
        self.scrim_id = scrim_id
        self.team_name = team_name
        self.tag = tag
        manual = discord.ui.Button(
            label="Manual Ban",
            style=discord.ButtonStyle.danger,
        )
        manual.callback = self.open_manual_form
        self.add_item(manual)
        if allow_live:
            live = discord.ui.Button(
                label="Live Ban",
                style=discord.ButtonStyle.secondary,
            )
            live.callback = self.open_live_flow
            self.add_item(live)

    async def open_manual_form(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ManualBanFormModal(
                self.service,
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                team_name=self.team_name,
                tag=self.tag,
            )
        )
        await _delete_public_launcher(interaction)

    async def open_live_flow(self, interaction: discord.Interaction) -> None:
        scrim = self.service._dep("repository").get(self.scrim_id)
        if (
            scrim is None
            or scrim.guild_id != self.guild_id
            or not getattr(scrim, "is_open", False)
            or not self.service._dep("is_active")(scrim)
            or interaction.channel_id != scrim.staff_channel_id
        ):
            await interaction.response.send_message(
                "That scrim is no longer open in this staff channel. Run `!ban` again.",
                ephemeral=True,
            )
            await _delete_public_launcher(interaction)
            return
        if not self.service.occupied_slots(scrim):
            await interaction.response.send_message(
                "That scrim no longer has any occupied teams.",
                ephemeral=True,
            )
            await _delete_public_launcher(interaction)
            return

        view = BanTeamSelectView(
            self.service,
            self.owner_id,
            scrim,
            guild=interaction.guild,
        )
        await interaction.response.send_message(
            content=view.page_text(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass
        await _delete_public_launcher(interaction)


class ManualBanFormModal(discord.ui.Modal):
    """Private form for a manual team identity, duration, and reason."""

    def __init__(
        self,
        service: BanService,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        team_name: str = "",
        tag: str = "",
    ) -> None:
        super().__init__(title="Manual Team Ban", timeout=_BAN_VIEW_TIMEOUT)
        self.service = service
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.scrim_id = scrim_id
        self.team_name_input = discord.ui.TextInput(
            label="Team Name",
            default=team_name[:100] or None,
            max_length=100,
            required=True,
        )
        self.tag_input = discord.ui.TextInput(
            label="Team TAG",
            default=tag[:32] or None,
            max_length=32,
            required=True,
        )
        self.duration_input = discord.ui.TextInput(
            label="Duration",
            placeholder="weeks days (for example: 2 3), or perm",
            max_length=32,
            required=True,
        )
        self.reason_input = discord.ui.TextInput(
            label="Reason (required)",
            style=discord.TextStyle.paragraph,
            placeholder="Enter the reason that will appear on the bans board.",
            max_length=_MAX_BAN_REASON_LENGTH,
            required=True,
        )
        self.add_item(self.team_name_input)
        self.add_item(self.tag_input)
        self.add_item(self.duration_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async def reject(content: str) -> None:
            await interaction.response.send_message(
                content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        if (
            interaction.user.id != self.owner_id
            or interaction.guild_id != self.guild_id
            or not self.service._dep("member_has_global_staff_role")(
                interaction.user, self.guild_id
            )
        ):
            await reject("Ban cancelled because your Staff role is no longer active.")
            return
        try:
            team_name, tag = validate_team_identity(
                self.team_name_input.value,
                self.tag_input.value,
            )
            parse_ban_duration_reply(self.duration_input.value)
            reason = validate_ban_reason(self.reason_input.value)
        except ValueError as error:
            await reject(str(error))
            return

        scrim = self.service._dep("repository").get(self.scrim_id)
        if (
            scrim is None
            or scrim.guild_id != self.guild_id
            or not getattr(scrim, "is_open", False)
            or not self.service._dep("is_active")(scrim)
            or interaction.channel_id != scrim.staff_channel_id
        ):
            await reject(
                "That scrim is no longer open in this staff channel. "
                "No changes were made; run `!ban` again."
            )
            return

        view = ManualBanCaptainSelectView(
            self.service,
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            scrim_id=self.scrim_id,
            team_name=team_name,
            tag=tag,
            duration=self.duration_input.value,
            reason=reason,
        )
        await interaction.response.send_message(
            content=(
                f"Select the current Captain for "
                f"**{_escape(team_name)} / {_escape(tag)}**."
            ),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


class LiveBanDurationModal(discord.ui.Modal):
    """Duration and required reason for the current occupied slot."""

    def __init__(
        self,
        service: BanService,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        team_name: str,
        tag: str,
        captain_id: int,
        expected_selection: tuple[int, int, int, str, str],
    ) -> None:
        title = f"Live Ban · {team_name}"[:45]
        super().__init__(title=title, timeout=_BAN_VIEW_TIMEOUT)
        self.service = service
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.scrim_id = scrim_id
        self.team_name = team_name
        self.tag = tag
        self.captain_id = captain_id
        self.expected_selection = expected_selection
        self.duration_input = discord.ui.TextInput(
            label="Duration",
            placeholder="weeks days (for example: 2 3), or perm",
            max_length=32,
            required=True,
        )
        self.reason_input = discord.ui.TextInput(
            label="Reason (required)",
            style=discord.TextStyle.paragraph,
            placeholder=(
                f"{team_name[:35]} / {tag[:20]} · explain why this team is banned."
            )[:100],
            max_length=_MAX_BAN_REASON_LENGTH,
            required=True,
        )
        self.add_item(self.duration_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.apply_form_ban(
            interaction,
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            scrim_id=self.scrim_id,
            team_name=self.team_name,
            tag=self.tag,
            captain_id=self.captain_id,
            duration=self.duration_input.value,
            reason=self.reason_input.value,
            expected_selection=self.expected_selection,
        )


class ManualBanCaptainSelectView(_OwnerView):
    """Native server member picker for a manually entered team identity."""

    def __init__(
        self,
        service: BanService,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        team_name: str,
        tag: str,
        duration: str,
        reason: str,
    ) -> None:
        super().__init__(service, owner_id)
        self.guild_id = guild_id
        self.scrim_id = scrim_id
        self.team_name = team_name
        self.tag = tag
        self.duration = duration
        self.reason = reason
        selector = discord.ui.UserSelect(
            placeholder="Search for and select the Captain",
            min_values=1,
            max_values=1,
        )
        selector.callback = self.select_captain
        self.add_item(selector)

    async def select_captain(self, interaction: discord.Interaction) -> None:
        scrim = self.service._dep("repository").get(self.scrim_id)
        if (
            scrim is None
            or scrim.guild_id != self.guild_id
            or not getattr(scrim, "is_open", False)
            or not self.service._dep("is_active")(scrim)
            or interaction.channel_id != scrim.staff_channel_id
        ):
            await interaction.response.send_message(
                "That scrim is no longer open in this staff channel. Run `!ban` again.",
                ephemeral=True,
            )
            return

        selected = self.children[0].values[0]
        guild = interaction.guild
        member = (
            selected
            if isinstance(selected, discord.Member)
            else guild.get_member(selected.id)
            if guild is not None
            else None
        )
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(selected.id)
            except discord.NotFound:
                member = None
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not resolve the selected ban captain.")
                await interaction.response.send_message(
                    "Discord could not verify that member. Please try the selection again.",
                    ephemeral=True,
                )
                return
        if (
            member is None
            or member.guild.id != self.guild_id
            or member.bot
        ):
            await interaction.response.send_message(
                "Select a current, non-bot member of this server as Captain.",
                ephemeral=True,
            )
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Captain selected. Applying the ban…",
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.service.apply_form_ban(
            interaction,
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            scrim_id=self.scrim_id,
            team_name=self.team_name,
            tag=self.tag,
            captain_id=member.id,
            duration=self.duration,
            reason=self.reason,
        )


class BanScrimSelectView(_OwnerView):
    def __init__(
        self,
        service: BanService,
        owner_id: int,
        guild_id: int,
        scrims: list[object],
        page: int = 0,
        message: object | None = None,
        team_name: str = "",
        tag: str = "",
        allow_live: bool = True,
    ) -> None:
        super().__init__(service, owner_id)
        self.guild_id = guild_id
        self.scrims = scrims
        self.page = page
        self.message = message
        self.team_name = team_name
        self.tag = tag
        self.allow_live = allow_live
        chunk = scrims[page * 25:(page + 1) * 25]
        selector = discord.ui.Select(
            placeholder="Select an active scrim",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=(getattr(scrim, "name", None) or scrim.id)[:100],
                    description=f"Open scrim · {len(service.occupied_slots(scrim))} occupied slots"[:100],
                    value=scrim.id,
                )
                for scrim in chunk
            ],
        )
        selector.callback = self.select_scrim
        self.add_item(selector)
        page_count = math.ceil(len(scrims) / 25)
        if page_count > 1:
            previous = discord.ui.Button(
                label="Previous",
                style=discord.ButtonStyle.secondary,
                disabled=page <= 0,
            )
            following = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                disabled=page >= page_count - 1,
            )
            previous.callback = self.previous_page
            following.callback = self.next_page
            self.add_item(previous)
            self.add_item(following)

    def page_text(self) -> str:
        return (
            f"**Select an active scrim** · page {self.page + 1}/"
            f"{math.ceil(len(self.scrims) / 25)}. This sets the timezone and scope."
        )

    async def select_scrim(self, interaction: discord.Interaction) -> None:
        scrim_id = self.children[0].values[0]
        scrim = self.service._dep("repository").get(scrim_id)
        if (
            scrim is None
            or scrim.guild_id != self.guild_id
            or not getattr(scrim, "is_open", False)
            or not self.service._dep("is_active")(scrim)
            or interaction.channel_id != scrim.staff_channel_id
        ):
            await interaction.response.send_message(
                "That scrim is no longer open in this staff channel. Run `!ban` again.",
                ephemeral=True,
            )
            return
        view = BanLaunchView(
            self.service,
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            scrim_id=scrim.id,
            team_name=self.team_name,
            tag=self.tag,
            allow_live=self.allow_live,
        )
        await interaction.response.send_message(
            content=(
                f"**{getattr(scrim, 'name', None) or scrim.id}** · "
                + (
                    "Choose a manual or live ban."
                    if self.allow_live
                    else "Open the manual ban form."
                )
            ),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass
        await _delete_public_launcher(interaction)

    async def _change_page(self, interaction, direction: int) -> None:
        if not await self.interaction_check(interaction):
            return
        page_count = math.ceil(len(self.scrims) / 25)
        page = max(0, min(self.page + direction, page_count - 1))
        view = BanScrimSelectView(
            self.service,
            self.owner_id,
            self.guild_id,
            self.scrims,
            page,
            message=interaction.message,
            team_name=self.team_name,
            tag=self.tag,
            allow_live=self.allow_live,
        )
        await interaction.response.edit_message(
            content=view.page_text(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def previous_page(self, interaction) -> None:
        await self._change_page(interaction, -1)

    async def next_page(self, interaction) -> None:
        await self._change_page(interaction, 1)


class BanTeamSelectView(_OwnerView):
    def __init__(
        self,
        service: BanService,
        owner_id: int,
        scrim: object,
        *,
        page: int = 0,
        message: object | None = None,
        guild: object | None = None,
    ) -> None:
        super().__init__(service, owner_id)
        self.scrim_id = scrim.id
        self.guild_id = scrim.guild_id
        self.slots = service.occupied_slots(scrim)
        self.selection_snapshots = {
            (slot.number, slot.assignment_id): (
                slot.team_name,
                slot.tag,
                _slot_primary_captain(slot),
            )
            for slot in self.slots
        }
        self.page = page
        self.message = message
        guild = guild or getattr(message, "guild", None)
        chunk = self.slots[page * 25:(page + 1) * 25]
        options = []
        for slot in chunk:
            captain_id = _slot_primary_captain(slot)
            member = (
                getattr(guild, "get_member", lambda _: None)(captain_id)
                if captain_id is not None
                else None
            )
            captain_label = (
                getattr(member, "display_name", None)
                or (str(captain_id) if captain_id is not None else "not set")
            )
            options.append(
                discord.SelectOption(
                    label=f"{slot.number:02d} · {slot.team_name}"[:100],
                    description=f"{slot.tag} · Primary Captain: {captain_label}"[:100],
                    value=f"{slot.number}:{slot.assignment_id}",
                )
            )
        selector = discord.ui.Select(
            placeholder="Select a team to ban",
            min_values=1,
            max_values=1,
            options=options,
        )
        selector.callback = self.select_team
        self.add_item(selector)

    def page_text(self) -> str:
        scrim = self.service._dep("repository").get(self.scrim_id)
        return (
            f"**Select a team to ban** · {getattr(scrim, 'name', None) or self.scrim_id}. "
            "Only the primary captain is shown. The next form asks for duration "
            "and a required reason."
        )

    async def select_team(self, interaction: discord.Interaction) -> None:
        scrim = self.service._dep("repository").get(self.scrim_id)
        if (
            scrim is None
            or scrim.guild_id != self.guild_id
            or not getattr(scrim, "is_open", False)
            or not self.service._dep("is_active")(scrim)
            or interaction.channel_id != scrim.staff_channel_id
        ):
            await interaction.response.send_message(
                "That scrim is no longer open in this staff channel. Run `!ban` again.",
                ephemeral=True,
            )
            return
        try:
            slot_number, assignment_id = (
                int(part) for part in self.children[0].values[0].split(":", 1)
            )
        except (ValueError, TypeError):
            await interaction.response.send_message(
                "That selection is no longer valid. Run `!ban` again.",
                ephemeral=True,
            )
            return
        async with scrim.state_lock:
            slot = scrim.slots.get(slot_number)
            expected = self.selection_snapshots.get((slot_number, assignment_id))
            if (
                expected is None
                or
                slot is None
                or slot.status == self.service._dep("STATUS_AVAILABLE")
                or slot.assignment_id != assignment_id
                or slot.team_name != expected[0]
                or slot.tag != expected[1]
                or _slot_primary_captain(slot) != expected[2]
            ):
                await interaction.response.send_message(
                    "The slot changed after the menu was created. Run `!ban` again.",
                    ephemeral=True,
                )
                return
            team_name, tag = slot.team_name, slot.tag
            captain_id = _slot_primary_captain(slot)
        if captain_id is None:
            await interaction.response.send_message(
                "That team has no primary captain. Assign one before banning it.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            LiveBanDurationModal(
                self.service,
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=scrim.id,
                team_name=team_name,
                tag=tag,
                captain_id=captain_id,
                expected_selection=(
                    scrim.id,
                    assignment_id,
                    slot_number,
                    team_name,
                    tag,
                ),
            )
        )
        await _delete_public_launcher(interaction)


def install_ban_commands(bot: object, service: BanService) -> None:
    """Register the ban prefix commands; the service starts during setup."""
    owner = service.owner

    async def authorized(ctx) -> bool:
        return await owner.require_global_staff_role(ctx, silent=False)

    async def send_feedback(ctx, content: str) -> None:
        await owner.send_private_command_feedback(ctx, content, silent=False)

    @bot.command(name="ban")
    async def ban_command(ctx, *, arguments: str = "") -> None:
        if not await authorized(ctx):
            return
        if ctx.guild is None:
            return
        if ctx.channel.id not in service.known_staff_channel_ids(ctx.guild.id):
            await send_feedback(
                ctx,
                "Use `!ban` in a configured private staff channel.",
            )
            return

        team_name = ""
        tag = ""
        allow_live = True
        if arguments.strip():
            try:
                team_name, tag = parse_manual_ban_arguments(arguments)
            except ValueError as error:
                await send_feedback(ctx, str(error))
                return
            allow_live = False

        matching = [
            scrim
            for scrim in service.active_scrims(ctx.guild.id)
            if scrim.staff_channel_id == ctx.channel.id
        ]
        if not matching:
            await send_feedback(
                ctx,
                "There is no open scrim for this staff channel.",
            )
            return
        if len(matching) == 1:
            scrim = matching[0]
            view = BanLaunchView(
                service,
                owner_id=ctx.author.id,
                guild_id=ctx.guild.id,
                scrim_id=scrim.id,
                team_name=team_name,
                tag=tag,
                allow_live=allow_live,
            )
            content = (
                f"**{getattr(scrim, 'name', None) or scrim.id}** · "
                "Choose a manual or live ban."
                if allow_live
                else "Open the manual ban form. Team Name and TAG are prefilled."
            )
            message = await ctx.send(
                content=content,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.message = message
            await owner.delete_command_message(ctx)
            return

        view = BanScrimSelectView(
            service,
            ctx.author.id,
            ctx.guild.id,
            matching,
            team_name=team_name,
            tag=tag,
            allow_live=allow_live,
        )
        message = await ctx.send(
            content=view.page_text(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message
        await owner.delete_command_message(ctx)

    @bot.command(name="unban")
    async def unban_command(ctx, *, arguments: str = "") -> None:
        if not await authorized(ctx):
            return
        if ctx.guild is None:
            return
        try:
            team_name, tag = parse_unban_arguments(arguments)
            removed, board_ok, role_failures = await service.remove_ban(
                ctx.guild.id, team_name, tag
            )
        except (ValueError, BanStorageError) as error:
            if isinstance(error, BanStorageError):
                logger.exception("Could not remove a team ban.")
                await send_feedback(ctx, "Ban storage is unavailable; no changes were made.")
            else:
                await send_feedback(ctx, str(error))
            return
        if not removed:
            result = f"No active ban matched **{_escape(team_name)} / {_escape(tag)}**."
        else:
            result = (
                f"✅ Removed the ban for **{_escape(team_name)} / {_escape(tag)}**. "
                "Purged slots are not restored."
            )
        if not board_ok:
            result += " ⚠️ The public ban board could not be refreshed."
        if role_failures:
            result += (
                " ⚠️ Punitive role reconciliation failed for "
                + ", ".join(f"<@{captain_id}>" for captain_id in role_failures[:5])
                + "."
            )
        await send_feedback(ctx, result)
        await owner.delete_command_message(ctx)

    @bot.command(name="banconfig")
    async def banconfig_command(ctx, *, arguments: str = "") -> None:
        if not await authorized(ctx):
            return
        if ctx.guild is None:
            return
        try:
            parts = shlex.split(arguments)
        except ValueError:
            parts = []
        if len(parts) != 2 or parts[0].casefold() not in {"channel", "emoji"}:
            await send_feedback(
                ctx,
                "Use `!banconfig channel #bans` or `!banconfig emoji 🔨` "
                "(`default` restores 🔨).",
            )
            return
        setting, value = parts[0].casefold(), parts[1]
        try:
            if setting == "channel":
                match = _CHANNEL_MENTION.fullmatch(value)
                if match:
                    channel_id = int(match.group(1))
                elif value.isdecimal():
                    channel_id = int(value)
                elif value.startswith("#"):
                    matching_channels = [
                        text_channel
                        for text_channel in getattr(ctx.guild, "text_channels", ())
                        if text_channel.name.casefold() == value[1:].casefold()
                    ]
                    if len(matching_channels) != 1:
                        await send_feedback(
                            ctx,
                            "Mention one text channel, such as `#bans`.",
                        )
                        return
                    channel_id = matching_channels[0].id
                else:
                    await send_feedback(ctx, "Mention a text channel, such as `#bans`.")
                    return
                channel = ctx.guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    await send_feedback(ctx, "Choose a text channel in this server.")
                    return
                bot_member = getattr(ctx.guild, "me", None)
                if bot_member is not None:
                    permissions = channel.permissions_for(bot_member)
                    if not (
                        permissions.view_channel
                        and permissions.send_messages
                        and permissions.embed_links
                        and permissions.read_message_history
                    ):
                        await send_feedback(
                            ctx,
                            "Give the bot View Channel, Send Messages, Embed Links, "
                            "and Read Message History in that channel.",
                        )
                        return
                refreshed, old_retired = await service.configure_board_channel(
                    ctx.guild.id, channel.id
                )
                result = (
                    f"✅ Ban board channel set to {channel.mention}."
                    if refreshed
                    else f"⚠️ Saved {channel.mention}, but the board could not be created."
                )
                if not old_retired:
                    result += " The previous board could not be removed."
            else:
                emoji = (
                    DEFAULT_BANNED_EMOJI
                    if value.casefold() == "default"
                    else value
                )
                if not owner.emoji_is_available_in_guild(emoji, ctx.guild):
                    await send_feedback(
                        ctx,
                        "That reaction is not available in this server. Use a standard or server emoji.",
                    )
                    return
                refreshed = await service.change_board_emoji(ctx.guild.id, emoji)
                result = f"✅ Blocked registrations will react with {emoji}."
                if not refreshed:
                    result += " ⚠️ The board could not be refreshed."
        except (BanStorageError, discord.HTTPException):
            logger.exception("Could not update the ban system configuration.")
            await send_feedback(ctx, "The ban system setting could not be saved.")
            return
        await send_feedback(ctx, result)
        await owner.delete_command_message(ctx)