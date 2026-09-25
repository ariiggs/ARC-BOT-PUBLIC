"""Gestionnaire multi-scrim de slots PUBG Mobile."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

from slot_storage import SlotStateStore, SlotStorageError
from scrim_state import (
    STATUS_AVAILABLE,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_RESERVED,
    DEFAULT_LEADERBOARD_ACCENT_COLOR,
    DEFAULT_PLACEMENT_POINTS_STRING,
    DEFAULT_OPERATIONAL_MESSAGES,
    DEFAULT_LEADERBOARD_FOOTER_HEIGHT,
    DEFAULT_LEADERBOARD_HEADER_HEIGHT,
    DEFAULT_LEADERBOARD_ORIENTATION,
    DEFAULT_LEADERBOARD_TEAM_COUNT,
    OPERATIONAL_MESSAGE_KEYS,
    LEADERBOARD_LAYOUTS,
    LEADERBOARD_ORIENTATIONS,
    LEADERBOARD_TEAM_COUNTS,
    LICENSE_TYPES,
    MatchScore,
    MAX_MATCHES,
    Scrim,
    ScrimRepository,
    RegistrationRequest,
    Slot,
    SlotSnapshot,
    parse_placement_points,
    timezone_for_name,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pung-scrim-bot")

FIGURE_SPACE = "\u2007"
SUPPORT_SERVER_URL = "https://discord.gg/S8uaGEJGv8"
LEADERBOARD_BACKGROUND = (
    Path(__file__).parent / "assets" / "leaderboard-background.png"
)
LEADERBOARD_BLUEPRINT_DIR = (
    Path(__file__).parent / "assets" / "leaderboard-blueprints"
)
LEADERBOARD_FONT_PATH = (
    Path(__file__).parent / "assets" / "fonts" / "Montserrat[wght].ttf"
)
LEADERBOARD_FONT_VARIATIONS = {
    400: "Regular",
    700: "Bold",
    800: "ExtraBold",
}
LEADERBOARD_BODY_FONT_WEIGHT = 400
LEADERBOARD_BACKGROUND_UPLOAD_DIR = (
    Path(__file__).parent / "data" / "leaderboard-backgrounds"
)
MAX_LEADERBOARD_BACKGROUND_UPLOAD_BYTES = 8 * 1024 * 1024
LEADERBOARD_VERTICAL_WIDTH = 1080
LEADERBOARD_HORIZONTAL_WIDTH = 1920
LEADERBOARD_OUTER_MARGIN = 28
LEADERBOARD_SECTION_GAP = 10
LEADERBOARD_TABLE_HEADER_HEIGHT = 64
LEADERBOARD_VERTICAL_ROW_HEIGHT = 60
LEADERBOARD_HORIZONTAL_ROW_HEIGHT = 80
LEADERBOARD_VERTICAL_ROW_FONT_SIZE = 21
LEADERBOARD_HORIZONTAL_ROW_FONT_SIZE = 32
LEADERBOARD_ROW_FONT_WEIGHT = 700
LEADERBOARD_DATE_FONT_SIZE = 36
LEADERBOARD_TEAM_NAME_LEFT_PADDING = 12
STANDARD_LEADERBOARD_TEAM_COUNT = 20
LEADERBOARD_FIELD_BASE_WIDTHS = (48, 500, 120, 120, 120, 116)


class LeaderboardBackgroundDimensionsError(ValueError):
    """Raised when an upload does not match the active leaderboard canvas."""

    def __init__(
        self,
        actual: tuple[int, int],
        required: tuple[int, int],
    ) -> None:
        self.actual = actual
        self.required = required
        super().__init__(
            f"Image dimensions are {actual[0]}×{actual[1]}; "
            f"this profile requires {required[0]}×{required[1]}."
        )


HEX_COLOR_GENERATOR_URL = "https://htmlcolorcodes.com/color-picker/"


def _leaderboard_accent_rgb(color_hex: str) -> tuple[int, int, int]:
    if (
        not isinstance(color_hex, str)
        or re.fullmatch(r"#[0-9A-Fa-f]{6}", color_hex) is None
    ):
        raise ValueError("Text colors must use the #RRGGBB HEX format.")
    return tuple(
        int(color_hex[offset : offset + 2], 16)
        for offset in (1, 3, 5)
    )


def _leaderboard_accent_label(color_hex: str) -> str:
    normalized = color_hex.upper()
    if normalized == DEFAULT_LEADERBOARD_ACCENT_COLOR:
        return f"White (`{normalized}`)"
    return f"Custom HEX (`{normalized}`)"

state_store = SlotStateStore(
    os.getenv("SLOTS_DB_PATH", str(Path(__file__).parent / "data" / "slots.sqlite3"))
)
repository = ScrimRepository(state_store)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
bot.remove_command("help")
_setup_installed = False
_global_setup_installed = False
_loaded = False
# Each scrim owns its own live announcement and alert tasks.
active_idpw: dict[str, dict[str, object]] = {}
logs_coverage_snapshots_sent: set[str] = set()


class UnauthorizedGuild(commands.CheckFailure):
    """Raised when a command is used from a guild outside the whitelist."""


class UnauthorizedAuthAdmin(commands.CheckFailure):
    """Raised when a user without bot-admin access invokes !auth."""


async def whitelist_check(ctx: commands.Context) -> bool:
    command_name = getattr(ctx.command, "qualified_name", "")
    if (
        command_name == "auth"
        or command_name.startswith("auth ")
        or command_name == "admin"
        or command_name.startswith("admin ")
        or command_name in {"sub", "status"}
    ):
        return True
    if ctx.guild is None or repository.is_guild_authorized(ctx.guild.id):
        return True
    raise UnauthorizedGuild()


bot.add_check(whitelist_check)


async def auth_admin_check(ctx: commands.Context) -> bool:
    if await bot.is_owner(ctx.author) or repository.is_admin_authorized(
        ctx.author.id
    ):
        return True
    raise UnauthorizedAuthAdmin()


def auth_admin_required():
    return commands.check(auth_admin_check)


def is_active(scrim: Scrim) -> bool:
    return not scrim.deleted and repository.get(scrim.id) is scrim


def assignment_fingerprint(scrim: Scrim) -> str:
    generations = ",".join(
        f"{slot.number}:{slot.assignment_id}" for slot in scrim.slots.values()
    )
    return hashlib.sha256(f"{scrim.id}:{generations}".encode()).hexdigest()[:24]


def slot_status_emoji(scrim: Scrim, slot: Slot | SlotSnapshot) -> str:
    field_name = {
        STATUS_AVAILABLE: "emoji_available",
        STATUS_RESERVED: "emoji_reserved",
        STATUS_PENDING: "emoji_pending",
        STATUS_CONFIRMED: "emoji_confirmed",
    }.get(slot.status, "emoji_available")
    return getattr(scrim, field_name, {
        "emoji_available": "⚪",
        "emoji_reserved": "🔵",
        "emoji_pending": "🟠",
        "emoji_confirmed": "🟢",
    }[field_name])


def slot_status_label(slot: Slot | SlotSnapshot) -> str:
    return {
        STATUS_AVAILABLE: "Available",
        STATUS_RESERVED: "Reserved",
        STATUS_PENDING: "Pending",
        STATUS_CONFIRMED: "Confirmed",
    }.get(slot.status, "Unknown")


def slot_is_assignable(slot: Slot | SlotSnapshot) -> bool:
    return slot.status == STATUS_AVAILABLE


def registration_slot_is_available(scrim: Scrim, slot: Slot | SlotSnapshot) -> bool:
    return slot_is_assignable(slot) and not any(
        request.slot_number == slot.number
        for request in getattr(scrim, "pending_registrations", {}).values()
    )


def release_slot(slot: Slot) -> SlotSnapshot:
    audit = slot.snapshot()
    slot.clear()
    return audit


def disable_view_items(view: discord.ui.View) -> None:
    for item in view.children:
        if isinstance(item, discord.ui.DynamicItem):
            item = item.item
        if hasattr(item, "disabled"):
            item.disabled = True


def slot_display_line(scrim: Scrim, slot: Slot) -> str:
    prefix = f"`{slot.number:02d}`{FIGURE_SPACE}{slot_status_emoji(scrim, slot)}"
    if slot.status == STATUS_AVAILABLE:
        return prefix
    tag = slot.tag or "—"
    captain_ids = [
        captain_id
        for captain_id in (
            getattr(slot, "captain_1_id", None),
            getattr(slot, "captain_2_id", None),
        )
        if captain_id is not None
    ]
    if not captain_ids and slot.manager_id:
        captain_ids.append(slot.manager_id)
    captains = (
        " / ".join(f"<@{captain_id}>" for captain_id in captain_ids)
        if captain_ids
        else "Not specified"
    )
    return f"{prefix}{FIGURE_SPACE}{slot.team_name} | {tag} | {captains}"


def build_slots_message(scrim: Scrim) -> str:
    emojis = {
        "available": scrim.emoji_available,
        "reserved": scrim.emoji_reserved,
        "pending": scrim.emoji_pending,
        "confirmed": scrim.emoji_confirmed,
    }
    legend = (
        f"{emojis['available']} Available · {emojis['reserved']} Reserved · "
        f"{emojis['pending']} Pending · {emojis['confirmed']} Confirmed"
    )
    return (
        f"**{discord.utils.escape_markdown(scrim.name)}**\n"
        f"{legend}\n\n"
        + "\n".join(
            slot_display_line(scrim, slot) for slot in scrim.slots.values()
        )
        + "\n\n_PUBG Mobile Scrim Slot Manager_"
    )


def build_staff_slots_message(scrim: Scrim) -> str:
    return (
        f"**Staff mirror — {discord.utils.escape_markdown(scrim.name)}**\n"
        f"{'OPEN' if scrim.is_open else 'CLOSED'} · updates remain in this channel\n\n"
        + "\n".join(
            slot_display_line(scrim, slot) for slot in scrim.slots.values()
        )
    )


def member_is_staff(member: object, scrim: Scrim) -> bool:
    roles = {getattr(role, "id", None) for role in getattr(member, "roles", ())}
    config = repository.get_server_config(scrim.guild_id)
    return bool(config and config.staff_role_id is not None and config.staff_role_id in roles)


def member_has_global_staff_role(member: object, guild_id: int) -> bool:
    """Require the configured server-wide Staff role without privilege bypasses."""
    roles = {getattr(role, "id", None) for role in getattr(member, "roles", ())}
    config = repository.get_server_config(guild_id)
    return bool(
        config
        and config.staff_role_id is not None
        and config.staff_role_id in roles
    )


def member_is_staff_in_guild(member: object, guild_id: int) -> bool:
    """Check staff authorization without requiring a scrim channel context."""
    if member_is_admin(member):
        return True
    roles = {getattr(role, "id", None) for role in getattr(member, "roles", ())}
    config = repository.get_server_config(guild_id)
    if config and {
        config.head_staff_role_id,
        config.staff_role_id,
    }.intersection(roles):
        return True
    return any(
        scrim.staff_role_id is not None and scrim.staff_role_id in roles
        for scrim in repository.list(guild_id)
    )


def member_can_configure_scrim(member: object, scrim: Scrim) -> bool:
    """Check leaderboard configuration access for this specific scrim."""
    if (
        member_is_head_staff(member, scrim.guild_id)
        or member_is_staff(member, scrim)
    ):
        return True
    return scrim.staff_role_id is not None and scrim.staff_role_id in {
        getattr(role, "id", None) for role in getattr(member, "roles", ())
    }


def member_is_admin(member: object) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions is not None and getattr(permissions, "administrator", False)
    )


def member_is_head_staff(member: object, guild_id: int) -> bool:
    if member_is_admin(member):
        return True
    config = repository.get_server_config(guild_id)
    return bool(
        config
        and config.head_staff_role_id in {
            getattr(role, "id", None) for role in getattr(member, "roles", ())
        }
    )


async def require_staff_scrim(
    ctx: commands.Context,
    *,
    public_only: bool = False,
    allow_public: bool = False,
    silent: bool = True,
) -> Scrim | None:
    if ctx.guild is None:
        await send_private_command_feedback(
            ctx, "This command can only be used in a Discord server.", silent=silent
        )
        return None
    scrim = resolve_channel_scrim(ctx)
    selected = False
    override = getattr(ctx, "_staff_scrim_override", None)
    if scrim is None and not public_only and override is not None:
        if (
            is_active(override)
            and override.guild_id == ctx.guild.id
            and member_is_staff(ctx.author, override)
        ):
            scrim = override
            selected = True
        try:
            delattr(ctx, "_staff_scrim_override")
        except AttributeError:
            pass
    allowed_channel_ids = (
        (scrim.public_channel_id, scrim.staff_channel_id)
        if scrim is not None and allow_public
        else (scrim.public_channel_id,)
        if scrim is not None and public_only
        else (scrim.staff_channel_id,)
        if scrim is not None
        else ()
    )
    if scrim is None:
        await show_staff_scrim_selector(ctx)
        return None
    if not selected and ctx.channel.id not in allowed_channel_ids:
        if public_only:
            message = "This command must be used in the configured public slots channel."
        elif allow_public:
            message = "This command must be used in the configured public or staff channel."
        else:
            message = "This command must be used in the configured staff channel."
        await send_private_command_feedback(
            ctx,
            message,
            silent=silent,
        )
        return None
    if not member_is_staff(ctx.author, scrim):
        await send_private_command_feedback(
            ctx,
            "You do not have the staff role authorized for this scrim.",
            silent=silent,
        )
        return None
    return scrim


async def delete_command_message(ctx: commands.Context) -> bool:
    message = getattr(ctx, "message", None)
    delete = getattr(message, "delete", None)
    if delete is None:
        return False
    try:
        await delete()
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False


async def send_private_command_feedback(
    ctx: commands.Context,
    content: str,
    *,
    delete_command: bool = True,
    silent: bool = False,
    **kwargs,
):
    """Send routine prefix-command feedback in the invoking channel."""
    if silent:
        if delete_command:
            await delete_command_message(ctx)
        return None
    send_kwargs = dict(kwargs)
    send_kwargs.setdefault("delete_after", 15)
    send_kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    sent = None
    try:
        sent = await ctx.send(content, **send_kwargs)
    except discord.HTTPException:
        logger.exception("Could not send command feedback.")
    if delete_command:
        await delete_command_message(ctx)
    return sent


async def send_private_registration_feedback(
    ctx: commands.Context,
    content: str,
) -> None:
    """Send registration errors only to the member who submitted the command."""
    try:
        await ctx.author.send(
            content,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception(
            "Could not DM registration feedback to %s.",
            getattr(ctx.author, "id", None),
        )
    finally:
        await delete_command_message(ctx)


async def purge_channel_messages(channel) -> bool:
    """Clear unpinned channel messages in batches, preserving pinned messages."""
    purge = getattr(channel, "purge", None)
    if purge is None:
        return False
    try:
        while True:
            deleted = await purge(
                limit=100,
                check=lambda message: not getattr(message, "pinned", False),
            )
            if len(deleted) < 100:
                return True
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        logger.exception("Could not clear channel %s.", getattr(channel, "id", None))
        return False


async def get_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    return channel if channel is not None else await bot.fetch_channel(channel_id)


async def configured_idpw_channel(guild: discord.Guild, channel_id: int):
    """Resolve an ID/password channel without crossing the configured guild."""
    try:
        channel = guild.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Could not resolve the configured ID/password channel (%s).",
            channel_id,
        )
        return None
    channel_guild = getattr(channel, "guild", None)
    if (
        not isinstance(channel, discord.TextChannel)
        or channel_guild is None
        or channel_guild.id != guild.id
    ):
        logger.error(
            "Invalid configured ID/password channel for guild %s (channel %s).",
            guild.id,
            channel_id,
        )
        return None
    return channel


def _format_idpw_reminder(
    *,
    title: str,
    message: str,
    confirmed_role_id: int | None,
) -> str:
    """Build the Discord-formatted reminder shown before a match."""
    lines = [f"# **{title}**", f"**{message}**"]
    if confirmed_role_id is not None:
        lines.append(f"<@&{confirmed_role_id}>")
    return "\n".join(lines)


def _track_active_idpw_message(
    scrim_id: str,
    state: dict[str, object],
    message: object,
) -> bool:
    """Track a sent reminder only while its ID/PW run is still current."""
    if active_idpw.get(scrim_id) is not state:
        return False
    messages = state.setdefault("messages", [])
    if isinstance(messages, list):
        messages.append(message)
    return True


async def clear_active_idpw(scrim_id: str | None = None) -> None:
    """Cancel ID/PW alerts and remove the persisted announcement for a scrim."""
    scrim_ids = (
        [scrim_id]
        if scrim_id is not None
        else list(set(active_idpw) | set(repository.idpw_configs))
    )
    for current_id in scrim_ids:
        state = active_idpw.pop(current_id, {})
        for task in state.get("tasks", []):
            if not task.done():
                task.cancel()

        message = state.get("message")
        messages = list(state.get("messages", []))
        if message is not None and all(
            getattr(existing, "id", None) != getattr(message, "id", None)
            for existing in messages
        ):
            messages.insert(0, message)
        config = repository.get_idpw_config(current_id)
        scrim = repository.get(current_id)
        persisted_message_id = (
            config.announcement_message_id
            if config is not None
            else None
        )
        active_message_id = getattr(message, "id", None)
        if (
            persisted_message_id is not None
            and persisted_message_id != active_message_id
            and scrim is not None
        ):
            try:
                channel = await configured_text_channel(
                    scrim, config.target_channel_id
                )
                fetch_message = getattr(channel, "fetch_message", None)
                if fetch_message is not None:
                    persisted_message = await fetch_message(persisted_message_id)
                    if all(
                        getattr(existing, "id", None)
                        != getattr(persisted_message, "id", None)
                        for existing in messages
                    ):
                        messages.insert(0, persisted_message)
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not resolve the MATCH ACCESS message for scrim %s.",
                    current_id,
                )

        for message in messages:
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not delete the MATCH ACCESS message for scrim %s.",
                    current_id,
                )

        if config is not None and config.announcement_message_id is not None:
            try:
                repository.set_idpw_announcement(current_id, None)
            except (SlotStorageError, ValueError):
                logger.exception(
                    "Could not clear the MATCH ACCESS reference for scrim %s.",
                    current_id,
                )


async def configured_text_channel(scrim: Scrim, channel_id: int):
    """Resolve a configured text channel without crossing the scrim's guild."""
    channel = await get_channel(channel_id)
    guild = getattr(channel, "guild", None)
    if not isinstance(channel, discord.TextChannel) or guild is None or guild.id != scrim.guild_id:
        logger.error(
            "Invalid configured text channel for scrim %s (channel %s).",
            scrim.id,
            channel_id,
        )
        return None
    return channel


def resolve_named_scrim(guild_id: int, name: str) -> Scrim | None:
    folded = name.strip().casefold()
    return next((s for s in repository.list(guild_id) if s.name.casefold() == folded), None)


def resolve_channel_scrim(ctx: commands.Context, *, staff_only: bool = False) -> Scrim | None:
    if ctx.guild is None:
        return None
    channel_id = getattr(ctx.channel, "id", None)
    matches = [
        scrim
        for scrim in repository.list(ctx.guild.id)
        if channel_id in (
            (getattr(scrim, "staff_channel_id", None),)
            if staff_only
            else (
                getattr(scrim, "public_channel_id", None),
                getattr(scrim, "staff_channel_id", None),
            )
        )
    ]
    return matches[0] if len(matches) == 1 else None


class StaffScrimSelectView(discord.ui.View):
    """Select a scrim for one Staff command without persisting the choice."""

    def __init__(
        self,
        ctx: commands.Context,
        scrims: list[Scrim],
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.owner_id = ctx.author.id
        self.guild_id = ctx.guild.id
        self.channel_id = ctx.channel.id
        self.scrim_ids = {scrim.id for scrim in scrims}
        self.command = getattr(ctx, "command", None)
        self.command_args = tuple(getattr(ctx, "args", (ctx,)))
        self.command_kwargs = dict(getattr(ctx, "kwargs", {}))

        select = discord.ui.Select(
            placeholder="Select a scrim for this command...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=scrim.name[:100],
                    value=scrim.id,
                )
                for scrim in scrims[:25]
            ],
        )

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This scrim selector belongs to another user.",
                    ephemeral=True,
                )
                return
            if (
                interaction.guild is None
                or interaction.guild.id != self.guild_id
                or interaction.channel_id != self.channel_id
            ):
                await interaction.response.send_message(
                    "This selector is not valid in this server or channel.",
                    ephemeral=True,
                )
                return

            selected_id = select.values[0]
            if selected_id not in self.scrim_ids:
                await interaction.response.send_message(
                    "That scrim selection is no longer available.",
                    ephemeral=True,
                )
                self.stop()
                return

            scrim = repository.get(selected_id)
            if (
                scrim is None
                or scrim.guild_id != self.guild_id
                or not is_active(scrim)
            ):
                await interaction.response.send_message(
                    "That scrim is no longer active.",
                    ephemeral=True,
                )
                self.stop()
                return
            if not member_is_staff(interaction.user, scrim):
                await interaction.response.send_message(
                    "You do not have the authorized Staff role for this scrim.",
                    ephemeral=True,
                )
                self.stop()
                return

            disable_view_items(self)
            self.stop()
            await interaction.response.edit_message(
                content=(
                    f"✅ Running this command for "
                    f"**{discord.utils.escape_markdown(scrim.name)}**."
                ),
                view=self,
            )
            setattr(self.ctx, "_staff_scrim_override", scrim)
            try:
                command = self.command
                if command is None:
                    await interaction.followup.send(
                        "The command could not be resumed.",
                        ephemeral=True,
                    )
                    return
                callback = getattr(command, "callback", None)
                if callback is not None:
                    await callback(*self.command_args, **self.command_kwargs)
                else:
                    await command.invoke(self.ctx)
            except Exception:
                logger.exception("Could not resume the command after scrim selection.")
            finally:
                try:
                    delattr(self.ctx, "_staff_scrim_override")
                except AttributeError:
                    pass

        select.callback = callback
        self.add_item(select)


async def show_staff_scrim_selector(ctx: commands.Context) -> None:
    """Ask Staff to choose a scrim for the current command invocation."""
    if ctx.guild is None:
        return
    active_scrims = [
        scrim
        for scrim in repository.list(ctx.guild.id)
        if is_active(scrim) and member_is_staff(ctx.author, scrim)
    ]
    if not active_scrims:
        await send_private_command_feedback(
            ctx,
            "No active scrims are available for your Staff role.",
            silent=False,
        )
        return

    view = StaffScrimSelectView(ctx, active_scrims)
    try:
        await ctx.send(
            "Select a scrim for this command:",
            view=view,
            delete_after=120,
        )
    except discord.HTTPException:
        logger.exception("Could not show the Staff scrim selector.")
    finally:
        await delete_command_message(ctx)


def resolve_registration_scrim(ctx: commands.Context) -> Scrim | None:
    """Resolve the scrim whose optional public registration channel was used."""
    if ctx.guild is None:
        return None
    matches = [
        scrim
        for scrim in repository.list(ctx.guild.id)
        if getattr(scrim, "registration_channel_id", None) == ctx.channel.id
    ]
    return matches[0] if len(matches) == 1 else None


def registration_role_allows(ctx: commands.Context, scrim: Scrim) -> bool:
    """Check the configured registration role, including @everyone."""
    role_id = getattr(scrim, "registration_role_id", None)
    if role_id is None or ctx.guild is None:
        return False
    if role_id == ctx.guild.id:
        return True
    return role_id in {
        getattr(role, "id", None) for role in getattr(ctx.author, "roles", ())
    }


async def require_registration_staff_channel(
    ctx: commands.Context,
) -> Scrim | None:
    if ctx.guild is None:
        await send_private_command_feedback(
            ctx,
            "This command can only be used in a Discord server.",
            silent=True,
        )
        return None
    scrim = resolve_registration_scrim(ctx)
    if scrim is None:
        await send_private_command_feedback(
            ctx,
            "This command must be used in the configured registration channel.",
            silent=True,
        )
        return None
    if not member_is_staff(ctx.author, scrim):
        await send_private_command_feedback(
            ctx,
            "You do not have the staff role authorized for this scrim.",
            silent=True,
        )
        return None
    return scrim


async def set_registration_channel_open(
    ctx: commands.Context, scrim: Scrim, is_open: bool
) -> bool:
    role_id = getattr(scrim, "registration_role_id", None)
    if role_id is None or ctx.guild is None:
        return False
    role = (
        ctx.guild.default_role
        if role_id == ctx.guild.id
        else ctx.guild.get_role(role_id)
    )
    if role is None:
        try:
            role = await ctx.guild.fetch_role(role_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not resolve registration role %s for scrim %s.",
                role_id,
                scrim.id,
            )
            return False
    channel = await configured_text_channel(scrim, scrim.registration_channel_id)
    if channel is None:
        return False
    try:
        await channel.set_permissions(
            role,
            send_messages=is_open,
            reason=(
                "Registration channel opened by staff"
                if is_open
                else "Registration channel closed by staff"
            ),
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Could not update registration channel permissions for scrim %s.",
            scrim.id,
        )
        return False
    async with scrim.state_lock:
        if not is_active(scrim):
            return False
        with repository.transaction():
            scrim.registration_open = is_open
    return True


OPERATIONAL_MESSAGE_LABELS = {
    "open_registration": "Open registrations",
    "close_registration": "Close registrations",
    "open_slots": "Open slot confirmations",
    "close_slots": "Close slot confirmations",
    "publish_results": "Results publication",
}


def resolve_operational_scrim(ctx: commands.Context) -> Scrim | None:
    """Resolve a scrim from its registration, public, or staff channel."""
    return resolve_registration_scrim(ctx) or resolve_channel_scrim(ctx)


def operational_message(scrim: Scrim, key: str) -> str:
    template = getattr(scrim, "operational_messages", {}).get(
        key, DEFAULT_OPERATIONAL_MESSAGES[key]
    )
    scrim_name = str(getattr(scrim, "name", "this scrim"))
    public_channel_id = getattr(scrim, "public_channel_id", 0)
    replacements = {
        "scrim": discord.utils.escape_markdown(scrim_name),
        "channel": f"<#{public_channel_id}>",
    }
    try:
        return template.format(**replacements)
    except (KeyError, ValueError):
        logger.warning(
            "Invalid operational message template for scrim %s and key %s.",
            scrim.id,
            key,
        )
        return DEFAULT_OPERATIONAL_MESSAGES[key].format(**replacements)


async def send_operational_message(
    ctx: commands.Context,
    scrim: Scrim,
    key: str,
) -> None:
    context = (
        "registration"
        if key in {"open_registration", "close_registration"}
        else "slots"
    )
    reference = getattr(scrim, "operational_message_refs", {}).get(context)
    if reference is not None:
        channel = bot.get_channel(reference["channel_id"])
        if channel is None:
            try:
                channel = await bot.fetch_channel(reference["channel_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not resolve the previous operational message channel "
                    "for scrim %s.",
                    scrim.id,
                )
        if channel is not None:
            try:
                previous = await channel.fetch_message(reference["message_id"])
                await previous.delete()
            except discord.NotFound:
                try:
                    repository.clear_operational_message_reference(
                        scrim.id, scrim.guild_id, context
                    )
                except (SlotStorageError, ValueError):
                    logger.exception(
                        "Could not clear the missing operational message reference "
                        "for scrim %s.",
                        scrim.id,
                    )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not delete the previous operational message for scrim %s.",
                    scrim.id,
                )
            else:
                try:
                    repository.clear_operational_message_reference(
                        scrim.id, scrim.guild_id, context
                    )
                except (SlotStorageError, ValueError):
                    logger.exception(
                        "Could not clear the previous operational message reference "
                        "for scrim %s.",
                        scrim.id,
                    )
    try:
        sent = await ctx.send(
            operational_message(scrim, key),
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=True,
                replied_user=False,
            ),
        )
    except discord.HTTPException:
        logger.exception(
            "Could not send operational message %s for scrim %s.",
            key,
            scrim.id,
        )
        return
    sent_id = getattr(sent, "id", None)
    channel_id = getattr(ctx.channel, "id", None)
    if type(sent_id) is int and sent_id > 0 and type(channel_id) is int and channel_id > 0:
        try:
            repository.set_operational_message_reference(
                scrim.id,
                scrim.guild_id,
                context,
                sent_id,
                channel_id,
            )
        except (SlotStorageError, ValueError):
            logger.exception(
                "Could not save the operational message reference for scrim %s.",
                scrim.id,
            )


def operational_message_panel_text(scrim: Scrim | None) -> str:
    if scrim is None:
        return (
            "**Operational Messages**\n"
            "Select a scrim, then choose which `!open`/`!close` messages or "
            "results publication template to edit."
        )
    return (
        f"**Operational Messages — "
        f"{discord.utils.escape_markdown(scrim.name)}**\n"
        "Choose **Registrations** or **Slots** to edit open/close messages, "
        "or **Results** to edit the `!res` publication.\n"
        "Results placeholders: `{scrim}`, `{team_count}`, `{match_count}`; "
        "for ranks 1–3 use `{top1_team}`, `{top1_points}`, `{top1_kills}`, "
        "`{top1_wins}` (replace `1` with `2` or `3`). Missing ranks show "
        "`—` and zeroes."
    )


def operational_message_preview(message: str) -> str:
    message = message.replace("```", "'''")
    if len(message) > 900:
        message = f"{message[:897]}..."
    return message


class OperationalMessageView(discord.ui.View):
    """Staff-bound panel for editing a scrim's operational messages."""

    def __init__(
        self,
        owner_id: int,
        guild_id: int,
        *,
        selected_id: str | None = None,
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.selected_id = selected_id
        self.message: discord.Message | None = None
        self.rebuild()

    def scrims(self) -> list[Scrim]:
        return repository.list(self.guild_id)

    def selected_scrim(self) -> Scrim | None:
        selected = repository.get(self.selected_id) if self.selected_id else None
        return selected if selected and selected.guild_id == self.guild_id else None

    def authorized(self, interaction: discord.Interaction) -> bool:
        return bool(
            interaction.guild is not None
            and interaction.guild.id == self.guild_id
            and interaction.user.id == self.owner_id
            and member_is_staff_in_guild(interaction.user, self.guild_id)
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.authorized(interaction):
            return True
        text = (
            "This message panel belongs to another staff member, "
            "or you no longer have staff access."
        )
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Operational message panel interaction failed", exc_info=error)
        text = (
            "The message could not be saved. Nothing was changed."
            if isinstance(error, (SlotStorageError, ValueError))
            else "Something went wrong. Please try again."
        )
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    def content(self) -> str:
        return operational_message_panel_text(self.selected_scrim())

    def embed(self) -> discord.Embed:
        scrim = self.selected_scrim()
        embed = discord.Embed(
            title="Operational Messages",
            description=(
                "Select a scrim below to edit messages sent by `!open`, "
                "`!close`, and `!res`."
                if scrim is None
                else (
                    f"**{discord.utils.escape_markdown(scrim.name)}**\n"
                    "The current templates are shown below. Choose Registrations "
                    "or Slots to edit open/close messages, or Results to edit "
                    "the `!res` publication."
                )
            ),
            color=discord.Color.blurple(),
        )
        if scrim is not None:
            messages = getattr(scrim, "operational_messages", {})
            for key in OPERATIONAL_MESSAGE_KEYS:
                embed.add_field(
                    name=OPERATIONAL_MESSAGE_LABELS[key],
                    value=operational_message_preview(
                        messages.get(key, DEFAULT_OPERATIONAL_MESSAGES[key])
                    ),
                    inline=False,
                )
        return embed

    def rebuild(self) -> None:
        self.clear_items()
        scrims = self.scrims()
        selected = self.selected_scrim()
        if selected is None and self.selected_id is not None:
            self.selected_id = None

        if scrims:
            options = [
                discord.SelectOption(
                    label=discord.utils.escape_markdown(scrim.name)[:100],
                    value=scrim.id,
                    default=scrim.id == self.selected_id,
                )
                for scrim in scrims
            ]
            selector = discord.ui.Select(
                placeholder="Select a scrim...",
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )

            async def select_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                self.selected_id = selector.values[0]
                self.rebuild()
                await interaction.response.edit_message(
                    content=self.content(),
                    embed=self.embed(),
                    view=self,
                )

            selector.callback = select_callback
            self.add_item(selector)

        registration_button = discord.ui.Button(
            label="Registrations",
            emoji="📝",
            style=discord.ButtonStyle.primary,
            disabled=(
                selected is None
                or getattr(selected, "registration_channel_id", None) is None
            ),
            row=1,
        )
        slots_button = discord.ui.Button(
            label="Slots",
            emoji="🎮",
            style=discord.ButtonStyle.primary,
            disabled=selected is None,
            row=1,
        )

        async def registration_callback(interaction: discord.Interaction) -> None:
            if await self.interaction_check(interaction):
                await interaction.response.send_modal(
                    OperationalMessageModal(self, "registration")
                )

        async def slots_callback(interaction: discord.Interaction) -> None:
            if await self.interaction_check(interaction):
                await interaction.response.send_modal(
                    OperationalMessageModal(self, "slots")
                )

        registration_button.callback = registration_callback
        slots_button.callback = slots_callback
        self.add_item(registration_button)
        self.add_item(slots_button)

        results_button = discord.ui.Button(
            label="Results",
            emoji="🏆",
            style=discord.ButtonStyle.primary,
            disabled=selected is None,
            row=1,
        )

        async def results_callback(interaction: discord.Interaction) -> None:
            if await self.interaction_check(interaction):
                await interaction.response.send_modal(ResultsMessageModal(self))

        results_button.callback = results_callback
        self.add_item(results_button)

        reset_button = discord.ui.Button(
            label="Reset Selected",
            emoji="↩️",
            style=discord.ButtonStyle.danger,
            disabled=selected is None,
            row=2,
        )
        close_button = discord.ui.Button(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.secondary,
            row=2,
        )

        async def reset_callback(interaction: discord.Interaction) -> None:
            if not await self.interaction_check(interaction):
                return
            scrim = self.selected_scrim()
            if scrim is None:
                await interaction.response.send_message(
                    "Select a scrim first.", ephemeral=True
                )
                return
            try:
                repository.update_operational_messages(
                    scrim.id,
                    self.guild_id,
                    dict(DEFAULT_OPERATIONAL_MESSAGES),
                )
            except (SlotStorageError, ValueError):
                logger.exception("Could not reset operational messages")
                await interaction.response.send_message(
                    "The messages could not be reset. Nothing was changed.",
                    ephemeral=True,
                )
                return
            self.rebuild()
            await interaction.response.edit_message(
                content=self.content(),
                embed=self.embed(),
                view=self,
            )

        async def close_callback(interaction: discord.Interaction) -> None:
            if not await self.interaction_check(interaction):
                return
            self.stop()
            await interaction.response.edit_message(
                content="Operational message panel closed.",
                embed=None,
                view=None,
            )

        reset_button.callback = reset_callback
        close_button.callback = close_callback
        self.add_item(reset_button)
        self.add_item(close_button)

    async def refresh_message(self) -> None:
        self.rebuild()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=self.content(),
                embed=self.embed(),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not refresh the operational message panel")


class OperationalMessageModal(discord.ui.Modal):
    open_message = discord.ui.TextInput(
        label="Message after !open",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )
    close_message = discord.ui.TextInput(
        label="Message after !close",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )

    def __init__(self, panel: OperationalMessageView, category: str):
        super().__init__(
            title=(
                "Edit registration messages"
                if category == "registration"
                else "Edit slot messages"
            )[:45],
            timeout=300,
        )
        self.panel = panel
        self.category = category
        scrim = panel.selected_scrim()
        if scrim is not None:
            messages = getattr(scrim, "operational_messages", {})
            prefix = "registration" if category == "registration" else "slots"
            self.open_message.default = messages.get(
                f"open_{prefix}", DEFAULT_OPERATIONAL_MESSAGES[f"open_{prefix}"]
            )
            self.close_message.default = messages.get(
                f"close_{prefix}", DEFAULT_OPERATIONAL_MESSAGES[f"close_{prefix}"]
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.panel.authorized(interaction):
            await interaction.response.send_message(
                "This message panel is no longer available to you.",
                ephemeral=True,
            )
            return
        scrim = self.panel.selected_scrim()
        if scrim is None:
            await interaction.response.send_message(
                "That scrim no longer exists.", ephemeral=True
            )
            return
        prefix = "registration" if self.category == "registration" else "slots"
        try:
            repository.update_operational_messages(
                scrim.id,
                self.panel.guild_id,
                {
                    f"open_{prefix}": str(self.open_message.value).strip(),
                    f"close_{prefix}": str(self.close_message.value).strip(),
                },
            )
        except (SlotStorageError, ValueError) as error:
            await interaction.response.send_message(
                f"The messages could not be saved. {error}",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.panel.refresh_message()
        await interaction.followup.send(
            "✅ The operational messages were saved.",
            ephemeral=True,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.exception("Operational message modal failed", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "The messages could not be saved. Nothing was changed.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "The messages could not be saved. Nothing was changed.",
                ephemeral=True,
            )


class ResultsMessageModal(discord.ui.Modal):
    """Edit the publication text attached to the !res leaderboard image."""

    template = discord.ui.TextInput(
        label="Results publication message",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )

    def __init__(self, panel: OperationalMessageView):
        super().__init__(title="Edit results publication", timeout=300)
        self.panel = panel
        scrim = panel.selected_scrim()
        if scrim is not None:
            self.template.default = getattr(scrim, "operational_messages", {}).get(
                "publish_results",
                DEFAULT_OPERATIONAL_MESSAGES["publish_results"],
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.panel.authorized(interaction):
            await interaction.response.send_message(
                "This message panel is no longer available to you.",
                ephemeral=True,
            )
            return
        scrim = self.panel.selected_scrim()
        if scrim is None:
            await interaction.response.send_message(
                "That scrim no longer exists.", ephemeral=True
            )
            return
        try:
            repository.update_operational_message(
                scrim.id,
                self.panel.guild_id,
                "publish_results",
                str(self.template.value).strip(),
            )
        except (SlotStorageError, ValueError) as error:
            await interaction.response.send_message(
                f"The results message could not be saved. {error}",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.panel.refresh_message()
        await interaction.followup.send(
            "✅ The results publication message was saved.",
            ephemeral=True,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.exception("Results message modal failed", exc_info=error)
        text = "The results message could not be saved. Nothing was changed."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


@bot.command(name="msg")
@commands.guild_only()
async def configure_operational_message(ctx: commands.Context) -> None:
    """Open the staff panel for customizing !open and !close messages."""
    if not member_is_staff_in_guild(ctx.author, ctx.guild.id):
        await send_private_command_feedback(
            ctx,
            "You do not have the configured Staff role to use `!msg`.",
            silent=False,
        )
        return
    selected = resolve_operational_scrim(ctx)
    panel = OperationalMessageView(
        ctx.author.id,
        ctx.guild.id,
        selected_id=selected.id if selected is not None else None,
    )
    try:
        panel.message = await ctx.send(
            panel.content(),
            embed=panel.embed(),
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception("Could not open the operational message panel")
        return
    await delete_command_message(ctx)


async def require_channel_scrim(
    ctx: commands.Context, *, staff_only: bool = False
) -> Scrim | None:
    scrim = resolve_channel_scrim(ctx, staff_only=staff_only)
    if scrim is None:
        where = "configured staff channel" if staff_only else "configured scrim channel"
        await send_private_command_feedback(
            ctx,
            f"This command must be used in a {where}. "
            "Use `!update Scrim Name` to select a board explicitly.",
            silent=True,
        )
    return scrim


class DurableView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        logger.error("Interaction error", exc_info=error)
        text = (
            "Your action could not be saved. Please check the board and try again."
            if isinstance(error, SlotStorageError)
            else "Something went wrong. Please check the board before trying again."
        )
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class SubscriptionStatusView(discord.ui.View):
    """Link-only view attached to subscription status messages."""

    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="🎧 Support Server",
                url=SUPPORT_SERVER_URL,
            )
        )


def subscription_remaining_text(expires_at: datetime) -> str:
    remaining_seconds = max(
        0, int((expires_at - datetime.now(timezone.utc)).total_seconds())
    )
    days, remainder = divmod(remaining_seconds, 24 * 60 * 60)
    hours = remainder // (60 * 60)
    return f"{days} day(s), {hours} hour(s) remaining"


def build_subscription_embed(
    *, authorized: bool, expires_at: datetime | None, duration_days: int | None
) -> discord.Embed:
    if authorized and duration_days == 0 and expires_at is None:
        embed = discord.Embed(
            title="💎 **A.R.C. Subscription Status**",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Status", value="🟢 Active", inline=False)
        embed.add_field(
            name="Time Remaining",
            value="♾️ Lifetime / Unlimited",
            inline=False,
        )
        return embed

    if authorized and expires_at is not None:
        expires_at = expires_at.astimezone(timezone.utc)
        if expires_at > datetime.now(timezone.utc):
            embed = discord.Embed(
                title="💎 **A.R.C. Subscription Status**",
                color=discord.Color.green(),
            )
            embed.add_field(name="Status", value="🟢 Active", inline=False)
            embed.add_field(
                name="Time Remaining",
                value=subscription_remaining_text(expires_at),
                inline=False,
            )
            return embed
        expired_text = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    else:
        expired_text = "Expired"

    embed = discord.Embed(
        title="💎 **A.R.C. Subscription Status**",
        color=discord.Color.red(),
    )
    embed.add_field(name="Status", value="🔴 Expired", inline=False)
    embed.add_field(
        name="Time Remaining",
        value=(
            f"Expired on {expired_text}"
            if expired_text != "Expired"
            else "Expired"
        ),
        inline=False,
    )
    return embed


@bot.command(name="sub", aliases=["status"])
async def subscription_status(ctx: commands.Context) -> None:
    access_denied = (
        "❌ **Access Denied.** Only the Server Owner or a designated "
        "Bot Manager can view the subscription status."
    )
    if ctx.guild is None:
        await send_private_command_feedback(ctx, access_denied, silent=False)
        return

    config = repository.get_server_config(ctx.guild.id)
    role_ids = {
        getattr(role, "id", None) for role in getattr(ctx.author, "roles", ())
    }
    is_owner = getattr(ctx.guild, "owner_id", None) == getattr(ctx.author, "id", None)
    manager_role_id = (
        getattr(config, "manager_role_id", None)
        if config is not None
        else None
    )
    if manager_role_id is None and config is not None:
        # !set currently persists the designated Bot Manager role as staff_role_id.
        manager_role_id = config.staff_role_id
    is_manager = bool(
        manager_role_id is not None and manager_role_id in role_ids
    )
    if not is_owner and not is_manager:
        await send_private_command_feedback(ctx, access_denied, silent=False)
        return

    authorized, expires_at, duration_days = repository.get_guild_subscription(
        ctx.guild.id
    )
    await send_private_command_feedback(
        ctx,
        "",
        embed=build_subscription_embed(
            authorized=authorized,
            expires_at=expires_at,
            duration_days=duration_days,
        ),
        view=SubscriptionStatusView(),
        delete_after=60,
    )


@bot.command(name="link")
async def support_link(ctx: commands.Context) -> None:
    """Post the support server URL as raw text so Discord can unfurl it."""
    await ctx.send(
        SUPPORT_SERVER_URL,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await delete_command_message(ctx)


def interaction_matches_scrim(
    interaction: discord.Interaction, scrim: Scrim, *, staff: bool = False
) -> bool:
    if (
        not is_active(scrim)
        or not repository.is_guild_authorized(scrim.guild_id)
        or interaction.guild is None
        or interaction.guild.id != scrim.guild_id
        or interaction.message is None
    ):
        return False
    if staff:
        return interaction.channel_id == scrim.staff_channel_id
    return (
        interaction.channel_id == scrim.public_channel_id
        and scrim.public_message_id is not None
        and interaction.message.id == scrim.public_message_id
    )


def interaction_in_public_channel(
    interaction: discord.Interaction, scrim: Scrim, board_message_id: int
) -> bool:
    """Validate follow-up ephemeral interactions without treating them as boards."""
    return (
        is_active(scrim)
        and repository.is_guild_authorized(scrim.guild_id)
        and interaction.guild is not None
        and interaction.guild.id == scrim.guild_id
        and interaction.channel_id == scrim.public_channel_id
        and scrim.public_message_id == board_message_id
    )


async def refresh_staff_slots_locked(
    scrim: Scrim, *, create: bool = False
) -> bool:
    if not is_active(scrim):
        return False
    try:
        channel = await configured_text_channel(scrim, scrim.staff_channel_id)
        if channel is None or not is_active(scrim):
            return False
        message = scrim.runtime_staff_message
        if message is None and scrim.staff_message_id is not None:
            try:
                message = await channel.fetch_message(scrim.staff_message_id)
            except discord.NotFound:
                message = None
        if message is None:
            if not create and scrim.staff_message_id is None:
                return False
            message = await channel.send(
                content=build_staff_slots_message(scrim),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if not is_active(scrim):
                return False
            if type(getattr(message, "id", None)) is not int:
                logger.error("Staff channel returned an invalid message reference.")
                return False
            with repository.transaction():
                if not is_active(scrim):
                    return False
                scrim.staff_message_id = message.id
            scrim.runtime_staff_message = message
            return True
        if not is_active(scrim):
            return False
        try:
            await message.edit(
                content=build_staff_slots_message(scrim),
                embed=None,
                view=None,
            )
        except (discord.NotFound, discord.Forbidden) as error:
            if (
                isinstance(error, discord.Forbidden)
                and getattr(error, "code", None) != 50005
            ):
                raise
            if not is_active(scrim):
                return False
            with repository.transaction():
                if not is_active(scrim):
                    return False
                scrim.staff_message_id = None
            message = await channel.send(
                content=build_staff_slots_message(scrim),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if not is_active(scrim):
                return False
            if type(getattr(message, "id", None)) is not int:
                logger.error("Staff channel returned an invalid message reference.")
                return False
            with repository.transaction():
                if not is_active(scrim):
                    return False
                scrim.staff_message_id = message.id
        if not is_active(scrim):
            return False
        scrim.runtime_staff_message = message
        return True
    except (discord.HTTPException, AttributeError):
        logger.exception("Staff mirror for scrim %s is inaccessible.", scrim.id)
        return False


async def refresh_public_slots(scrim: Scrim) -> bool:
    async with scrim.board_lock:
        return await refresh_public_slots_locked(scrim)


async def refresh_public_slots_locked(scrim: Scrim, *, create: bool = False) -> bool:
    if not is_active(scrim):
        return False
    try:
        channel = await configured_text_channel(scrim, scrim.public_channel_id)
        if channel is None or not is_active(scrim):
            return False
        message = scrim.runtime_message
        if message is None and scrim.public_message_id is not None:
            try:
                message = await channel.fetch_message(scrim.public_message_id)
            except discord.NotFound:
                message = None
        if message is None:
            if not create and scrim.public_message_id is None:
                return False
            if not is_active(scrim):
                return False
            message = await channel.send(
                content=build_slots_message(scrim), view=ManagerSlotView(scrim)
            )
            if not is_active(scrim):
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                return False
            with repository.transaction():
                if not is_active(scrim):
                    return False
                scrim.public_message_id = message.id
            scrim.runtime_message = message
            try:
                await refresh_staff_slots_locked(scrim, create=True)
            except SlotStorageError:
                logger.exception("Could not persist the staff mirror reference.")
            return True
        if not is_active(scrim) or message.id != scrim.public_message_id:
            return False
        try:
            await message.edit(
                content=build_slots_message(scrim),
                embed=None,
                view=ManagerSlotView(scrim),
            )
        except (discord.NotFound, discord.Forbidden) as error:
            if (
                isinstance(error, discord.Forbidden)
                and getattr(error, "code", None) != 50005
            ):
                raise
            if not is_active(scrim):
                return False
            with repository.transaction():
                if not is_active(scrim):
                    return False
                scrim.public_message_id = None
            message = await channel.send(
                content=build_slots_message(scrim), view=ManagerSlotView(scrim)
            )
            if not is_active(scrim):
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                return False
            with repository.transaction():
                if not is_active(scrim):
                    return False
                scrim.public_message_id = message.id
        if not is_active(scrim) or message.id != scrim.public_message_id:
            return False
        scrim.runtime_message = message
        try:
            await refresh_staff_slots_locked(scrim, create=True)
        except SlotStorageError:
            logger.exception("Could not persist the staff mirror reference.")
        return True
    except (discord.HTTPException, AttributeError):
        logger.exception("Scrim board %s is inaccessible.", scrim.id)
        return False


async def publish_scrim(scrim: Scrim) -> bool:
    """Recover, refresh, or create the configured board for one active scrim."""
    async with scrim.board_lock:
        return await refresh_public_slots_locked(scrim, create=True)


async def staff_channel(scrim: Scrim):
    try:
        channel = await configured_text_channel(scrim, scrim.staff_channel_id)
        return channel if channel is not None and is_active(scrim) else None
    except discord.HTTPException:
        logger.exception("Staff channel is inaccessible for scrim %s.", scrim.id)
        return None


async def send_scrim_log(
    scrim: Scrim,
    action: str,
    details: str,
) -> bool:
    """Send an immutable, timestamped audit entry to the configured logs channel."""
    config = repository.get_server_config(scrim.guild_id)
    logs_channel_id = (
        config.logs_channel_id
        if config is not None and config.logs_channel_id is not None
        else scrim.logs_channel_id
    )
    if logs_channel_id is None:
        return False
    try:
        channel = await configured_text_channel(scrim, logs_channel_id)
    except discord.HTTPException:
        logger.exception("Could not resolve scrim logs channel (%s).", scrim.id)
        return False
    if channel is None:
        return False
    timestamp = discord.utils.utcnow().astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    header = (
        f"**[{timestamp}] {action} — "
        f"{discord.utils.escape_markdown(scrim.name)}**\n"
    )
    detail_lines = details.splitlines() or [""]
    messages: list[str] = []
    current = header
    for line in detail_lines:
        candidate = f"{current}{line}\n"
        if len(candidate) > 1900 and current != header:
            messages.append(current.rstrip())
            current = f"{header}{line}\n"
        else:
            current = candidate
    if current != header:
        messages.append(current.rstrip())
    try:
        for message in messages:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return True
    except discord.HTTPException:
        logger.exception("Could not write scrim log (%s).", scrim.id)
        return False


def scrim_log_coverage_details(scrim: Scrim) -> str:
    pending_role_id = getattr(
        scrim, "pending_role_id", getattr(scrim, "manager_role_id", None)
    )
    confirmed_role_id = getattr(scrim, "confirmed_role_id", None)
    details = [
        "Coverage snapshot of the persisted configuration and assignments.",
        f"Public channel: <#{scrim.public_channel_id}>",
        f"Staff channel: <#{scrim.staff_channel_id}>",
        f"Captain management channel: "
        f"{f'<#{scrim.cap_channel_id}>' if scrim.cap_channel_id else 'not configured'}",
        f"Scrim logs channel: "
        f"{f'<#{scrim.logs_channel_id}>' if scrim.logs_channel_id else 'not configured'}",
        f"History channel: "
        f"{f'<#{scrim.history_channel_id}>' if scrim.history_channel_id else 'not configured'}",
        f"Staff role: "
        f"{f'<@&{scrim.staff_role_id}>' if scrim.staff_role_id else 'not configured'}",
        f"Pending Captain role: "
        f"{f'<@&{pending_role_id}>' if pending_role_id else 'not configured'}",
        f"Confirmed Captain role: "
        f"{f'<@&{confirmed_role_id}>' if confirmed_role_id else 'not configured'}",
        f"State: {'open' if scrim.is_open else 'closed'} · "
        f"slots {scrim.slot_start:02d}–{scrim.slot_end:02d}",
        f"Legend: {scrim.emoji_available} available · {scrim.emoji_reserved} reserved · "
        f"{scrim.emoji_pending} pending · {scrim.emoji_confirmed} confirmed",
    ]
    occupied = [
        slot
        for slot in scrim.slots.values()
        if not slot_is_assignable(slot)
    ]
    if not occupied:
        details.append("Assignments: none.")
    else:
        details.append("Assignments:")
        for slot in occupied:
            primary = (
                f"<@{slot.captain_1_id}>"
                if slot.captain_1_id is not None
                else "not specified"
            )
            co_captain = (
                f"<@{slot.captain_2_id}>"
                if slot.captain_2_id is not None
                else "none"
            )
            details.append(
                f"Slot {slot.number:02d} · team **{slot.team_name}** · "
                f"status {slot_status_label(slot)} · primary {primary} · "
                f"co-captain {co_captain}"
            )
    return "\n".join(details)


async def send_scrim_log_coverage_snapshot(scrim: Scrim) -> bool:
    if scrim.id in logs_coverage_snapshots_sent:
        return True
    sent = await send_scrim_log(
        scrim,
        "AUDIT COVERAGE SNAPSHOT",
        scrim_log_coverage_details(scrim),
    )
    if sent:
        logs_coverage_snapshots_sent.add(scrim.id)
    return sent


async def send_reset_history_snapshot(scrim: Scrim) -> bool:
    """Archive the final scrim state before a reset clears its slots."""
    if scrim.history_channel_id is None:
        return False
    try:
        channel = await configured_text_channel(scrim, scrim.history_channel_id)
    except discord.HTTPException:
        logger.exception("Could not resolve scrim history channel (%s).", scrim.id)
        return False
    if channel is None:
        return False
    timestamp = discord.utils.utcnow().astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    try:
        await channel.send(
            f"🏆 **Scrim Summary — {discord.utils.escape_markdown(scrim.name)}**\n"
            f"📅 {timestamp}\n\n"
            f"{build_slots_message(scrim)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except discord.HTTPException:
        logger.exception("Could not archive scrim state (%s).", scrim.id)
        return False


async def grant_manager_access(scrim: Scrim, manager: discord.Member) -> bool:
    """Assign the pending captain role and grant temporary board access."""
    role_granted = True
    if scrim.pending_role_id is not None:
        guild = getattr(manager, "guild", None)
        get_role = getattr(guild, "get_role", None)
        role = get_role(scrim.pending_role_id) if get_role is not None else None
        if role is None or not hasattr(manager, "add_roles"):
            role_granted = False
        else:
            try:
                await manager.add_roles(
                    role, reason=f"Pending captain assignment for {scrim.name}"
                )
            except discord.HTTPException:
                logger.exception("Could not assign pending captain role (%s).", scrim.id)
                role_granted = False
    try:
        channel = await configured_text_channel(scrim, scrim.public_channel_id)
    except discord.HTTPException:
        logger.exception("Could not resolve public scrim channel (%s).", scrim.id)
        return False
    if channel is None or not hasattr(channel, "set_permissions"):
        return False
    try:
        await channel.set_permissions(
            manager,
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            reason=f"Scrim manager access for {scrim.name}",
        )
        return role_granted
    except discord.HTTPException:
        logger.exception("Could not grant manager access (%s).", scrim.id)
        return False


def manager_has_active_assignment(scrim: Scrim, manager_id: int) -> bool:
    return any(
        manager_id in {slot.captain_1_id, slot.captain_2_id}
        and not slot_is_assignable(slot)
        for slot in scrim.slots.values()
    )


def captain_assignments(
    guild_id: int, user_id: int
) -> list[tuple[Scrim, Slot, int]]:
    assignments: list[tuple[Scrim, Slot, int]] = []
    for scrim in repository.list(guild_id):
        for slot in scrim.slots.values():
            if slot_is_assignable(slot):
                continue
            if slot.captain_1_id == user_id:
                assignments.append((scrim, slot, 1))
            elif slot.captain_2_id == user_id:
                assignments.append((scrim, slot, 2))
    return assignments


def captain_slot_id(scrim: Scrim, slot: Slot) -> str:
    """Return a stable, guild-local identifier for a scrim slot selection."""
    return f"{scrim.id}:{slot.number}"


CAP_CHANNEL_RESTRICTION_MESSAGE = (
    "❌ **Command restricted.** Please use the designated Cap Transfer "
    "channel for this command."
)
CAP_ROLE_RESTRICTION_MESSAGE = (
    "❌ **Command restricted.** You need the Pending or Confirmed Captain "
    "role for this scrim to use captain commands."
)


def cap_channel_is_allowed(
    guild_id: int, channel_id: int | None, scrim: Scrim | None = None
) -> bool:
    if scrim is not None:
        return scrim.cap_channel_id == channel_id
    return any(
        configured.cap_channel_id == channel_id
        for configured in repository.list(guild_id)
    )


def cap_role_is_allowed(
    member: object,
    guild_id: int,
    scrim: Scrim | None = None,
    channel_id: int | None = None,
) -> bool:
    """Allow captain commands to captain-status roles and staff."""
    if member_is_admin(member) or member_is_staff_in_guild(member, guild_id):
        return True
    roles = {
        getattr(role, "id", None) for role in getattr(member, "roles", ())
    }
    scrims = (
        [scrim]
        if scrim is not None
        else [
            configured
            for configured in repository.list(guild_id)
            if configured.cap_channel_id == channel_id
        ]
    )
    return any(
        configured is not None
        and bool(
            {
                role_id
                for role_id in (
                    getattr(configured, "pending_role_id", None),
                    getattr(configured, "confirmed_role_id", None),
                )
                if role_id is not None
            }.intersection(roles)
        )
        for configured in scrims
    )


async def require_cap_channel(
    ctx: commands.Context, scrim: Scrim | None = None
) -> bool:
    channel_id = getattr(ctx.channel, "id", None)
    allowed = cap_channel_is_allowed(ctx.guild.id, channel_id, scrim)
    if not allowed:
        await send_private_command_feedback(
            ctx,
            CAP_CHANNEL_RESTRICTION_MESSAGE,
            silent=False,
        )
        return False
    if not cap_role_is_allowed(
        ctx.author,
        ctx.guild.id,
        scrim,
        channel_id=channel_id,
    ):
        await send_private_command_feedback(
            ctx,
            CAP_ROLE_RESTRICTION_MESSAGE,
            silent=False,
        )
        return False
    return True


async def revoke_manager_access(
    scrim: Scrim,
    manager_id: int,
    *,
    member: discord.Member | None = None,
) -> bool:
    """Remove captain roles and temporary board access when unused."""
    try:
        channel = await configured_text_channel(scrim, scrim.public_channel_id)
    except discord.HTTPException:
        logger.exception("Could not resolve public scrim channel (%s).", scrim.id)
        return False
    if channel is None or not hasattr(channel, "set_permissions"):
        return False
    if member is None:
        guild = getattr(channel, "guild", None)
        if guild is None:
            return False
        get_member = getattr(guild, "get_member", None)
        member = get_member(manager_id) if get_member is not None else None
        if member is None:
            try:
                fetch_member = getattr(guild, "fetch_member", None)
                if fetch_member is None:
                    return False
                member = await fetch_member(manager_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.info(
                    "Manager %s is no longer resolvable in scrim %s.",
                    manager_id,
                    scrim.id,
                )
                return False
    role_removed = True
    role_ids = {
        role_id
        for role_id in (scrim.pending_role_id, scrim.confirmed_role_id)
        if role_id is not None
    }
    for role_id in role_ids:
        guild = getattr(member, "guild", None) or getattr(channel, "guild", None)
        get_role = getattr(guild, "get_role", None)
        role = get_role(role_id) if get_role is not None else None
        if role is None or not hasattr(member, "remove_roles"):
            role_removed = False
        else:
            try:
                await member.remove_roles(
                    role, reason=f"Remove released scrim captain role for {scrim.name}"
                )
            except discord.HTTPException:
                logger.exception("Could not remove captain role (%s).", scrim.id)
                role_removed = False
    try:
        await channel.set_permissions(
            member,
            overwrite=None,
            reason=f"Remove released scrim manager access for {scrim.name}",
        )
        return role_removed
    except discord.HTTPException:
        logger.exception("Could not revoke manager access (%s).", scrim.id)
        return False


async def confirm_captain_role(
    scrim: Scrim,
    manager_id: int,
    *,
    member: discord.Member | None = None,
) -> bool:
    """Swap a captain from the pending role to the confirmed role."""
    if member is None:
        guild = bot.get_guild(scrim.guild_id)
        if guild is None:
            return False
        member = guild.get_member(manager_id)
        if member is None:
            try:
                member = await guild.fetch_member(manager_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.info(
                    "Captain %s is no longer resolvable in scrim %s.",
                    manager_id,
                    scrim.id,
                )
                return False
    guild = getattr(member, "guild", None)
    get_role = getattr(guild, "get_role", None)
    confirmed_role = (
        get_role(scrim.confirmed_role_id)
        if get_role is not None and scrim.confirmed_role_id is not None
        else None
    )
    pending_role = (
        get_role(scrim.pending_role_id)
        if get_role is not None and scrim.pending_role_id is not None
        else None
    )
    try:
        if confirmed_role is not None and hasattr(member, "add_roles"):
            await member.add_roles(
                confirmed_role,
                reason=f"Confirmed captain assignment for {scrim.name}",
            )
        still_pending = any(
            manager_id in {slot.captain_1_id, slot.captain_2_id}
            and slot.status == STATUS_PENDING
            for slot in scrim.slots.values()
        )
        if not still_pending and pending_role is not None and hasattr(member, "remove_roles"):
            await member.remove_roles(
                pending_role,
                reason=f"Remove pending captain role for {scrim.name}",
            )
        return confirmed_role is not None
    except discord.HTTPException:
        logger.exception("Could not swap captain roles (%s).", scrim.id)
        return False


async def revoke_manager_access_if_unused(
    scrim: Scrim,
    manager_id: int | None,
    *,
    member: discord.Member | None = None,
) -> bool:
    if manager_id is None or manager_has_active_assignment(scrim, manager_id):
        return True
    return await revoke_manager_access(scrim, manager_id, member=member)


async def remove_public_controls(scrim: Scrim) -> bool:
    """Remove manager controls without replacing the board message."""
    try:
        channel = await configured_text_channel(scrim, scrim.public_channel_id)
        if channel is None:
            return False
        message = scrim.runtime_message
        if message is None and scrim.public_message_id is not None:
            try:
                message = await channel.fetch_message(scrim.public_message_id)
            except discord.NotFound:
                return False
        if message is None:
            return False
        clear_reactions = getattr(message, "clear_reactions", None)
        if clear_reactions is not None:
            await clear_reactions()
        await message.edit(view=None)
        return True
    except (discord.HTTPException, AttributeError):
        logger.exception("Could not remove public scrim controls (%s).", scrim.id)
        return False


class SlotReviewView(DurableView):
    def __init__(
        self,
        scrim: Scrim,
        slot: SlotSnapshot,
        *,
        registration_request: bool = False,
        registration_request_id: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.scrim = scrim
        self.slot = slot
        self.registration_request = registration_request
        self.registration_request_id = registration_request_id
        self.add_item(
            StaffActionButton(
                scrim.id,
                slot.number,
                slot.assignment_id,
                True,
                registration_request=registration_request,
            )
        )
        self.add_item(
            StaffActionButton(
                scrim.id,
                slot.number,
                slot.assignment_id,
                False,
                registration_request=registration_request,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        allowed = (
            interaction_matches_scrim(interaction, self.scrim, staff=True)
            and isinstance(member, discord.Member)
            and member_is_staff(member, self.scrim)
        )
        if not allowed:
            await interaction.response.send_message(
                "These buttons are for staff in the configured staff channel only.",
                ephemeral=True,
            )
        return allowed

    async def finish_review(self, interaction: discord.Interaction, confirm: bool) -> None:
        await interaction.response.defer(ephemeral=True)
        result: SlotSnapshot | None = None
        registration_member_id: int | None = None
        approved_registration: RegistrationRequest | None = None
        async with self.scrim.state_lock:
            if is_active(self.scrim):
                current = self.scrim.slots[self.slot.number]
                if self.registration_request:
                    requests = getattr(self.scrim, "pending_registrations", {})
                    request = requests.get(self.registration_request_id)
                    if (
                        request is not None
                        and current.assignment_id == request.assignment_id
                        and current.status == STATUS_AVAILABLE
                    ):
                        result = SlotSnapshot(
                            number=request.slot_number,
                            status=STATUS_PENDING,
                            team_name=request.team_name,
                            tag=request.tag,
                            manager_id=request.manager_id,
                            assignment_id=request.assignment_id,
                            captain_1_id=request.manager_id,
                        )
                        with repository.transaction():
                            requests.pop(request.request_id, None)
                            if confirm:
                                approved_registration = request
                                current.assignment_id += 1
                                current.status = STATUS_RESERVED
                                current.team_name = request.team_name
                                current.tag = request.tag
                                current.manager_id = request.manager_id
                                current.captain_1_id = request.manager_id
                                current.captain_2_id = None
                                result = current.snapshot()
                                registration_member_id = request.manager_id
                elif (
                    current.assignment_id == self.slot.assignment_id
                    and current.team_name == self.slot.team_name
                    and current.manager_id == self.slot.manager_id
                    and current.status == STATUS_PENDING
                ):
                    with repository.transaction():
                        result = current.snapshot() if not confirm else None
                        if confirm:
                            current.status = STATUS_CONFIRMED
                            result = current.snapshot()
                        else:
                            result = release_slot(current)
                    if not confirm and result is not None:
                        await revoke_manager_access_if_unused(
                            self.scrim, result.manager_id
                        )
        if result is None:
            disable_view_items(self)
            await interaction.followup.send(
                "This request is no longer active or the slot was reassigned.",
                ephemeral=True,
            )
            await delete_staff_review_message(interaction)
        else:
            access_ok = True
            if confirm and self.registration_request:
                guild = getattr(interaction, "guild", None) or bot.get_guild(
                    self.scrim.guild_id
                )
                member = None
                if guild is not None:
                    get_member = getattr(guild, "get_member", None)
                    member = (
                        get_member(registration_member_id)
                        if get_member is not None and registration_member_id is not None
                        else None
                    )
                    if member is None:
                        try:
                            fetch_member = getattr(guild, "fetch_member", None)
                            if fetch_member is not None and registration_member_id is not None:
                                member = await fetch_member(registration_member_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            member = None
                access_ok = member is not None and await grant_manager_access(
                    self.scrim, member
                )
            text = (
                (
                    f"✅ Team **{result.team_name}** has been reserved."
                    if self.registration_request
                    else f"🟢 Team **{result.team_name}** has been confirmed."
                )
                if confirm
                else f"Team **{result.team_name}** has been released."
            )
            if confirm and self.registration_request and not access_ok:
                text += " Pending Captain access could not be completed."
            if confirm and self.registration_request:
                if not await update_registration_reaction(
                    self.scrim, approved_registration
                ):
                    text += " The registration message reaction could not be updated."
            if (
                confirm
                and not self.registration_request
                and not await confirm_captain_role(
                    self.scrim, result.manager_id
                )
            ):
                text += " The confirmed captain role could not be updated."
            if confirm and not await refresh_public_slots(self.scrim):
                text += " The public board could not be updated."
            await send_scrim_log(
                self.scrim,
                (
                    "STAFF REGISTRATION APPROVAL"
                    if self.registration_request and confirm
                    else "STAFF VALIDATION"
                    if confirm
                    else "STAFF RELEASE"
                ),
                f"Slot {result.number:02d} · team **{result.team_name}** · "
                f"result {slot_status_emoji(self.scrim, result)} "
                f"{slot_status_label(result)}",
            )
            await interaction.followup.send(text, ephemeral=True)
            await delete_staff_review_message(interaction)


async def delete_staff_review_message(interaction: discord.Interaction) -> bool:
    message = getattr(interaction, "message", None)
    delete = getattr(message, "delete", None)
    if delete is None:
        return False
    try:
        await delete()
        return True
    except (discord.NotFound, discord.Forbidden):
        return False
    except discord.HTTPException:
        logger.exception("Could not delete the processed staff review message.")
        return False


async def update_registration_reaction(
    scrim: Scrim, request: RegistrationRequest | None
) -> bool:
    if request is None or request.registration_message_id is None:
        return False
    registration_channel_id = getattr(scrim, "registration_channel_id", None)
    if registration_channel_id is None:
        return False
    try:
        channel = await configured_text_channel(
            scrim, registration_channel_id
        )
        if channel is None:
            return False
        message = await channel.fetch_message(request.registration_message_id)
        await message.add_reaction("✅")
        bot_user = getattr(bot, "user", None)
        if bot_user is not None:
            await message.remove_reaction("🆗", bot_user)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        logger.exception(
            "Could not update the registration reaction (%s).", scrim.id
        )
        return False


async def notify_staff_for_review(scrim: Scrim, slot: SlotSnapshot) -> bool:
    if getattr(scrim, "staff_channel_id", None) is None:
        return False
    channel = await staff_channel(scrim)
    if channel is None or not is_active(scrim):
        return False
    try:
        await channel.send(
            f"**{slot.team_name}**\nconfirmed",
            view=SlotReviewView(scrim, slot),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except discord.HTTPException:
        logger.exception("Could not send the staff notification (%s).", scrim.id)
        return False


async def notify_staff_for_registration(
    scrim: Scrim, request: RegistrationRequest | None
) -> bool:
    """Send a staff-only review message for a public team registration."""
    if request is None:
        return False
    channel = await staff_channel(scrim)
    if channel is None or not is_active(scrim):
        return False
    try:
        await channel.send(
            f"📝 **Team registration request**\n"
            f"**{discord.utils.escape_markdown(request.team_name)}** · "
            f"Slot {request.slot_number:02d}\n"
            f"Captain: <@{request.manager_id}>",
            view=SlotReviewView(
                scrim,
                SlotSnapshot(
                    number=request.slot_number,
                    status=STATUS_PENDING,
                    team_name=request.team_name,
                    tag=request.tag,
                    manager_id=request.manager_id,
                    assignment_id=request.assignment_id,
                    captain_1_id=request.manager_id,
                ),
                registration_request=True,
                registration_request_id=request.request_id,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except discord.HTTPException:
        logger.exception(
            "Could not send the registration review (%s).", scrim.id
        )
        return False


async def notify_staff_of_manager_action(
    scrim: Scrim, slot: SlotSnapshot, emoji: str, label: str
) -> bool:
    channel = await staff_channel(scrim)
    if channel is None or not is_active(scrim):
        return False
    status = "confirmed" if slot.status == STATUS_PENDING else "released"
    try:
        await channel.send(
            f"**{slot.team_name}**\n{status}",
            view=SlotReviewView(scrim, slot) if slot.status == STATUS_PENDING else None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except discord.HTTPException:
        logger.exception("Could not log the manager action (%s).", scrim.id)
        return False


class ManagerSlotView(DurableView):
    def __init__(self, scrim: Scrim) -> None:
        super().__init__(timeout=None)
        self.scrim = scrim
        self.assignments = {
            slot.number: slot.snapshot() for slot in scrim.slots.values()
            if not slot_is_assignable(slot)
        }
        self.fingerprint = assignment_fingerprint(scrim)
        self.add_item(
            ManagerActionButton(scrim.id, self.fingerprint, True, enabled=scrim.is_open)
        )
        self.add_item(
            ManagerActionButton(scrim.id, self.fingerprint, False, enabled=scrim.is_open)
        )

    async def finish_manager_action(
        self,
        interaction: discord.Interaction,
        confirm: bool,
        assignment: SlotSnapshot | None = None,
        cancellation_confirmed: bool = False,
        expected_board_id: int | None = None,
    ) -> None:
        if not (
            interaction_matches_scrim(interaction, self.scrim)
            if assignment is None
            else (
                expected_board_id is not None
                and interaction_in_public_channel(
                    interaction, self.scrim, expected_board_id
                )
            )
        ):
            await interaction.response.send_message(
                "This action is not valid for this board.", ephemeral=True
            )
            return
        if not self.scrim.is_open:
            await interaction.response.send_message(
                "Manager interactions are currently closed. Please wait for staff to reopen this scrim.",
                ephemeral=True,
            )
            return
        candidates = [
            item for item in self.assignments.values()
            if item.manager_id == interaction.user.id
        ]
        if assignment is None and len(candidates) > 1:
            select = discord.ui.Select(
                placeholder="Choose your slot",
                options=[
                    discord.SelectOption(
                        label=f"{item.number:02d} — {item.team_name}"[:100],
                        value=str(item.number),
                    ) for item in candidates
                ],
            )
            view = DurableView(timeout=60)
            actor_id = interaction.user.id
            board_message_id = interaction.message.id

            async def selected(choice: discord.Interaction) -> None:
                if (
                    choice.user.id != actor_id
                    or not interaction_in_public_channel(
                        choice, self.scrim, board_message_id
                    )
                ):
                    await choice.response.send_message(
                        "This selection is no longer available.", ephemeral=True
                    )
                    return
                picked = next(x for x in candidates if str(x.number) == select.values[0])
                await self.finish_manager_action(
                    choice,
                    confirm,
                    picked,
                    expected_board_id=board_message_id,
                )

            select.callback = selected
            view.add_item(select)
            await interaction.response.send_message(
                "Choose the slot you want to manage:", view=view, ephemeral=True
            )
            return
        if assignment is None and not candidates:
            await interaction.response.send_message(
                "You have no assigned slot on this board.", ephemeral=True
            )
            return
        assignment = assignment or candidates[0]
        result: SlotSnapshot | None = None
        if confirm:
            already_confirmed = False
            async with self.scrim.state_lock:
                current = self.scrim.slots.get(assignment.number)
                valid = (
                    is_active(self.scrim)
                    and current is not None
                    and current.assignment_id == assignment.assignment_id
                    and current.manager_id == interaction.user.id
                )
                if valid and current.status in {STATUS_PENDING, STATUS_CONFIRMED}:
                    already_confirmed = True
                elif valid and current.status == STATUS_RESERVED:
                    with repository.transaction():
                        current.status = STATUS_PENDING
                        result = current.snapshot()
            if already_confirmed:
                await interaction.response.send_message(
                    "⏳ You have already confirmed your slot. "
                    "Please wait for the staff to validate it.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
        else:
            if not cancellation_confirmed:
                async with self.scrim.state_lock:
                    current = self.scrim.slots.get(assignment.number)
                    valid = (
                        is_active(self.scrim)
                        and current is not None
                        and current.assignment_id == assignment.assignment_id
                        and current.manager_id == interaction.user.id
                        and not slot_is_assignable(current)
                    )
                if not valid:
                    await interaction.response.send_message(
                        "This slot is no longer assigned to you. Please use the current board.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.send_message(
                    f"Are you sure you want to cancel slot **{assignment.number:02d}** "
                    f"for **{discord.utils.escape_markdown(assignment.team_name)}**?\n"
                    "Your slot will be released immediately and may be assigned to another team.\n"
                    "Choose within 60 seconds. No action will be taken if this request expires.",
                    view=CancelConfirmationView(
                        self,
                        assignment,
                        expected_board_id or interaction.message.id,
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.defer(ephemeral=True)

        if not confirm:
            async with self.scrim.state_lock:
                if is_active(self.scrim):
                    current = self.scrim.slots.get(assignment.number)
                    if (
                        current is not None
                        and current.assignment_id == assignment.assignment_id
                        and current.manager_id == interaction.user.id
                        and not slot_is_assignable(current)
                    ):
                        with repository.transaction():
                            result = release_slot(current)
                        if result is not None:
                            await revoke_manager_access_if_unused(
                                self.scrim,
                                result.manager_id,
                                member=(
                                    interaction.user
                                    if isinstance(interaction.user, discord.Member)
                                    else None
                                ),
                            )
        if result is None:
            await interaction.followup.send(
                "This request was already processed or the slot was reassigned. "
                "Please check the current board.",
                ephemeral=True,
            )
            return
        if confirm:
            emoji, label = "✅", "Confirmation"
            text = f"🟠 Slot **{result.number:02d}**: Pending — awaiting staff approval."
        else:
            emoji, label = "❌", "Release / Decline"
            text = f"Slot **{result.number:02d}** has been released and is available."
        updated, notified = await asyncio.gather(
            refresh_public_slots(self.scrim),
            notify_staff_of_manager_action(self.scrim, result, emoji, label),
        )
        if not updated:
            text += " The public board could not be updated."
        if not notified:
            text += " Staff could not be notified."
        await send_scrim_log(
            self.scrim,
            "MANAGER CONFIRMATION" if confirm else "MANAGER SLOT RELEASE",
            f"Slot {result.number:02d} · team **{result.team_name}** · "
            f"result {slot_status_emoji(self.scrim, result)} "
            f"{slot_status_label(result)}",
        )
        await interaction.followup.send(text, ephemeral=True)


class CancelConfirmationView(DurableView):
    def __init__(
        self,
        manager_view: ManagerSlotView,
        assignment: SlotSnapshot,
        board_message_id: int,
    ):
        super().__init__(timeout=60)
        self.manager_view = manager_view
        self.scrim = manager_view.scrim
        self.assignment = assignment
        self.board_message_id = board_message_id
        self.completed = False

    async def interaction_check(self, interaction):
        allowed = (
            interaction.user.id == self.assignment.manager_id
            and interaction_in_public_channel(
                interaction, self.scrim, self.board_message_id
            )
        )
        if not allowed:
            await interaction.response.send_message(
                "Only the assigned manager can answer this active request.", ephemeral=True
            )
        return allowed

    async def on_timeout(self):
        self.completed = True
        disable_view_items(self)

    @discord.ui.button(label="Yes, cancel my slot", style=discord.ButtonStyle.danger)
    async def accept(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        if self.completed:
            await interaction.response.send_message(
                "This request is no longer active. Please use the current board.",
                ephemeral=True,
            )
            return
        self.completed = True
        await self.manager_view.finish_manager_action(
            interaction,
            False,
            self.assignment,
            cancellation_confirmed=True,
            expected_board_id=self.board_message_id,
        )
        disable_view_items(self)
        self.stop()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Keep my slot", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        if self.completed:
            await interaction.response.send_message(
                "This request was already processed.", ephemeral=True
            )
            return
        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.edit_message(
            content="Cancellation dismissed. No changes were made to your slot.",
            view=self,
        )


class ManagerActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"slots:manager:(?P<scrim>[a-f0-9]{16}):(?P<action>confirm|cancel):(?P<generation>[a-f0-9]{24})",
):
    def __init__(
        self, scrim_id: str, generation: str, confirm: bool, *, enabled: bool = True
    ):
        self.scrim_id, self.generation, self.confirm = scrim_id, generation, confirm
        super().__init__(discord.ui.Button(
            label="Confirm" if confirm else "Cancel",
            style=discord.ButtonStyle.success if confirm else discord.ButtonStyle.danger,
            emoji="✅" if confirm else "❌",
            disabled=not enabled,
            custom_id=f"slots:manager:{scrim_id}:{'confirm' if confirm else 'cancel'}:{generation}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["scrim"], match["generation"], match["action"] == "confirm")

    async def callback(self, interaction):
        scrim = repository.get(self.scrim_id)
        if scrim is None:
            await interaction.response.send_message(
                "This scrim no longer exists.", ephemeral=True
            )
            return
        view = ManagerSlotView(scrim)
        try:
            if not interaction_matches_scrim(interaction, scrim):
                await interaction.response.send_message(
                    "This button does not belong to this board.", ephemeral=True
                )
            elif self.generation != view.fingerprint:
                await interaction.response.send_message(
                    "This button has expired. Please use the updated board.", ephemeral=True
                )
            elif not scrim.is_open:
                await interaction.response.send_message(
                    "Manager interactions are currently closed. Please wait for staff to reopen this scrim.",
                    ephemeral=True,
                )
            else:
                await view.finish_manager_action(interaction, self.confirm)
        except Exception as error:
            await view.on_error(interaction, error, self)


async def perform_scrim_reset(scrim: Scrim, invoking_channel) -> str:
    """Archive and reset one scrim after confirmation has been authorized."""
    async with scrim.board_lock:
        if not is_active(scrim):
            return "This scrim no longer exists. Nothing was reset."
        async with scrim.state_lock:
            if not is_active(scrim):
                return "This scrim no longer exists. Nothing was reset."
            if not await send_reset_history_snapshot(scrim):
                logger.warning(
                    "Could not archive the final state before resetting scrim %s.",
                    scrim.id,
                )
            await clear_active_idpw(scrim.id)
            manager_ids = {
                slot.manager_id
                for slot in scrim.slots.values()
                if slot.manager_id is not None
            }
            with repository.transaction():
                for slot in scrim.slots.values():
                    slot.clear()
                scrim.pending_registrations.clear()
                scrim.current_match_counter = 1
        for manager_id in manager_ids:
            await revoke_manager_access_if_unused(scrim, manager_id)
        await send_scrim_log(scrim, "SCRIM RESET", "All slots were reset by staff.")
        if not is_active(scrim):
            return "This scrim no longer exists. The reset was stopped."

        channels_to_clear = []
        registration_channel_id = getattr(scrim, "registration_channel_id", None)
        for channel_id in (
            scrim.staff_channel_id,
            scrim.public_channel_id,
            registration_channel_id,
        ):
            if channel_id is None:
                continue
            channel = (
                invoking_channel
                if invoking_channel is not None and invoking_channel.id == channel_id
                else None
            )
            if channel is None:
                try:
                    channel = await configured_text_channel(scrim, channel_id)
                except discord.HTTPException:
                    logger.exception(
                        "Could not resolve scrim channel (%s, %s).",
                        scrim.id,
                        channel_id,
                    )
            if channel is not None and all(
                existing.id != channel.id for existing in channels_to_clear
            ):
                channels_to_clear.append(channel)

        channel_clear_results = {}
        for channel in channels_to_clear:
            if not is_active(scrim):
                return "This scrim no longer exists. The reset was stopped."
            channel_clear_results[channel.id] = await purge_channel_messages(channel)

        public_cleared = channel_clear_results.get(scrim.public_channel_id, False)
        if public_cleared:
            scrim.runtime_message = None
        staff_cleared = channel_clear_results.get(scrim.staff_channel_id, False)
        if staff_cleared:
            scrim.runtime_staff_message = None

        updated = await refresh_public_slots_locked(scrim, create=True)
        if not is_active(scrim):
            return "This scrim no longer exists. The reset was stopped."

        channels_cleared = all(
            channel_clear_results.get(channel_id, False)
            for channel_id in (
                scrim.staff_channel_id,
                scrim.public_channel_id,
                scrim.registration_channel_id,
            )
            if channel_id is not None
        )
        if updated and channels_cleared:
            cleared_channel_label = "staff and public"
            if registration_channel_id is not None:
                cleared_channel_label += ", and registration"
            return (
                f"✅ All slots are now available and the {cleared_channel_label} "
                "channels were cleared (pinned messages were kept)."
            )
        if updated:
            return (
                "✅ All slots are now available and the public board was updated, "
                "but one or more scrim channels could not be cleared."
            )
        return (
            "✅ All slots are now available, but the public board could not be updated. "
            "Please use `!update` to refresh it."
        )


class ResetConfirmationView(DurableView):
    def __init__(self, scrim: Scrim, invoking_channel_id: int):
        super().__init__(timeout=60)
        self.scrim = scrim
        self.scrim_id = scrim.id
        self.guild_id = scrim.guild_id
        self.invoking_channel_id = invoking_channel_id
        self.message = None
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        current = repository.get(self.scrim_id)
        allowed = (
            current is self.scrim
            and is_active(self.scrim)
            and interaction.guild is not None
            and interaction.guild.id == self.guild_id
            and interaction.channel_id == self.invoking_channel_id
            and interaction.message is not None
            and (
                self.message is None
                or interaction.message.id == self.message.id
            )
            and member_is_staff(interaction.user, self.scrim)
        )
        if not allowed:
            text = (
                "Only an authorized Staff member can confirm this reset "
                "in the original scrim channel."
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                logger.exception("Could not reject an invalid reset confirmation.")
        return allowed

    async def on_timeout(self) -> None:
        if self.completed:
            return
        self.completed = True
        disable_view_items(self)
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Reset confirmation expired. No slots were changed.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Confirm Reset",
        emoji="⚠️",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_reset(self, interaction: discord.Interaction, button) -> None:
        if not await self.interaction_check(interaction):
            return
        if self.completed:
            await interaction.response.send_message(
                "This reset confirmation is no longer active.", ephemeral=True
            )
            return
        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.defer()
        result = await perform_scrim_reset(self.scrim, interaction.channel)
        if self.message is not None:
            try:
                await self.message.edit(content=result, view=self)
            except discord.NotFound:
                # Reset purges the invoking channel, so the confirmation message
                # may have been deleted before the result can be written to it.
                try:
                    await interaction.followup.send(result, ephemeral=True)
                except discord.HTTPException:
                    logger.exception("Could not send the reset result privately.")
            except discord.HTTPException:
                logger.exception("Could not update the reset confirmation message.")

    @discord.ui.button(
        label="Cancel",
        emoji="❌",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_reset(self, interaction: discord.Interaction, button) -> None:
        if not await self.interaction_check(interaction):
            return
        if self.completed:
            await interaction.response.send_message(
                "This reset confirmation is no longer active.", ephemeral=True
            )
            return
        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.edit_message(
            content="Reset cancelled. No slots were changed.",
            view=self,
        )


@bot.command(name="reset")
@commands.guild_only()
async def reset_slots(ctx: commands.Context) -> None:
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return
    confirmation = ResetConfirmationView(scrim, ctx.channel.id)
    reset_channel_label = "staff and public"
    if getattr(scrim, "registration_channel_id", None) is not None:
        reset_channel_label += ", and registration"
    confirmation_message = await send_private_command_feedback(
        ctx,
        (
            f"⚠️ Reset **{discord.utils.escape_markdown(scrim.name)}**? "
            f"This clears every slot and the configured {reset_channel_label} "
            "channels, keeping pinned messages. "
            "Click **Confirm Reset** to continue."
        ),
        view=confirmation,
        delete_after=60,
    )
    confirmation.message = confirmation_message


@bot.command(name="open", aliases=["o"])
@commands.guild_only()
async def open_scrim(ctx: commands.Context) -> None:
    """Open managers or registrations based on the configured command channel."""
    registration_scrim = resolve_registration_scrim(ctx)
    if registration_scrim is not None:
        if await require_registration_staff_channel(ctx) is None:
            return
        if not await set_registration_channel_open(ctx, registration_scrim, True):
            await send_private_command_feedback(
                ctx,
                "The registration channel could not be opened. "
                "Check the configured registration role and channel permissions.",
                silent=False,
            )
            return
        await delete_command_message(ctx)
        await send_scrim_log(
            registration_scrim,
            "REGISTRATIONS OPENED",
            "The configured registration role can now send messages.",
        )
        await send_operational_message(
            ctx, registration_scrim, "open_registration"
        )
        return
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return
    async with scrim.state_lock:
        if not is_active(scrim):
            await send_private_command_feedback(
                ctx, "This scrim no longer exists.", silent=True
            )
            return
        with repository.transaction():
            scrim.is_open = True
    await refresh_public_slots(scrim)
    await delete_command_message(ctx)
    await send_scrim_log(scrim, "SCRIM OPENED", "Manager interactions were opened.")
    await send_operational_message(ctx, scrim, "open_slots")


@bot.command(name="close", aliases=["c"])
@commands.guild_only()
async def close_scrim(ctx: commands.Context) -> None:
    """Close managers or registrations based on the configured command channel."""
    registration_scrim = resolve_registration_scrim(ctx)
    if registration_scrim is not None:
        if await require_registration_staff_channel(ctx) is None:
            return
        if not await set_registration_channel_open(ctx, registration_scrim, False):
            await send_private_command_feedback(
                ctx,
                "The registration channel could not be closed. "
                "Check the configured registration role and channel permissions.",
                silent=False,
            )
            return
        await delete_command_message(ctx)
        await send_scrim_log(
            registration_scrim,
            "REGISTRATIONS CLOSED",
            "The configured registration role can no longer send messages.",
        )
        await send_operational_message(
            ctx, registration_scrim, "close_registration"
        )
        return
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return
    async with scrim.state_lock:
        if not is_active(scrim):
            await send_private_command_feedback(
                ctx, "This scrim no longer exists.", silent=True
            )
            return
        with repository.transaction():
            scrim.is_open = False
    await remove_public_controls(scrim)
    await send_scrim_log(scrim, "SCRIM CLOSED", "Manager interactions were closed.")
    await delete_command_message(ctx)
    await send_operational_message(ctx, scrim, "close_slots")


@bot.command(name="idpw")
@commands.guild_only()
async def distribute_idpw(
    ctx: commands.Context, *, input_string: str
) -> None:
    parsed = await _parse_idpw_input(ctx, input_string, "!idpw")
    if parsed is None:
        return
    scrim, config, room_id, minutes, password = parsed
    await _publish_idpw(
        ctx,
        room_id,
        minutes,
        password=password,
        scrim=scrim,
        config=config,
    )


async def _parse_idpw_input(
    ctx: commands.Context,
    input_string: str,
    command_name: str,
) -> tuple[Scrim, object, str, int, str] | None:
    """Parse slash-delimited ID/PW arguments after loading the active scrim."""
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return None
    config = repository.get_idpw_config(scrim.id)
    if config is None:
        await send_private_command_feedback(
            ctx,
            "ID/password distribution is not configured. "
            "Configure ID/PW in `!setup` first.",
        )
        return None

    parts = [part.strip() for part in input_string.split("/")]
    dynamic = str(getattr(scrim, "pw_type", "fixed")).casefold() == "dynamic"
    expected = 3 if dynamic else 2
    if dynamic:
        format_text = (
            f"❌ Format: `{command_name} <room_id> / <password> / <minutes>`"
        )
    else:
        format_text = f"❌ Format: `{command_name} <room_id> / <minutes>`"
    if len(parts) != expected or any(not part for part in parts):
        await send_private_command_feedback(ctx, format_text)
        return None

    try:
        minutes = int(parts[-1])
    except ValueError:
        await send_private_command_feedback(
            ctx, "❌ Minutes must be a whole number."
        )
        return None
    if minutes < 1:
        await send_private_command_feedback(
            ctx, "❌ Minutes must be at least 1."
        )
        return None

    room_id = parts[0]
    password = (
        parts[1]
        if dynamic
        else getattr(scrim, "fixed_pw", "") or config.fixed_password
    )
    return scrim, config, room_id, minutes, password


def _format_idpw_announcement(
    *,
    match_number: int,
    room_id: str,
    password: str,
    start_time: str,
    confirmed_role_id: int | None,
    map_name: str | None = None,
    start_label: str = "Start",
    separate_role_mention: bool = False,
) -> str:
    """Build the Discord-formatted ID/password announcement."""
    detail_lines = []
    if map_name:
        detail_lines.append(f"Map : {discord.utils.escape_markdown(map_name)}")
    detail_lines.extend(
        (
            f"ID : `{discord.utils.escape_markdown(room_id)}`",
            f"PW : {discord.utils.escape_markdown(password)}",
            f"{start_label} : {start_time}",
        )
    )
    lines = [f"# __**Match {match_number}**__"]
    if separate_role_mention:
        lines.append("")
    lines.append(f"**{chr(10).join(detail_lines)}**")
    if confirmed_role_id is not None:
        if separate_role_mention:
            lines.append("")
        lines.append(f"<@&{confirmed_role_id}>")
    return "\n".join(lines)


async def _publish_idpw(
    ctx: commands.Context,
    lobby_id: str,
    minutes: int,
    requested_match: int | None = None,
    *,
    password: str | None = None,
    scrim: Scrim | None = None,
    config: object | None = None,
) -> None:
    """Publish ID/password details for the current or requested match."""
    if scrim is None:
        scrim = await require_staff_scrim(ctx, allow_public=True)
        if scrim is None:
            return
    if config is None:
        config = repository.get_idpw_config(scrim.id)
    if config is None:
        await send_private_command_feedback(
            ctx,
            "ID/password distribution is not configured. "
            "A member with the configured global Staff role must open `!setup`, "
            f"select **{discord.utils.escape_markdown(scrim.name)}**, and use **ID/PW** first.",
        )
        return
    if getattr(scrim, "pw_type", "fixed") == "dynamic" and password is None:
        await send_private_command_feedback(
            ctx,
            "This scrim uses DYNAMIC password mode. Use "
            "`!idpw <room_id> / <password> / <minutes>`.",
        )
        return
    if minutes < 1:
        await send_private_command_feedback(
            ctx, "The number of minutes must be at least 1."
        )
        return
    if scrim.confirmed_role_id is None:
        await send_private_command_feedback(
            ctx,
            "This scrim has no Confirmed Captain Role configured. "
            "Edit the scrim in `!setup` first.",
        )
        return

    target_channel = await configured_text_channel(scrim, config.target_channel_id)
    if target_channel is None:
        await send_private_command_feedback(
            ctx,
            "The configured ID/password channel could not be found or used.",
        )
        return

    await clear_active_idpw(scrim.id)
    start_timestamp = int(time.time()) + (minutes * 60)
    local_start = datetime.fromtimestamp(
        start_timestamp, tz=timezone_for_name(config.timezone_name)
    )
    heure_formatee = local_start.strftime("%H:%M")
    match_number = requested_match or scrim.current_match_counter
    if password is None:
        password = getattr(scrim, "fixed_pw", "") or config.fixed_password
    message_content = _format_idpw_announcement(
        match_number=match_number,
        room_id=lobby_id,
        password=password,
        start_time=heure_formatee,
        confirmed_role_id=scrim.confirmed_role_id,
        map_name=_match_map_for_scrim(scrim, match_number),
    )
    role_mentions = discord.AllowedMentions(
        everyone=False, users=False, roles=True, replied_user=False
    )
    try:
        message = await target_channel.send(
            content=message_content,
            allowed_mentions=role_mentions,
        )
        repository.set_idpw_announcement(scrim.id, message.id)
        if requested_match is None:
            with repository.transaction():
                scrim.current_match_counter = (
                    scrim.current_match_counter % scrim.max_matches
                ) + 1
        state = {"message": message, "messages": [message], "tasks": []}
        active_idpw[scrim.id] = state
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not publish MATCH ACCESS for scrim %s.", scrim.id)
        await send_private_command_feedback(
            ctx,
            "The MATCH ACCESS message could not be sent to the configured channel.",
        )
        return

    async def schedule_alerts() -> None:
        try:
            if minutes > 3:
                await asyncio.sleep((minutes - 3) * 60)
                reminder = await target_channel.send(
                    content=_format_idpw_reminder(
                        title="The match starts in 3 minutes",
                        message="Please prepare.",
                        confirmed_role_id=scrim.confirmed_role_id,
                    ),
                    allowed_mentions=role_mentions,
                )
                if not _track_active_idpw_message(scrim.id, state, reminder):
                    await reminder.delete()
                await asyncio.sleep(2 * 60)
            elif minutes > 1:
                await asyncio.sleep((minutes - 1) * 60)
            if minutes >= 1:
                reminder = await target_channel.send(
                    content=_format_idpw_reminder(
                        title="FINAL CALL",
                        message="The match begins in 1 minute.",
                        confirmed_role_id=scrim.confirmed_role_id,
                    ),
                    allowed_mentions=role_mentions,
                )
                if not _track_active_idpw_message(scrim.id, state, reminder):
                    await reminder.delete()
        except asyncio.CancelledError:
            raise
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not send MATCH ACCESS alert.")

    task = bot.loop.create_task(schedule_alerts())
    active_idpw[scrim.id]["tasks"].append(task)
    await delete_command_message(ctx)


def _match_map_for_scrim(scrim: Scrim, game_number: int) -> str | None:
    match_maps = getattr(scrim, "match_maps", None)
    maps = getattr(scrim, "maps", ()) if match_maps is None else match_maps
    if isinstance(maps, str):
        maps = [
            item.strip()
            for item in maps.strip("[]").split(",")
            if item.strip()
        ]
    if not isinstance(maps, (list, tuple)):
        return None
    index = game_number - 1
    if not 0 <= index < len(maps):
        return None
    map_name = maps[index]
    return map_name.strip() if isinstance(map_name, str) and map_name.strip() else None


async def _publish_idpwg(
    ctx: commands.Context,
    game_number: int,
    input_string: str,
) -> None:
    """Publish one configured match using FIXED or DYNAMIC password arguments."""
    command_name = f"!idpwg{game_number}"
    parsed = await _parse_idpw_input(ctx, input_string, command_name)
    if parsed is None:
        return
    scrim, config, room_id, minutes, password = parsed

    target_channel = await configured_text_channel(scrim, config.target_channel_id)
    if target_channel is None:
        await send_private_command_feedback(
            ctx,
            "The configured ID/password channel could not be found or used.",
        )
        return

    await clear_active_idpw(scrim.id)
    start_timestamp = int(time.time()) + minutes * 60
    configured_timezone = getattr(scrim, "timezone", config.timezone_name)
    local_start = datetime.fromtimestamp(
        start_timestamp,
        tz=timezone_for_name(configured_timezone),
    )
    map_name = _match_map_for_scrim(scrim, game_number)
    message_content = _format_idpw_announcement(
        match_number=game_number,
        room_id=room_id,
        password=password,
        start_time=local_start.strftime("%H:%M"),
        confirmed_role_id=scrim.confirmed_role_id,
        map_name=map_name,
        start_label="Start Time",
        separate_role_mention=True,
    )
    allowed_mentions = discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=True,
        replied_user=False,
    )
    try:
        message = await target_channel.send(
            content=message_content,
            allowed_mentions=allowed_mentions,
        )
        repository.set_idpw_announcement(scrim.id, message.id)
        with repository.transaction():
            scrim.current_match_counter = (
                game_number + 1
                if game_number < scrim.max_matches
                else 1
            )
        state = {"message": message, "messages": [message], "tasks": []}
        active_idpw[scrim.id] = state
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not publish MATCH ACCESS for scrim %s.", scrim.id)
        await send_private_command_feedback(
            ctx,
            "The MATCH ACCESS message could not be sent to the configured channel.",
        )
        return
    await delete_command_message(ctx)


def _register_specific_idpw_commands() -> None:
    for match_number in range(1, MAX_MATCHES + 1):
        def make_specific_idpw_handler(game_number: int):
            async def specific_idpw(
                ctx: commands.Context,
                *,
                input_string: str,
            ) -> None:
                await _publish_idpwg(ctx, game_number, input_string)

            return specific_idpw

        specific_idpw = make_specific_idpw_handler(match_number)
        specific_idpw.__name__ = f"distribute_idpwg{match_number}"
        bot.command(name=f"idpwg{match_number}")(specific_idpw)


_register_specific_idpw_commands()


async def _record_match_scores(
    ctx: commands.Context,
    match_number: int,
    input_string: str,
) -> None:
    scrim = await require_staff_scrim(ctx)
    if scrim is None:
        return
    if match_number > scrim.max_matches:
        await send_private_command_feedback(
            ctx,
            f"❌ Match {match_number} is not configured for this scrim. "
            f"The match limit is {scrim.max_matches}.",
            silent=False,
        )
        return
    if match_number < 1:
        await send_private_command_feedback(
            ctx,
            "❌ Match number must be at least 1.",
            silent=False,
        )
        return
    progress_message: discord.Message | None = None
    try:
        try:
            progress_message = await ctx.send(
                "⏳ Preparing the score review…",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not show score-review progress for scrim %s.",
                scrim.id,
            )

        async def update_prompt(
            content: str,
            *,
            embed: discord.Embed | None = None,
            view: discord.ui.View | None = None,
        ) -> discord.Message | None:
            if progress_message is not None:
                try:
                    await progress_message.edit(
                        content=content,
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return progress_message
                except discord.HTTPException:
                    logger.exception(
                        "Could not update score review for scrim %s.",
                        scrim.id,
                    )
            try:
                return await ctx.send(
                    content,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception(
                    "Could not send score review for scrim %s.",
                    scrim.id,
                )
                return None

        normalized_input = normalize_score_submission_text(
            input_string,
            match_number,
        )
        try:
            scores = parse_match_score_lines(
                normalized_input,
                match_number=match_number,
                scrim=scrim,
            )
        except ValueError as error:
            await update_prompt(f"❌ {error} Nothing was saved.")
            return
        if not scores:
            await update_prompt(
                f"❌ No valid scores found for Match {match_number}. "
                "Use one `slot kills` entry per rank, best team first. "
                "Nothing was saved."
            )
            return

        review = MatchScoreSubmissionReviewView(
            owner_id=ctx.author.id,
            guild_id=scrim.guild_id,
            channel_id=ctx.channel.id,
            scrim=scrim,
            match_number=match_number,
            scores=scores,
            raw_input=normalized_input,
        )
        review.message = await update_prompt(
            "Review the rank order and kills below. **Nothing has been saved.**",
            embed=review.embed(),
            view=review,
        )
    finally:
        await delete_command_message(ctx)


def normalize_score_submission_text(
    input_string: str,
    match_number: int,
) -> str:
    """Allow the edit form to accept either score lines or a full !resgN command."""
    lines = str(input_string).splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        command = re.fullmatch(
            rf"!?resg{match_number}(?:\s+(.*))?",
            line.strip(),
            flags=re.IGNORECASE,
        )
        if command is not None:
            if command.group(1):
                lines[index] = command.group(1)
            else:
                lines.pop(index)
        break
    return "\n".join(lines).strip()


def _match_scores_snapshot(scrim: Scrim, match_number: int) -> tuple:
    return tuple(
        (score.slot_number, score.kills, score.placement)
        for score in sorted(
            (
                score
                for score in getattr(scrim, "match_scores", {}).values()
                if score.match_number == match_number
            ),
            key=lambda score: score.slot_number,
        )
    )


class MatchScoreSubmissionReviewView(discord.ui.View):
    """Confirm or edit a complete match result set before it is saved."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        channel_id: int,
        scrim: Scrim,
        match_number: int,
        scores: list[MatchScore],
        raw_input: str,
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.scrim_id = scrim.id
        self.match_number = match_number
        self.scores = tuple(scores)
        self.raw_input = raw_input.strip()
        self.assignment_generation = assignment_fingerprint(scrim)
        self.baseline_scores = _match_scores_snapshot(scrim, match_number)
        self.team_names = {
            score.slot_number: scrim.slots[score.slot_number].team_name
            for score in scores
        }
        self.message: discord.Message | None = None
        self.completed = False
        self.editing = False
        self.processing = False

    def embed(self) -> discord.Embed:
        score_lines = []
        for score in sorted(self.scores, key=lambda item: item.placement):
            team_name = discord.utils.escape_markdown(
                discord.utils.escape_mentions(
                    self.team_names.get(score.slot_number, "Unknown team")
                )
            )
            score_lines.append(
                f"**{score.placement:02d}.** Slot {score.slot_number:02d} · "
                f"{team_name} — **{score.kills}** kills"
            )
        embed = discord.Embed(
            title=f"Review Match {self.match_number} results",
            description=(
                "Check the rank order and kills before saving. "
                f"Confirm replaces the previous complete result set for Match "
                f"{self.match_number}; teams omitted here will be removed.\n\n"
                + "\n".join(score_lines)
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(
            text="No scores have been saved · Confirm saves · Edit changes the command"
        )
        return embed

    async def authorized_scrim(
        self,
        interaction: discord.Interaction,
    ) -> Scrim | None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This score review belongs to another staff member.",
                ephemeral=True,
            )
            return None
        if (
            getattr(interaction.guild, "id", None) != self.guild_id
            or interaction.channel_id != self.channel_id
        ):
            await interaction.response.send_message(
                "This score review is only valid in its original server and channel.",
                ephemeral=True,
            )
            return None
        if self.completed:
            await interaction.response.send_message(
                "This score review has already been processed.",
                ephemeral=True,
            )
            return None

        scrim = repository.get(self.scrim_id)
        if (
            scrim is None
            or scrim.guild_id != self.guild_id
            or not is_active(scrim)
            or not member_is_staff(interaction.user, scrim)
        ):
            await interaction.response.send_message(
                "You no longer have access to this scrim's score review.",
                ephemeral=True,
            )
            return None
        if not 1 <= self.match_number <= scrim.max_matches:
            await interaction.response.send_message(
                "That match is no longer configured for this scrim.",
                ephemeral=True,
            )
            return None
        return scrim

    async def current_scrim(
        self,
        interaction: discord.Interaction,
    ) -> Scrim | None:
        scrim = await self.authorized_scrim(interaction)
        if scrim is None:
            return None
        if (
            assignment_fingerprint(scrim) != self.assignment_generation
            or _match_scores_snapshot(scrim, self.match_number)
            != self.baseline_scores
        ):
            self.completed = True
            disable_view_items(self)
            self.stop()
            await interaction.response.edit_message(
                content=(
                    "This review is out of date because teams or saved scores "
                    "changed. No proposed scores were saved. Run the command "
                    "again to review the current data."
                ),
                embed=None,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        return scrim

    async def close(self, content: str) -> None:
        self.completed = True
        self.editing = False
        disable_view_items(self)
        self.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=content,
                embed=None,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not close the Match %s score review.", self.match_number)

    async def on_timeout(self) -> None:
        if self.completed:
            return
        await self.close(
            f"Match {self.match_number} score review expired. "
            "No proposed scores were saved."
        )

    @discord.ui.button(
        label="Confirm",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.editing:
            await interaction.response.send_message(
                "Finish or cancel the edit form before confirming this review.",
                ephemeral=True,
            )
            return
        if self.processing:
            await interaction.response.send_message(
                "This score review is already being processed.",
                ephemeral=True,
            )
            return
        scrim = await self.current_scrim(interaction)
        if scrim is None:
            return
        self.processing = True
        try:
            processed = repository.replace_match_scores(
                scrim.id,
                scrim.guild_id,
                self.match_number,
                list(self.scores),
            )
        except ValueError as error:
            self.processing = False
            await interaction.response.send_message(
                f"❌ {error} Nothing was saved.",
                ephemeral=True,
            )
            return
        except SlotStorageError:
            self.processing = False
            logger.exception(
                "Could not save submitted scores for scrim %s.",
                scrim.id,
            )
            await interaction.response.send_message(
                "The scores could not be saved. Nothing was changed; please try again.",
                ephemeral=True,
            )
            return

        self.completed = True
        self.editing = False
        disable_view_items(self)
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"✅ Scores saved for Match {self.match_number}: "
                f"Processed {processed} teams. Use `!res` when you are ready "
                "to publish the leaderboard image."
            ),
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Edit",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.processing:
            await interaction.response.send_message(
                "This score review is already being processed.",
                ephemeral=True,
            )
            return
        scrim = await self.current_scrim(interaction)
        if scrim is None:
            return
        self.editing = True
        await interaction.response.send_modal(
            MatchScoreSubmissionEditModal(self)
        )

    @discord.ui.button(
        label="Cancel",
        emoji="❌",
        style=discord.ButtonStyle.danger,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.processing:
            await interaction.response.send_message(
                "This score review is already being processed.",
                ephemeral=True,
            )
            return
        if await self.authorized_scrim(interaction) is None:
            return
        self.completed = True
        self.editing = False
        disable_view_items(self)
        self.stop()
        await interaction.response.edit_message(
            content=(
                "Score entry cancelled. No proposed scores were saved; "
                "previously saved results remain unchanged."
            ),
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class MatchScoreSubmissionEditModal(discord.ui.Modal):
    """Let staff edit the pasted !resgN command before confirming it."""

    def __init__(self, source_review: MatchScoreSubmissionReviewView) -> None:
        super().__init__(
            title=f"Edit Match {source_review.match_number} score command",
            timeout=300,
        )
        self.source_review = source_review
        default_command = (
            f"!resg{source_review.match_number}\n{source_review.raw_input}"
        )
        self.command_input = discord.ui.TextInput(
            label="Paste or edit the full command",
            placeholder=f"!resg{source_review.match_number} then one slot kills line per rank",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
            default=default_command[:4000],
        )
        self.add_item(self.command_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        source_review = self.source_review
        if source_review.completed or not source_review.editing:
            await interaction.response.send_message(
                "This score review is no longer available. Run the command again.",
                ephemeral=True,
            )
            return
        scrim = await source_review.authorized_scrim(interaction)
        if scrim is None:
            return
        if (
            assignment_fingerprint(scrim) != source_review.assignment_generation
            or _match_scores_snapshot(scrim, source_review.match_number)
            != source_review.baseline_scores
        ):
            await interaction.response.send_message(
                "Teams or saved results changed while the edit form was open. "
                "Nothing was saved; run the command again.",
                ephemeral=True,
            )
            await source_review.close(
                "This review became out of date while the edit form was open. "
                "No proposed scores were saved."
            )
            return
        normalized_input = normalize_score_submission_text(
            self.command_input.value,
            source_review.match_number,
        )
        try:
            scores = parse_match_score_lines(
                normalized_input,
                match_number=source_review.match_number,
                scrim=scrim,
            )
        except ValueError as error:
            await interaction.response.send_message(
                f"❌ {error} Nothing was saved. Reopen Edit to try again.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not scores:
            await interaction.response.send_message(
                "❌ No valid scores found. Nothing was saved. Reopen Edit to try again.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        review = MatchScoreSubmissionReviewView(
            owner_id=source_review.owner_id,
            guild_id=scrim.guild_id,
            channel_id=source_review.channel_id,
            scrim=scrim,
            match_number=source_review.match_number,
            scores=scores,
            raw_input=normalized_input,
        )
        await interaction.response.send_message(
            "Review the edited scores below. **Nothing has been saved.**",
            embed=review.embed(),
            view=review,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            review.message = await interaction.original_response()
        except discord.HTTPException:
            logger.exception("Could not retain the edited Match %s review.", review.match_number)
        await source_review.close(
            "This score review was replaced by the edited proposal below. "
            "It did not save any scores."
        )


class MatchScoreCorrectionView(discord.ui.View):
    """Let the command owner choose one assigned team to correct."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        channel_id: int,
        scrim: Scrim,
        match_number: int,
        slots: list[Slot],
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.scrim_id = scrim.id
        self.match_number = match_number
        self.slot_assignment_ids = {
            slot.number: slot.assignment_id for slot in slots
        }
        self.message: discord.Message | None = None
        self.expired = False

        selector = discord.ui.Select(
            placeholder="Choose the slot/team to correct...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"Slot {slot.number:02d} · {slot.team_name}"[:100],
                    value=str(slot.number),
                    description=f"Tag: {slot.tag}"[:100] if slot.tag else None,
                )
                for slot in slots
            ],
        )

        async def select_callback(interaction: discord.Interaction) -> None:
            if self.expired or self.is_finished():
                await interaction.response.send_message(
                    "This score correction has expired. Run the command again.",
                    ephemeral=True,
                )
                return
            scrim = await self.authorized_scrim(interaction)
            if scrim is None:
                return
            try:
                slot_number = int(selector.values[0])
            except (IndexError, ValueError):
                await interaction.response.send_message(
                    "That slot selection is invalid.",
                    ephemeral=True,
                )
                return
            slot = scrim.slots.get(slot_number)
            if (
                slot_number not in self.slot_assignment_ids
                or slot is None
                or slot.status == STATUS_AVAILABLE
                or not slot.team_name
                or slot.assignment_id
                != self.slot_assignment_ids[slot_number]
            ):
                await interaction.response.send_message(
                    "That team changed after this correction menu opened. "
                    "Run the command again and choose the current team.",
                    ephemeral=True,
                )
                return
            current_score = scrim.match_scores.get(
                (self.match_number, slot_number)
            )
            await interaction.response.send_modal(
                MatchScoreCorrectionModal(
                    self,
                    slot,
                    default_score=current_score,
                    baseline_score=current_score,
                )
            )

        selector.callback = select_callback
        self.add_item(selector)

    async def authorized_scrim(
        self,
        interaction: discord.Interaction,
    ) -> Scrim | None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This score correction belongs to another staff member.",
                ephemeral=True,
            )
            return None
        if (
            getattr(interaction.guild, "id", None) != self.guild_id
            or interaction.channel_id != self.channel_id
        ):
            await interaction.response.send_message(
                "This score correction is only valid in its original server "
                "and channel.",
                ephemeral=True,
            )
            return None
        if self.expired:
            await interaction.response.send_message(
                "This score correction has expired. Run the command again.",
                ephemeral=True,
            )
            return None

        scrim = repository.get(self.scrim_id)
        if (
            scrim is None
            or scrim.guild_id != self.guild_id
            or not is_active(scrim)
            or not member_is_staff(interaction.user, scrim)
        ):
            await interaction.response.send_message(
                "You no longer have access to this scrim's score correction.",
                ephemeral=True,
            )
            return None
        if self.match_number > scrim.max_matches:
            await interaction.response.send_message(
                "That match is no longer configured for this scrim.",
                ephemeral=True,
            )
            return None
        return scrim

    async def finish(self, content: str) -> None:
        disable_view_items(self)
        self.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=content,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not close the score correction selector.")

    async def on_timeout(self) -> None:
        self.expired = True
        disable_view_items(self)
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=(
                    f"Score correction expired. Run `!editres "
                    f"{self.match_number}` to start again."
                ),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not expire the score correction selector.")


class MatchScoreCorrectionModal(discord.ui.Modal):
    """Collect a proposed correction, then require a review confirmation."""

    def __init__(
        self,
        correction_view: MatchScoreCorrectionView,
        slot: Slot,
        *,
        default_score: MatchScore | None = None,
        baseline_score: MatchScore | None = None,
        source_review: MatchScoreCorrectionReviewView | None = None,
    ) -> None:
        super().__init__(
            title=f"Correct Match {correction_view.match_number} Result",
            timeout=300,
        )
        self.correction_view = correction_view
        self.slot_number = slot.number
        self.slot_assignment_id = slot.assignment_id
        self.slot_team_name = slot.team_name
        self.baseline_score = baseline_score
        self.source_review = source_review
        self.placement_input = discord.ui.TextInput(
            label="New placement / rank",
            placeholder="For example: 1",
            style=discord.TextStyle.short,
            required=True,
            max_length=3,
        )
        self.kills_input = discord.ui.TextInput(
            label="New kills",
            placeholder="For example: 6",
            style=discord.TextStyle.short,
            required=True,
            max_length=5,
        )
        if default_score is not None:
            self.placement_input.default = str(default_score.placement)
            self.kills_input.default = str(default_score.kills)
        self.add_item(self.placement_input)
        self.add_item(self.kills_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.source_review is not None and self.source_review.completed:
            await interaction.response.send_message(
                "This score review was already processed. Run the command "
                "again if another correction is needed.",
                ephemeral=True,
            )
            return
        scrim = await self.correction_view.authorized_scrim(interaction)
        if scrim is None:
            return
        slot = scrim.slots.get(self.slot_number)
        if (
            slot is None
            or slot.status == STATUS_AVAILABLE
            or slot.assignment_id != self.slot_assignment_id
            or slot.team_name != self.slot_team_name
        ):
            await interaction.response.send_message(
                "That team changed before the correction was submitted. "
                "Run the command again and select the current team.",
                ephemeral=True,
            )
            return

        current_score = scrim.match_scores.get(
            (self.correction_view.match_number, self.slot_number)
        )
        if current_score != self.baseline_score:
            await interaction.response.send_message(
                "This team's saved score changed while the form was open. "
                "No correction was saved. Run the command again to review "
                "the current score.",
                ephemeral=True,
            )
            if self.source_review is not None:
                await self.source_review.close_as_stale()
            return

        rank_text = self.placement_input.value.strip()
        kills_text = self.kills_input.value.strip()
        if (
            re.fullmatch(r"[0-9]+", rank_text) is None
            or re.fullmatch(r"[0-9]+", kills_text) is None
        ):
            await interaction.response.send_message(
                "Placement and kills must be whole numbers.",
                ephemeral=True,
            )
            return
        placement = int(rank_text)
        kills = int(kills_text)
        if placement < 1:
            await interaction.response.send_message(
                "Placement must be at least 1.",
                ephemeral=True,
            )
            return

        proposed_score = MatchScore(
            self.correction_view.match_number,
            self.slot_number,
            kills,
            placement,
        )
        review = MatchScoreCorrectionReviewView(
            correction_view=self.correction_view,
            slot=slot,
            previous_score=current_score,
            proposed_score=proposed_score,
        )
        await interaction.response.send_message(
            embed=review.embed(),
            view=review,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await review.capture_message(interaction)
        if self.source_review is not None:
            await self.source_review.close_as_edited()
        else:
            await self.correction_view.finish(
                f"Match {self.correction_view.match_number} correction "
                "proposal ready. Review the private recap and confirm to save."
            )


class MatchScoreCorrectionReviewView(discord.ui.View):
    """Review a score correction before saving its single-team upsert."""

    def __init__(
        self,
        *,
        correction_view: MatchScoreCorrectionView,
        slot: Slot,
        previous_score: MatchScore | None,
        proposed_score: MatchScore,
    ) -> None:
        super().__init__(timeout=300)
        self.correction_view = correction_view
        self.match_number = correction_view.match_number
        self.slot_number = slot.number
        self.slot_assignment_id = slot.assignment_id
        self.team_name = slot.team_name
        self.previous_score = previous_score
        self.proposed_score = proposed_score
        self.completed = False
        self.message: discord.Message | None = None

    def embed(self) -> discord.Embed:
        team_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(self.team_name)
        )
        current_result = (
            "No result recorded"
            if self.previous_score is None
            else (
                f"Rank **{self.previous_score.placement}** · "
                f"**{self.previous_score.kills}** kills"
            )
        )
        proposed_result = (
            f"Rank **{self.proposed_score.placement}** · "
            f"**{self.proposed_score.kills}** kills"
        )
        embed = discord.Embed(
            title=f"Review score correction · Match {self.match_number}",
            description=(
                "No score has been changed yet. Confirm to save this "
                "correction, edit the values, or cancel."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="Team",
            value=f"Slot {self.slot_number:02d} · {team_name}",
            inline=False,
        )
        embed.add_field(
            name="Current result",
            value=current_result,
            inline=True,
        )
        embed.add_field(
            name="Proposed result",
            value=proposed_result,
            inline=True,
        )
        embed.set_footer(
            text="Confirm saves · Edit reopens the form · Cancel discards"
        )
        return embed

    async def capture_message(self, interaction: discord.Interaction) -> None:
        try:
            self.message = await interaction.original_response()
        except discord.HTTPException:
            logger.exception("Could not retain the score correction review.")

    async def current_scrim_and_slot(
        self,
        interaction: discord.Interaction,
    ) -> tuple[Scrim, Slot] | None:
        if self.completed:
            await interaction.response.send_message(
                "This score review has already been processed.",
                ephemeral=True,
            )
            return None
        scrim = await self.correction_view.authorized_scrim(interaction)
        if scrim is None:
            return None
        slot = scrim.slots.get(self.slot_number)
        current_score = scrim.match_scores.get(
            (self.match_number, self.slot_number)
        )
        if (
            slot is None
            or slot.status == STATUS_AVAILABLE
            or slot.assignment_id != self.slot_assignment_id
            or slot.team_name != self.team_name
            or current_score != self.previous_score
        ):
            self.completed = True
            disable_view_items(self)
            self.stop()
            await interaction.response.edit_message(
                content=(
                    "This team or score changed after the recap was created. "
                    "No correction was saved. Run "
                    f"`!editres {self.match_number}` to review the current data."
                ),
                embed=None,
                view=self,
            )
            return None
        return scrim, slot

    async def close_as_edited(self) -> None:
        await self.close(
            "This recap was replaced by the newer edited proposal. "
            "Review the latest recap before confirming."
        )

    async def close_as_stale(self) -> None:
        await self.close(
            "This recap is out of date. No correction was saved. "
            f"Run `!editres {self.match_number}` to review the current data."
        )

    async def close(self, content: str) -> None:
        self.completed = True
        disable_view_items(self)
        self.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=content,
                embed=None,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not close the score correction recap.")

    async def on_timeout(self) -> None:
        if self.completed:
            return
        self.completed = True
        disable_view_items(self)
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="Score correction review expired. No change was saved.",
                embed=None,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not expire the score correction recap.")

    @discord.ui.button(
        label="Confirm",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        result = await self.current_scrim_and_slot(interaction)
        if result is None:
            return
        scrim, _slot = result
        try:
            repository.upsert_match_scores(
                scrim.id,
                scrim.guild_id,
                self.match_number,
                [self.proposed_score],
            )
        except ValueError as error:
            await interaction.response.send_message(
                f"❌ {error}",
                ephemeral=True,
            )
            return
        except SlotStorageError:
            logger.exception(
                "Could not save a score correction for scrim %s.",
                scrim.id,
            )
            await interaction.response.send_message(
                "The correction could not be saved. Please try again.",
                ephemeral=True,
            )
            return

        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"✅ Corrected Match {self.match_number} for "
                f"Slot {self.slot_number:02d} · {self.team_name}: "
                f"rank {self.proposed_score.placement}, "
                f"{self.proposed_score.kills} kills. "
                "Other teams' scores were left unchanged."
            ),
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Edit",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        result = await self.current_scrim_and_slot(interaction)
        if result is None:
            return
        _scrim, slot = result
        await interaction.response.send_modal(
            MatchScoreCorrectionModal(
                self.correction_view,
                slot,
                default_score=self.proposed_score,
                baseline_score=self.previous_score,
                source_review=self,
            )
        )

    @discord.ui.button(
        label="Choose another team",
        emoji="👥",
        style=discord.ButtonStyle.secondary,
    )
    async def choose_another_team_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        result = await self.current_scrim_and_slot(interaction)
        if result is None:
            return
        scrim, _slot = result
        assigned_slots = [
            slot
            for slot in scrim.slots.values()
            if slot.status != STATUS_AVAILABLE and slot.team_name
        ]
        new_view = MatchScoreCorrectionView(
            owner_id=self.correction_view.owner_id,
            guild_id=scrim.guild_id,
            channel_id=self.correction_view.channel_id,
            scrim=scrim,
            match_number=self.match_number,
            slots=assigned_slots,
        )
        new_view.message = interaction.message
        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.edit_message(
            content=f"Choose a team to correct for Match {self.match_number}:",
            embed=None,
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Cancel",
        emoji="❌",
        style=discord.ButtonStyle.danger,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.completed:
            await interaction.response.send_message(
                "This score review has already been processed.",
                ephemeral=True,
            )
            return
        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.edit_message(
            content="Correction cancelled. No score was changed.",
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _start_match_score_correction(
    ctx: commands.Context,
    match_number: int,
) -> None:
    scrim = await require_staff_scrim(ctx)
    if scrim is None:
        return
    if not 1 <= match_number <= scrim.max_matches:
        await send_private_command_feedback(
            ctx,
            f"❌ Match number must be between 1 and {scrim.max_matches}.",
            silent=False,
        )
        return
    assigned_slots = [
        slot
        for slot in scrim.slots.values()
        if slot.status != STATUS_AVAILABLE and slot.team_name
    ]
    if not assigned_slots:
        await send_private_command_feedback(
            ctx,
            "❌ There are no assigned teams to correct for this scrim.",
            silent=False,
        )
        return

    view = MatchScoreCorrectionView(
        owner_id=ctx.author.id,
        guild_id=scrim.guild_id,
        channel_id=ctx.channel.id,
        scrim=scrim,
        match_number=match_number,
        slots=assigned_slots,
    )
    try:
        view.message = await ctx.send(
            f"Choose the team to correct for Match {match_number}:",
            view=view,
            delete_after=300,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception(
            "Could not show the score correction selector for scrim %s.",
            scrim.id,
        )
    finally:
        await delete_command_message(ctx)


@bot.command(name="editres")
async def edit_match_score_command(
    ctx: commands.Context,
    match_number: str = "",
) -> None:
    """Correct one team's rank and kills without re-entering the match."""
    match_number = match_number.strip()
    if re.fullmatch(r"[0-9]+", match_number) is None:
        await send_private_command_feedback(
            ctx,
            "Use `!editres <match number>`, for example `!editres 2`.",
            silent=False,
        )
        return
    await _start_match_score_correction(ctx, int(match_number))


def _register_specific_score_commands() -> None:
    for match_number in range(1, MAX_MATCHES + 1):
        def make_score_handler(game_number: int):
            async def specific_score(
                ctx: commands.Context,
                *,
                input_string: str,
            ) -> None:
                await _record_match_scores(ctx, game_number, input_string)

            return specific_score

        specific_score = make_score_handler(match_number)
        specific_score.__name__ = f"record_match_scores{match_number}"
        bot.command(name=f"resg{match_number}")(specific_score)


_register_specific_score_commands()


_leaderboard_background_locks: dict[str, asyncio.Lock] = {}


def _leaderboard_profile_suffix(orientation: str, team_count: int) -> str:
    if (
        orientation not in LEADERBOARD_ORIENTATIONS
        or type(team_count) is not int
        or team_count not in LEADERBOARD_TEAM_COUNTS
    ):
        raise ValueError("Invalid leaderboard profile for a background.")
    return f"{orientation}-{team_count}"


def _leaderboard_background_path(
    scrim_id: str,
    orientation: str | None = None,
    team_count: int | None = None,
) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", scrim_id):
        raise ValueError("Invalid scrim id for a leaderboard background.")
    if orientation is None and team_count is None:
        return LEADERBOARD_BACKGROUND_UPLOAD_DIR / f"{scrim_id}.png"
    if orientation is None or team_count is None:
        raise ValueError("Both orientation and team count are required.")
    suffix = _leaderboard_profile_suffix(orientation, team_count)
    return LEADERBOARD_BACKGROUND_UPLOAD_DIR / f"{scrim_id}-{suffix}.png"


def _leaderboard_background_metadata_path(
    scrim_id: str,
    orientation: str | None = None,
    team_count: int | None = None,
) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", scrim_id):
        raise ValueError("Invalid scrim id for a leaderboard background.")
    if orientation is None and team_count is None:
        return LEADERBOARD_BACKGROUND_UPLOAD_DIR / f"{scrim_id}.json"
    if orientation is None or team_count is None:
        raise ValueError("Both orientation and team count are required.")
    suffix = _leaderboard_profile_suffix(orientation, team_count)
    return LEADERBOARD_BACKGROUND_UPLOAD_DIR / f"{scrim_id}-{suffix}.json"


def _read_leaderboard_background_metadata(
    scrim_id: str,
    orientation: str | None = None,
    team_count: int | None = None,
) -> dict[str, object]:
    path = _leaderboard_background_metadata_path(
        scrim_id,
        orientation,
        team_count,
    )
    if not path.exists():
        return {}
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("The saved leaderboard background link could not be read.") from error
    if not isinstance(metadata, dict):
        raise ValueError("The saved leaderboard background link is invalid.")
    return metadata


def _write_leaderboard_background_metadata(
    scrim_id: str,
    metadata: dict[str, object],
    orientation: str | None = None,
    team_count: int | None = None,
) -> None:
    path = _leaderboard_background_metadata_path(
        scrim_id,
        orientation,
        team_count,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary_path.write_text(
        json.dumps(metadata, ensure_ascii=True),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _leaderboard_scrim_profile(scrim: Scrim) -> tuple[str, int]:
    orientation = getattr(
        scrim,
        "leaderboard_orientation",
        DEFAULT_LEADERBOARD_ORIENTATION,
    )
    team_count = getattr(
        scrim,
        "leaderboard_team_count",
        DEFAULT_LEADERBOARD_TEAM_COUNT,
    )
    if repository.get_server_license_type(scrim.guild_id) != "Gold":
        team_count = STANDARD_LEADERBOARD_TEAM_COUNT
    return orientation, team_count


def _leaderboard_profile_accent_color(scrim: Scrim) -> str:
    orientation, team_count = _leaderboard_scrim_profile(scrim)
    accent_colors = getattr(scrim, "leaderboard_accent_colors", {})
    profile_color = (
        accent_colors.get(f"{orientation}:{team_count}")
        if isinstance(accent_colors, dict)
        else None
    )
    if isinstance(profile_color, str):
        return profile_color
    if not accent_colors:
        legacy_color = getattr(
            scrim,
            "leaderboard_accent_color",
            DEFAULT_LEADERBOARD_ACCENT_COLOR,
        )
        if isinstance(legacy_color, str):
            return legacy_color
    return DEFAULT_LEADERBOARD_ACCENT_COLOR


def _migrate_legacy_leaderboard_background(scrim: Scrim) -> None:
    legacy_image_path = _leaderboard_background_path(scrim.id)
    legacy_metadata_path = _leaderboard_background_metadata_path(scrim.id)
    if not legacy_image_path.exists() and not legacy_metadata_path.exists():
        return

    orientation, team_count = _leaderboard_scrim_profile(scrim)
    profile_image_path = _leaderboard_background_path(
        scrim.id,
        orientation,
        team_count,
    )
    profile_metadata_path = _leaderboard_background_metadata_path(
        scrim.id,
        orientation,
        team_count,
    )
    profile_image_existed = profile_image_path.exists()
    metadata_to_migrate = None
    if (
        not profile_image_existed
        and not profile_metadata_path.exists()
        and legacy_metadata_path.exists()
    ):
        metadata_to_migrate = _read_leaderboard_background_metadata(scrim.id)

    profile_image_path.parent.mkdir(parents=True, exist_ok=True)
    if legacy_image_path.exists():
        if not profile_image_existed:
            os.replace(legacy_image_path, profile_image_path)
        else:
            legacy_image_path.unlink()
    if metadata_to_migrate is not None:
        _write_leaderboard_background_metadata(
            scrim.id,
            metadata_to_migrate,
            orientation,
            team_count,
        )
    legacy_metadata_path.unlink(missing_ok=True)


def _current_leaderboard_background_metadata(scrim: Scrim) -> dict[str, object]:
    _migrate_legacy_leaderboard_background(scrim)
    orientation, team_count = _leaderboard_scrim_profile(scrim)
    return _read_leaderboard_background_metadata(
        scrim.id,
        orientation,
        team_count,
    )


def _current_leaderboard_background_path(scrim: Scrim) -> Path:
    _migrate_legacy_leaderboard_background(scrim)
    orientation, team_count = _leaderboard_scrim_profile(scrim)
    custom_path = _leaderboard_background_path(
        scrim.id,
        orientation,
        team_count,
    )
    return custom_path if custom_path.is_file() else LEADERBOARD_BACKGROUND


def _leaderboard_background_lock(scrim_id: str) -> asyncio.Lock:
    lock = _leaderboard_background_locks.get(scrim_id)
    if lock is None:
        lock = asyncio.Lock()
        _leaderboard_background_locks[scrim_id] = lock
    return lock


async def _delete_background_preview(
    metadata: dict[str, object],
) -> None:
    channel_id = metadata.get("channel_id")
    message_id = metadata.get("message_id")
    if type(channel_id) is not int or type(message_id) is not int:
        return
    channel = bot.get_channel(channel_id)
    if channel is None or not hasattr(channel, "fetch_message"):
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        return
    except discord.HTTPException:
        logger.exception("Could not remove replaced leaderboard background preview")


async def _ensure_leaderboard_background_preview(
    scrim: Scrim,
    channel: object,
) -> str:
    """Return a durable Discord CDN link for this scrim's current background."""
    async with _leaderboard_background_lock(scrim.id):
        metadata = _current_leaderboard_background_metadata(scrim)
        channel_id = metadata.get("channel_id")
        message_id = metadata.get("message_id")
        if type(channel_id) is int and type(message_id) is int:
            preview_channel = bot.get_channel(channel_id)
            if preview_channel is not None and hasattr(
                preview_channel,
                "fetch_message",
            ):
                try:
                    preview_message = await preview_channel.fetch_message(message_id)
                    if preview_message.attachments:
                        return preview_message.attachments[0].url
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException:
                    logger.exception(
                        "Could not check leaderboard background preview for %s",
                        scrim.id,
                    )
        if not hasattr(channel, "send"):
            raise ValueError("The current channel cannot host a background preview.")
        background_path = _current_leaderboard_background_path(scrim)
        orientation, team_count = _leaderboard_scrim_profile(scrim)
        if (
            background_path == LEADERBOARD_BACKGROUND
            and not background_path.is_file()
        ):
            # The built-in canvas is generated during rendering, not stored as
            # an uploadable asset. No preview attachment is needed to open the
            # settings panel.
            return ""
        profile_suffix = _leaderboard_profile_suffix(
            orientation,
            team_count,
        )
        preview_file = discord.File(
            background_path,
            filename=f"leaderboard-background-{scrim.id}-{profile_suffix}.png",
        )
        try:
            preview = await channel.send(
                content=(
                    "Current leaderboard background for "
                    f"**{discord.utils.escape_markdown(scrim.name)}** "
                    f"({orientation}, {team_count} teams)"
                ),
                file=preview_file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not upload leaderboard background preview for %s",
                scrim.id,
            )
            return ""
        finally:
            preview_file.close()
        if not preview.attachments:
            raise RuntimeError("Discord did not return the uploaded background link.")
        attachment_url = preview.attachments[0].url
        _write_leaderboard_background_metadata(
            scrim.id,
            {
                "channel_id": preview.channel.id,
                "message_id": preview.id,
                "attachment_url": attachment_url,
            },
            orientation,
            team_count,
        )
        return attachment_url


def _available_leaderboard_scrims(
    guild_id: int,
    member: object,
) -> list[Scrim]:
    return [
        scrim
        for scrim in repository.list(guild_id)
        if is_active(scrim) and member_can_configure_scrim(member, scrim)
    ]


class LeaderboardPanelView(discord.ui.View):
    """Owner- and scrim-bound base for the standalone leaderboard panel."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.scrim_id = scrim_id

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the staff member who opened this panel can use it.",
                ephemeral=True,
            )
            return False
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This leaderboard panel is not valid in this server.",
                ephemeral=True,
            )
            return False
        if self.scrim_id is not None:
            scrim = repository.get(self.scrim_id)
            if (
                scrim is None
                or scrim.guild_id != self.guild_id
                or not is_active(scrim)
                or not member_can_configure_scrim(interaction.user, scrim)
            ):
                await interaction.response.send_message(
                    "You no longer have access to this leaderboard panel.",
                    ephemeral=True,
                )
                return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[object],
    ) -> None:
        logger.error(
            "Leaderboard panel interaction failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Could not update leaderboard settings. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class LeaderboardBlueprintOfferView(LeaderboardPanelView):
    """Offer the exact upload canvas after a dimension mismatch."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
    ) -> None:
        super().__init__(
            owner_id=owner_id,
            guild_id=guild_id,
            scrim_id=scrim_id,
        )
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        yes_button = discord.ui.Button(
            label="Yes, send both blueprints",
            style=discord.ButtonStyle.success,
            row=0,
        )

        async def yes_callback(interaction: discord.Interaction) -> None:
            scrim = repository.get(self.scrim_id)
            if scrim is None or scrim.guild_id != self.guild_id:
                await interaction.response.send_message(
                    "This scrim is no longer available.",
                    ephemeral=True,
                )
                return
            try:
                blueprint, filename = _build_empty_leaderboard_blueprint(scrim)
                dimensioned_blueprint, dimensioned_filename = (
                    _load_dimensioned_leaderboard_blueprint(scrim)
                )
            except (OSError, ValueError, RuntimeError) as error:
                await interaction.response.send_message(
                    f"Could not prepare the leaderboard blueprints: {error}",
                    ephemeral=True,
                )
                return
            self.stop()
            await interaction.response.send_message(
                (
                    "Attached are the exact-size blank canvas for this profile "
                    f"and the {leaderboard_blueprint_reference_label(scrim)}. "
                    "The reference is not an upload background."
                ),
                files=[
                    discord.File(blueprint, filename=filename),
                    discord.File(
                        dimensioned_blueprint,
                        filename=dimensioned_filename,
                    ),
                ],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        yes_button.callback = yes_callback
        self.add_item(yes_button)

        no_button = discord.ui.Button(
            label="No",
            style=discord.ButtonStyle.secondary,
            row=0,
        )

        async def no_callback(interaction: discord.Interaction) -> None:
            self.stop()
            await interaction.response.edit_message(
                content="No blueprint sent.",
                embed=None,
                view=None,
            )

        no_button.callback = no_callback
        self.add_item(no_button)


class LeaderboardSettingsView(LeaderboardPanelView):
    """Dashboard for the separate !setres staff panel."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrims: list[Scrim],
        has_created_scrims: bool | None = None,
    ) -> None:
        super().__init__(owner_id=owner_id, guild_id=guild_id)
        self.scrim_ids = [scrim.id for scrim in scrims]
        self.has_created_scrims = (
            bool(repository.list(guild_id))
            if has_created_scrims is None
            else has_created_scrims
        )
        self.rebuild()

    def content(self) -> str:
        return (
            "**Leaderboard Settings**\n"
            "Edit the leaderboard presentation for one of your active scrims."
        )

    def embed(self) -> discord.Embed:
        if self.scrim_ids:
            scrims = [
                repository.get(scrim_id)
                for scrim_id in self.scrim_ids
            ]
            description = "\n".join(
                f"• **{discord.utils.escape_markdown(scrim.name)}**"
                for scrim in scrims
                if scrim is not None and scrim.guild_id == self.guild_id
            )
        elif not self.has_created_scrims:
            description = (
                "No scrims have been created yet. Use `!setup` to create one."
            )
        else:
            description = (
                "No active scrims are available to your Staff role."
            )
        return discord.Embed(
            title="Leaderboard Settings",
            description=description,
            color=discord.Color.blurple(),
        )

    def rebuild(self) -> None:
        self.clear_items()
        edit_button = discord.ui.Button(
            label="Edit Leaderboard",
            emoji="📊",
            style=discord.ButtonStyle.primary,
            disabled=not self.scrim_ids,
            row=0,
        )

        async def edit_callback(interaction: discord.Interaction) -> None:
            scrims = _available_leaderboard_scrims(
                self.guild_id,
                interaction.user,
            )
            if not scrims:
                self.scrim_ids = []
                self.has_created_scrims = bool(repository.list(self.guild_id))
                self.rebuild()
                await interaction.response.edit_message(
                    content=self.content(),
                    embed=self.embed(),
                    view=self,
                )
                return
            if len(scrims) == 1:
                await interaction.response.defer()
                background_url = await _ensure_leaderboard_background_preview(
                    scrims[0],
                    interaction.channel,
                )
                view = LeaderboardScrimEditView(
                    owner_id=self.owner_id,
                    guild_id=self.guild_id,
                    scrim_id=scrims[0].id,
                    background_url=background_url,
                )
                await interaction.edit_original_response(
                    content=view.content(),
                    embed=view.embed(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            view = LeaderboardScrimSelectView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrims=scrims,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        edit_button.callback = edit_callback
        self.add_item(edit_button)

        close_button = discord.ui.Button(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.secondary,
            row=0,
        )

        async def close_callback(interaction: discord.Interaction) -> None:
            self.stop()
            await interaction.response.edit_message(
                content="Leaderboard settings closed.",
                embed=None,
                view=None,
            )

        close_button.callback = close_callback
        self.add_item(close_button)


class LeaderboardScrimSelectView(LeaderboardPanelView):
    """Select an active scrim before opening its leaderboard settings."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrims: list[Scrim],
    ) -> None:
        super().__init__(owner_id=owner_id, guild_id=guild_id)
        self.scrim_ids = [scrim.id for scrim in scrims]
        self.scrims = scrims[:25]
        self.rebuild()

    def content(self) -> str:
        return (
            "**Edit Leaderboard**\n"
            "Choose which scrim's leaderboard settings to edit."
        )

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="Select a Scrim",
            description="Choose an active scrim from the dropdown below.",
            color=discord.Color.blurple(),
        )

    def rebuild(self) -> None:
        self.clear_items()
        selector = discord.ui.Select(
            placeholder="Choose a scrim",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=scrim.name[:100],
                    value=scrim.id,
                )
                for scrim in self.scrims
            ],
            row=0,
        )

        async def select_callback(interaction: discord.Interaction) -> None:
            selected_id = selector.values[0]
            if selected_id not in self.scrim_ids:
                await interaction.response.send_message(
                    "That scrim selection is no longer available.",
                    ephemeral=True,
                )
                return
            selected = repository.get(selected_id)
            if (
                selected is None
                or selected.guild_id != self.guild_id
                or not is_active(selected)
                or not member_can_configure_scrim(interaction.user, selected)
            ):
                await interaction.response.send_message(
                    "You no longer have access to that scrim.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            background_url = await _ensure_leaderboard_background_preview(
                selected,
                interaction.channel,
            )
            view = LeaderboardScrimEditView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=selected.id,
                background_url=background_url,
            )
            await interaction.edit_original_response(
                content=view.content(),
                embed=view.embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        selector.callback = select_callback
        self.add_item(selector)

        back_button = discord.ui.Button(
            label="Back to Dashboard",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

        async def back_callback(interaction: discord.Interaction) -> None:
            scrims = _available_leaderboard_scrims(
                self.guild_id,
                interaction.user,
            )
            view = LeaderboardSettingsView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrims=scrims,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        back_button.callback = back_callback
        self.add_item(back_button)


class LeaderboardScrimEditView(LeaderboardPanelView):
    """Show the selected scrim and its leaderboard controls."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        background_url: str,
    ) -> None:
        super().__init__(
            owner_id=owner_id,
            guild_id=guild_id,
            scrim_id=scrim_id,
        )
        self.background_url = background_url
        self.rebuild()

    def content(self) -> str:
        return "**Edit Leaderboard**\nChanges are saved automatically."

    def embed(self) -> discord.Embed:
        scrim = repository.get(self.scrim_id)
        if scrim is None or scrim.guild_id != self.guild_id:
            return discord.Embed(
                title="Scrim no longer exists",
                color=discord.Color.red(),
            )
        is_gold = repository.get_server_license_type(self.guild_id) == "Gold"
        orientation, team_count = _leaderboard_scrim_profile(scrim)
        background_metadata = _current_leaderboard_background_metadata(scrim)
        saved_background_url = background_metadata.get("attachment_url")
        profile_background_path = _leaderboard_background_path(
            scrim.id,
            orientation,
            team_count,
        )
        if isinstance(saved_background_url, str) and saved_background_url:
            self.background_url = saved_background_url
            background_text = f"[View current background]({saved_background_url})"
        elif profile_background_path.is_file():
            background_text = "Custom background saved; preview link unavailable"
        else:
            background_text = "Built-in default background"
        accent_color = _leaderboard_profile_accent_color(scrim)
        embed = discord.Embed(
            title=f"Edit Leaderboard — {discord.utils.escape_markdown(scrim.name)}",
            description=(
                "Standard is fixed at 20 teams in either orientation. "
                "Gold can choose the team count. Backgrounds are available "
                "for each license-available profile."
                if not is_gold
                else
                "Background and text color are saved separately for each "
                "orientation and team count."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Scrim",
            value=discord.utils.escape_markdown(scrim.name),
            inline=True,
        )
        embed.add_field(
            name="Teams to Display",
            value=str(team_count),
            inline=True,
        )
        embed.add_field(
            name="Orientation",
            value=orientation.title(),
            inline=True,
        )
        embed.add_field(
            name="Text Color",
            value=_leaderboard_accent_label(accent_color),
            inline=True,
        )
        embed.add_field(
            name="Background",
            value=background_text,
            inline=False,
        )
        return embed

    def rebuild(self) -> None:
        self.clear_items()
        teams_button = discord.ui.Button(
            label=(
                "Teams to Display"
                if repository.get_server_license_type(self.guild_id) == "Gold"
                else "Teams to Display · Gold"
            ),
            emoji="🔢",
            style=discord.ButtonStyle.primary,
            disabled=repository.get_server_license_type(self.guild_id) != "Gold",
            row=0,
        )

        async def teams_callback(interaction: discord.Interaction) -> None:
            if repository.get_server_license_type(self.guild_id) != "Gold":
                await interaction.response.send_message(
                    "Standard licenses are locked to 20 teams. "
                    "Gold licenses can change the team count.",
                    ephemeral=True,
                )
                return
            view = LeaderboardTeamCountView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=self.background_url,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        teams_button.callback = teams_callback
        self.add_item(teams_button)

        background_button = discord.ui.Button(
            label="Background",
            emoji="🖼️",
            style=discord.ButtonStyle.primary,
            row=0,
        )

        async def background_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                (
                    "To replace this background, send one image attachment in "
                    f"this channel and mention {bot.user.mention}. "
                    "Accepted formats: PNG, JPG, or WebP; maximum size: 8 MB. "
                    "You have 2 minutes."
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            bot_user = bot.user

            def upload_check(message: discord.Message) -> bool:
                return (
                    message.author.id == self.owner_id
                    and message.channel.id == interaction.channel_id
                    and (
                        bool(message.attachments)
                        or (
                            bot_user is not None
                            and bot_user in message.mentions
                        )
                    )
                )

            try:
                upload_message = await bot.wait_for(
                    "message",
                    check=upload_check,
                    timeout=120,
                )
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    "Background upload timed out. Use the Background button to try again.",
                    ephemeral=True,
                )
                return

            if bot_user is None or bot_user not in upload_message.mentions:
                await interaction.followup.send(
                    "Please mention this bot in the message that contains the image.",
                    ephemeral=True,
                )
                return
            if len(upload_message.attachments) != 1:
                await interaction.followup.send(
                    "Attach exactly one image file.",
                    ephemeral=True,
                )
                return
            attachment = upload_message.attachments[0]
            if attachment.size > MAX_LEADERBOARD_BACKGROUND_UPLOAD_BYTES:
                await interaction.followup.send(
                    "That image is larger than the 8 MB limit.",
                    ephemeral=True,
                )
                return

            try:
                payload = await attachment.read()
                if len(payload) > MAX_LEADERBOARD_BACKGROUND_UPLOAD_BYTES:
                    raise ValueError("That image is larger than the 8 MB limit.")
                scrim = repository.get(self.scrim_id)
                if (
                    scrim is None
                    or scrim.guild_id != self.guild_id
                    or not is_active(scrim)
                    or not member_can_configure_scrim(
                        interaction.user,
                        scrim,
                    )
                ):
                    raise ValueError(
                        "You no longer have access to this leaderboard."
                    )
                background_url = await _store_leaderboard_background(
                    scrim,
                    upload_message.channel,
                    payload,
                )
                updated_view = LeaderboardScrimEditView(
                    owner_id=self.owner_id,
                    guild_id=self.guild_id,
                    scrim_id=self.scrim_id,
                    background_url=background_url,
                )
                await interaction.message.edit(
                    content=(
                        "**Edit Leaderboard**\n"
                        "Background saved. Changes are saved automatically."
                    ),
                    embed=updated_view.embed(),
                    view=updated_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await interaction.followup.send(
                    "Background saved.",
                    ephemeral=True,
                )
            except LeaderboardBackgroundDimensionsError as error:
                view = LeaderboardBlueprintOfferView(
                    owner_id=self.owner_id,
                    guild_id=self.guild_id,
                    scrim_id=self.scrim_id,
                )
                await interaction.followup.send(
                    (
                        f"Normalized image size: **{error.actual[0]} × "
                        f"{error.actual[1]} px**. Required for this profile: "
                        f"**{error.required[0]} × {error.required[1]} px**. "
                        "Would you like the matching blank canvas and the "
                        f"{leaderboard_blueprint_reference_label(scrim)}?"
                    ),
                    view=view,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (OSError, ValueError, discord.HTTPException) as error:
                await interaction.followup.send(
                    f"Could not save that background: {error}",
                    ephemeral=True,
                )

        background_button.callback = background_callback
        self.add_item(background_button)

        orientation_button = discord.ui.Button(
            label="Orientation",
            emoji="↔️",
            style=discord.ButtonStyle.primary,
            row=0,
        )

        async def orientation_callback(interaction: discord.Interaction) -> None:
            view = LeaderboardOrientationView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=self.background_url,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        orientation_button.callback = orientation_callback
        self.add_item(orientation_button)

        scrim = repository.get(self.scrim_id)
        has_custom_background = False
        if scrim is not None:
            orientation, team_count = _leaderboard_scrim_profile(scrim)
            has_custom_background = _leaderboard_background_path(
                self.scrim_id,
                orientation,
                team_count,
            ).is_file()
        accent_button = discord.ui.Button(
            label="Text Color",
            emoji="🎨",
            style=discord.ButtonStyle.primary,
            row=1,
        )

        async def accent_callback(interaction: discord.Interaction) -> None:
            view = LeaderboardAccentColorView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=self.background_url,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        accent_button.callback = accent_callback
        self.add_item(accent_button)

        restore_background_button = discord.ui.Button(
            label="Restore Default Background",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            disabled=not has_custom_background,
            row=1,
        )

        async def restore_background_callback(
            interaction: discord.Interaction,
        ) -> None:
            scrim = repository.get(self.scrim_id)
            if (
                scrim is None
                or scrim.guild_id != self.guild_id
                or not is_active(scrim)
                or not member_can_configure_scrim(interaction.user, scrim)
            ):
                await interaction.response.send_message(
                    "You no longer have access to this leaderboard panel.",
                    ephemeral=True,
                )
                return
            if not _leaderboard_background_path(
                self.scrim_id,
                *_leaderboard_scrim_profile(scrim),
            ).is_file():
                await interaction.response.send_message(
                    "This scrim is already using the default background.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            try:
                background_url = await _restore_default_leaderboard_background(
                    scrim,
                    interaction.channel,
                )
            except (OSError, ValueError, RuntimeError, discord.HTTPException) as error:
                await interaction.followup.send(
                    f"Could not restore the default background: {error}",
                    ephemeral=True,
                )
                return

            updated_view = LeaderboardScrimEditView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=background_url,
            )
            await interaction.edit_original_response(
                content=(
                    "**Edit Leaderboard**\n"
                    "The default background is restored. Changes are saved automatically."
                ),
                embed=updated_view.embed(),
                view=updated_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                "Restored the default leaderboard background.",
                ephemeral=True,
            )

        restore_background_button.callback = restore_background_callback
        self.add_item(restore_background_button)

        back_button = discord.ui.Button(
            label="Back to Dashboard",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

        async def back_callback(interaction: discord.Interaction) -> None:
            scrims = _available_leaderboard_scrims(
                self.guild_id,
                interaction.user,
            )
            view = LeaderboardSettingsView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrims=scrims,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        back_button.callback = back_callback
        self.add_item(back_button)


        blueprint_button = discord.ui.Button(
            label="Blueprint",
            emoji="📐",
            style=discord.ButtonStyle.secondary,
            row=2,
        )

        async def blueprint_callback(interaction: discord.Interaction) -> None:
            scrim = repository.get(self.scrim_id)
            if (
                scrim is None
                or scrim.guild_id != self.guild_id
                or not is_active(scrim)
                or not member_can_configure_scrim(interaction.user, scrim)
            ):
                await interaction.response.send_message(
                    "You no longer have access to this leaderboard panel.",
                    ephemeral=True,
                )
                return
            try:
                blueprint, filename = _build_empty_leaderboard_blueprint(scrim)
                dimensioned_blueprint, dimensioned_filename = (
                    _load_dimensioned_leaderboard_blueprint(scrim)
                )
            except (OSError, ValueError, RuntimeError) as error:
                await interaction.response.send_message(
                    f"Could not prepare the leaderboard blueprints: {error}",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                (
                    "Attached are the exact-size blank canvas for this profile "
                    f"and the {leaderboard_blueprint_reference_label(scrim)}. "
                    "The reference is not an upload background."
                ),
                files=[
                    discord.File(blueprint, filename=filename),
                    discord.File(
                        dimensioned_blueprint,
                        filename=dimensioned_filename,
                    ),
                ],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        blueprint_button.callback = blueprint_callback
        self.add_item(blueprint_button)


class LeaderboardAccentColorView(LeaderboardPanelView):
    """Choose the generated date, team, and score text color for one profile."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        background_url: str,
    ) -> None:
        super().__init__(
            owner_id=owner_id,
            guild_id=guild_id,
            scrim_id=scrim_id,
        )
        self.background_url = background_url
        self.rebuild()

    def content(self) -> str:
        return (
            "**Leaderboard Text Color**\n"
            "Choose a color for generated dates, team names, and scores."
        )

    def embed(self) -> discord.Embed:
        scrim = repository.get(self.scrim_id)
        accent_color = (
            _leaderboard_profile_accent_color(scrim)
            if scrim is not None
            else DEFAULT_LEADERBOARD_ACCENT_COLOR
        )
        orientation, team_count = (
            _leaderboard_scrim_profile(scrim)
            if scrim is not None
            else (DEFAULT_LEADERBOARD_ORIENTATION, DEFAULT_LEADERBOARD_TEAM_COUNT)
        )
        profile = f"{orientation.title()} / {team_count} teams"
        return discord.Embed(
            title="Leaderboard Text Color",
            description=(
                f"Profile: **{profile}**\n"
                f"Current color: **{_leaderboard_accent_label(accent_color)}**\n"
                "This color applies only to generated dates, team names, and "
                "scores. Table styling stays fixed.\n"
                "Enter a custom color in `#RRGGBB` format. Generated text "
                "defaults to white.\n"
                f"[Open a HEX color generator]({HEX_COLOR_GENERATOR_URL})"
            ),
            color=discord.Color.blurple(),
        )

    def rebuild(self) -> None:
        self.clear_items()
        custom_button = discord.ui.Button(
            label="Custom HEX",
            emoji="🎨",
            style=discord.ButtonStyle.primary,
            row=0,
        )

        async def custom_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                LeaderboardAccentColorModal(
                    color_view=self,
                    prompt_message=interaction.message,
                )
            )

        custom_button.callback = custom_callback
        self.add_item(custom_button)

        reset_button = discord.ui.Button(
            label="Reset to White",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            row=0,
        )

        async def reset_callback(interaction: discord.Interaction) -> None:
            await self.save_color(interaction, DEFAULT_LEADERBOARD_ACCENT_COLOR)

        reset_button.callback = reset_callback
        self.add_item(reset_button)

        back_button = discord.ui.Button(
            label="Back to Leaderboard",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

        async def back_callback(interaction: discord.Interaction) -> None:
            view = LeaderboardScrimEditView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=self.background_url,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        back_button.callback = back_callback
        self.add_item(back_button)

    async def save_color(
        self,
        interaction: discord.Interaction,
        color_hex: str,
        *,
        prompt_message: discord.Message | None = None,
        from_modal: bool = False,
    ) -> None:
        scrim = repository.get(self.scrim_id)
        if (
            interaction.user.id != self.owner_id
            or interaction.guild_id != self.guild_id
            or scrim is None
            or scrim.guild_id != self.guild_id
            or not is_active(scrim)
            or not member_can_configure_scrim(interaction.user, scrim)
        ):
            await interaction.response.send_message(
                "You no longer have access to this leaderboard panel.",
                ephemeral=True,
            )
            return
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", color_hex) is None:
            await interaction.response.send_message(
                "Enter a HEX color in `#RRGGBB` format, such as `#FF00FF`. "
                f"[Open a HEX color generator]({HEX_COLOR_GENERATOR_URL})",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if from_modal:
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            repository.update_leaderboard_settings(
                self.scrim_id,
                self.guild_id,
                leaderboard_accent_color=color_hex.upper(),
            )
        except (SlotStorageError, ValueError) as error:
            message = f"Could not save the text color: {error}"
            if from_modal:
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
            return

        updated_view = LeaderboardScrimEditView(
            owner_id=self.owner_id,
            guild_id=self.guild_id,
            scrim_id=self.scrim_id,
            background_url=self.background_url,
        )
        if not from_modal:
            await interaction.response.edit_message(
                content=(
                    f"Text color saved as `{color_hex.upper()}`. "
                    "Changes are saved automatically."
                ),
                embed=updated_view.embed(),
                view=updated_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if prompt_message is not None:
            try:
                await prompt_message.edit(
                    content=updated_view.content(),
                    embed=updated_view.embed(),
                    view=updated_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception(
                    "Saved leaderboard text color, but could not refresh "
                    "the panel for scrim %s.",
                    self.scrim_id,
                )
                await interaction.followup.send(
                    "Text color saved. Reopen `!setres` if the panel does not refresh.",
                    ephemeral=True,
                )
                return
        await interaction.followup.send(
            f"Text color saved as `{color_hex.upper()}`.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class LeaderboardAccentColorModal(discord.ui.Modal):
    """Collect and validate a custom six-digit HEX text color."""

    def __init__(
        self,
        *,
        color_view: LeaderboardAccentColorView,
        prompt_message: discord.Message | None,
    ) -> None:
        super().__init__(title="Custom HEX Text Color")
        self.color_view = color_view
        self.prompt_message = prompt_message
        self.hex_code = discord.ui.TextInput(
            label="Hexadecimal color code",
            placeholder="#FF00FF",
            min_length=7,
            max_length=7,
            required=True,
            style=discord.TextStyle.short,
        )
        self.add_item(self.hex_code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color_hex = self.hex_code.value
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", color_hex) is None:
            await interaction.response.send_message(
                "Invalid HEX code. It must start with `#` and contain exactly "
                "six letters or numbers, for example `#FF00FF`.\n"
                f"[Open a HEX color generator]({HEX_COLOR_GENERATOR_URL})",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.color_view.save_color(
            interaction,
            color_hex.upper(),
            prompt_message=self.prompt_message,
            from_modal=True,
        )


class LeaderboardTeamCountView(LeaderboardPanelView):
    """Choose the number of highest-ranked teams shown."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        background_url: str,
    ) -> None:
        super().__init__(
            owner_id=owner_id,
            guild_id=guild_id,
            scrim_id=scrim_id,
        )
        self.background_url = background_url
        self.rebuild()

    def content(self) -> str:
        return "**Teams to Display**\nChoose how many ranked teams to show."

    def embed(self) -> discord.Embed:
        scrim = repository.get(self.scrim_id)
        current = (
            _leaderboard_scrim_profile(scrim)[1]
            if scrim is not None
            else STANDARD_LEADERBOARD_TEAM_COUNT
        )
        is_gold = repository.get_server_license_type(self.guild_id) == "Gold"
        return discord.Embed(
            title="Teams to Display",
            description=(
                f"Current setting: **{current} teams**."
                if is_gold
                else "Standard licenses are locked to **20 teams**. "
                "Gold licenses can change this setting."
            ),
            color=discord.Color.blurple(),
        )

    def rebuild(self) -> None:
        self.clear_items()
        scrim = repository.get(self.scrim_id)
        is_gold = repository.get_server_license_type(self.guild_id) == "Gold"
        current = (
            _leaderboard_scrim_profile(scrim)[1]
            if scrim is not None
            else STANDARD_LEADERBOARD_TEAM_COUNT
        )
        if is_gold:
            selector = discord.ui.Select(
                placeholder="Choose 16, 18, 20, 22, or 24 teams",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=f"{count} teams",
                        value=str(count),
                        default=count == current,
                    )
                    for count in LEADERBOARD_TEAM_COUNTS
                ],
                row=0,
            )

            async def select_callback(interaction: discord.Interaction) -> None:
                if repository.get_server_license_type(self.guild_id) != "Gold":
                    await interaction.response.send_message(
                        "Standard licenses are locked to 20 teams. "
                        "Gold licenses can change the team count.",
                        ephemeral=True,
                    )
                    return
                try:
                    repository.update_leaderboard_settings(
                        self.scrim_id,
                        self.guild_id,
                        leaderboard_team_count=int(selector.values[0]),
                    )
                except (SlotStorageError, ValueError) as error:
                    await interaction.response.send_message(
                        f"Could not save the team count: {error}",
                        ephemeral=True,
                    )
                    return
                view = LeaderboardScrimEditView(
                    owner_id=self.owner_id,
                    guild_id=self.guild_id,
                    scrim_id=self.scrim_id,
                    background_url=self.background_url,
                )
                await interaction.response.edit_message(
                    content=view.content(),
                    embed=view.embed(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            selector.callback = select_callback
            self.add_item(selector)
        else:
            self.add_item(
                discord.ui.Button(
                    label="Locked to 20 Teams",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    row=0,
                )
            )

        back_button = discord.ui.Button(
            label="Back to Leaderboard",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

        async def back_callback(interaction: discord.Interaction) -> None:
            view = LeaderboardScrimEditView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=self.background_url,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        back_button.callback = back_callback
        self.add_item(back_button)


class LeaderboardOrientationView(LeaderboardPanelView):
    """Choose vertical or horizontal leaderboard orientation."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrim_id: str,
        background_url: str,
    ) -> None:
        super().__init__(
            owner_id=owner_id,
            guild_id=guild_id,
            scrim_id=scrim_id,
        )
        self.background_url = background_url
        self.rebuild()

    def content(self) -> str:
        return "**Orientation**\nChoose the leaderboard layout."

    def embed(self) -> discord.Embed:
        scrim = repository.get(self.scrim_id)
        current = (
            scrim.leaderboard_orientation
            if scrim is not None
            else DEFAULT_LEADERBOARD_ORIENTATION
        )
        description = f"Current setting: **{current.title()}**."
        if repository.get_server_license_type(self.guild_id) != "Gold":
            description += (
                "\nStandard licenses use 20 teams in either orientation."
            )
        return discord.Embed(
            title="Leaderboard Orientation",
            description=description,
            color=discord.Color.blurple(),
        )

    def rebuild(self) -> None:
        self.clear_items()
        scrim = repository.get(self.scrim_id)
        current = (
            scrim.leaderboard_orientation
            if scrim is not None
            else DEFAULT_LEADERBOARD_ORIENTATION
        )
        options = [
            discord.SelectOption(
                label="Vertical",
                value="vertical",
                default=current == "vertical",
            ),
            discord.SelectOption(
                label="Horizontal",
                value="horizontal",
                default=current == "horizontal",
            ),
        ]
        selector = discord.ui.Select(
            placeholder="Choose vertical or horizontal",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

        async def select_callback(interaction: discord.Interaction) -> None:
            try:
                repository.update_leaderboard_settings(
                    self.scrim_id,
                    self.guild_id,
                    leaderboard_orientation=selector.values[0],
                )
            except (SlotStorageError, ValueError) as error:
                await interaction.response.send_message(
                    f"Could not save the orientation: {error}",
                    ephemeral=True,
                )
                return
            view = LeaderboardScrimEditView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=self.background_url,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        selector.callback = select_callback
        self.add_item(selector)

        back_button = discord.ui.Button(
            label="Back to Leaderboard",
            emoji="↩️",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

        async def back_callback(interaction: discord.Interaction) -> None:
            view = LeaderboardScrimEditView(
                owner_id=self.owner_id,
                guild_id=self.guild_id,
                scrim_id=self.scrim_id,
                background_url=self.background_url,
            )
            await interaction.response.edit_message(
                content=view.content(),
                embed=view.embed(),
                view=view,
            )

        back_button.callback = back_callback
        self.add_item(back_button)


async def _store_leaderboard_background(
    scrim: Scrim,
    channel: object,
    payload: bytes,
) -> str:
    if not hasattr(channel, "send"):
        raise ValueError("The current channel cannot host the background image.")
    orientation, team_count = _leaderboard_scrim_profile(scrim)
    required_dimensions = leaderboard_canvas_dimensions(
        team_count,
        orientation,
    )
    try:
        with Image.open(io.BytesIO(payload)) as source:
            image_format = source.format
            if image_format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Use a PNG, JPG, or WebP image.")
            if (
                source.width < 64
                or source.height < 64
                or source.width > 16384
                or source.height > 16384
                or source.width * source.height > 40_000_000
            ):
                raise ValueError(
                    "Image dimensions must be at least 64×64 and no larger than 40 megapixels."
                )
            normalized = ImageOps.exif_transpose(source)
            actual_dimensions = normalized.size
            if actual_dimensions != required_dimensions:
                raise LeaderboardBackgroundDimensionsError(
                    actual_dimensions,
                    required_dimensions,
                )
            image = normalized.convert("RGB")
    except (Image.DecompressionBombError, OSError) as error:
        raise ValueError("That file is not a supported image.") from error

    profile_suffix = _leaderboard_profile_suffix(orientation, team_count)
    lock = _leaderboard_background_lock(scrim.id)
    async with lock:
        _migrate_legacy_leaderboard_background(scrim)
        metadata_path = _leaderboard_background_metadata_path(
            scrim.id,
            orientation,
            team_count,
        )
        final_path = _leaderboard_background_path(
            scrim.id,
            orientation,
            team_count,
        )
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        previous_metadata = _read_leaderboard_background_metadata(
            scrim.id,
            orientation,
            team_count,
        )
        previous_image = final_path.read_bytes() if final_path.exists() else None
        temporary_image_path = final_path.with_name(
            f".{scrim.id}-{profile_suffix}.{secrets.token_hex(4)}.tmp.png"
        )
        image.save(temporary_image_path, format="PNG", optimize=True)
        preview = None
        try:
            preview_file = discord.File(
                temporary_image_path,
                filename=(
                    f"leaderboard-background-{scrim.id}-{profile_suffix}.png"
                ),
            )
            try:
                preview = await channel.send(
                    content=(
                        "Current leaderboard background for "
                        f"**{discord.utils.escape_markdown(scrim.name)}** "
                        f"({orientation}, {team_count} teams)"
                    ),
                    file=preview_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                preview_file.close()
            if not preview.attachments:
                raise RuntimeError(
                    "Discord did not return the uploaded background link."
                )
            os.replace(temporary_image_path, final_path)
            _write_leaderboard_background_metadata(
                scrim.id,
                {
                    "channel_id": preview.channel.id,
                    "message_id": preview.id,
                    "attachment_url": preview.attachments[0].url,
                },
                orientation,
                team_count,
            )
        except Exception:
            temporary_image_path.unlink(missing_ok=True)
            if previous_image is None:
                final_path.unlink(missing_ok=True)
            else:
                restore_path = final_path.with_name(
                    f".{scrim.id}-{profile_suffix}.{secrets.token_hex(4)}.restore.png"
                )
                restore_path.write_bytes(previous_image)
                os.replace(restore_path, final_path)
            if preview is not None:
                try:
                    await preview.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
            raise

        await _delete_background_preview(previous_metadata)
        return preview.attachments[0].url


async def _restore_default_leaderboard_background(
    scrim: Scrim,
    channel: object,
) -> str:
    """Remove a custom image and point its preview to the default background."""
    if not hasattr(channel, "send"):
        raise ValueError("The current channel cannot host the background image.")

    orientation, team_count = _leaderboard_scrim_profile(scrim)
    profile_suffix = _leaderboard_profile_suffix(orientation, team_count)
    lock = _leaderboard_background_lock(scrim.id)
    async with lock:
        _migrate_legacy_leaderboard_background(scrim)
        custom_path = _leaderboard_background_path(
            scrim.id,
            orientation,
            team_count,
        )
        if not custom_path.is_file():
            raise ValueError("This scrim is already using the default background.")
        metadata_path = _leaderboard_background_metadata_path(
            scrim.id,
            orientation,
            team_count,
        )
        previous_metadata = _read_leaderboard_background_metadata(
            scrim.id,
            orientation,
            team_count,
        )
        backup_path = custom_path.with_name(
            f".{scrim.id}-{profile_suffix}.{secrets.token_hex(4)}.reset.png"
        )
        preview = None
        moved_custom = False
        try:
            default_buffer, _ = _build_empty_leaderboard_blueprint(scrim)
            preview_file = discord.File(
                default_buffer,
                filename=f"leaderboard-background-{scrim.id}-{profile_suffix}.png",
            )
            try:
                preview = await channel.send(
                    content=(
                        "Restored default leaderboard background for "
                        f"**{discord.utils.escape_markdown(scrim.name)}** "
                        f"({orientation}, {team_count} teams)"
                    ),
                    file=preview_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                preview_file.close()
                default_buffer.close()
            if not preview.attachments:
                raise RuntimeError(
                    "Discord did not return the restored background link."
                )

            os.replace(custom_path, backup_path)
            moved_custom = True
            _write_leaderboard_background_metadata(
                scrim.id,
                {
                    "channel_id": preview.channel.id,
                    "message_id": preview.id,
                    "attachment_url": preview.attachments[0].url,
                },
                orientation,
                team_count,
            )
        except Exception:
            if moved_custom and backup_path.exists():
                try:
                    os.replace(backup_path, custom_path)
                except OSError:
                    logger.exception(
                        "Could not restore custom leaderboard background for %s",
                        scrim.id,
                    )
            try:
                if previous_metadata:
                    _write_leaderboard_background_metadata(
                        scrim.id,
                        previous_metadata,
                        orientation,
                        team_count,
                    )
                else:
                    metadata_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Could not restore leaderboard background metadata for %s",
                    scrim.id,
                )
            if preview is not None:
                try:
                    await preview.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException:
                    logger.exception(
                        "Could not remove failed default background preview"
                    )
            raise

        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "Could not remove replaced custom leaderboard background for %s",
                scrim.id,
            )
        await _delete_background_preview(previous_metadata)
        return preview.attachments[0].url


@bot.command(name="setres")
@commands.guild_only()
async def leaderboard_settings_command(ctx: commands.Context) -> None:
    """Open the standalone leaderboard settings panel."""
    all_scrims = repository.list(ctx.guild.id)
    scrims = _available_leaderboard_scrims(ctx.guild.id, ctx.author)
    view = LeaderboardSettingsView(
        owner_id=ctx.author.id,
        guild_id=ctx.guild.id,
        scrims=scrims,
        has_created_scrims=bool(all_scrims),
    )
    await ctx.send(
        content=view.content(),
        embed=view.embed(),
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
        delete_after=600,
    )
    await delete_command_message(ctx)


@bot.command(name="res", aliases=["leaderboard", "lb"])
@commands.guild_only()
async def leaderboard_command(ctx: commands.Context) -> None:
    """Render the final current leaderboard image."""
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return
    progress_message: discord.Message | None = None
    try:
        try:
            progress_message = await ctx.send(
                "⏳ Working on the leaderboard…",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not show leaderboard progress for scrim %s.",
                scrim.id,
            )
        rows = calculate_leaderboard(scrim)
        image = build_leaderboard_image(scrim, rows)
        await ctx.send(
            content=build_results_publication_message(scrim, rows),
            file=discord.File(image, filename="leaderboard.png"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if progress_message is not None:
            try:
                await progress_message.delete()
            except discord.HTTPException:
                try:
                    await progress_message.edit(
                        content="✅ Leaderboard generated above.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not clear leaderboard progress for scrim %s.",
                        scrim.id,
                    )
    except (OSError, ValueError, discord.HTTPException) as error:
        logger.exception("Could not generate leaderboard for scrim %s.", scrim.id)
        if progress_message is not None:
            try:
                await progress_message.edit(
                    content=f"❌ The leaderboard could not be generated: {error}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception(
                    "Could not report the leaderboard error for scrim %s.",
                    scrim.id,
                )
        else:
            await send_private_command_feedback(
                ctx,
                f"❌ The leaderboard could not be generated: {error}",
                silent=False,
            )
        return
    finally:
        await delete_command_message(ctx)


HELP_COPY_TEXT = (
    "A.R.C. HELP\n"
    "STAFF: !setup | !setres | !set @Role | !say text | !reset\n"
    "SLOTS: !add Team / TAG / @Captain | !confirm 03 04 | !remove 03 | "
    "!open | !close | !remind\n"
    "STATUS: !slots [Scrim] | !update [Scrim] | !res / !lb "
    "(posts leaderboard image)\n"
    f"!resg1-{MAX_MATCHES} slot kills "
    "(best team first; omit missed teams; review and confirm to save)\n"
    "!editres <match> (choose team, enter rank and kills)\n"
    "ROOM: !idpw room / minutes | !idpwg1-25 room / minutes\n"
    "CAPTAINS: !register Team / TAG [/ @Manager] | "
    "!cap add|transfer|remove @User\n"
    "Use ! for every command. Staff, registration, Manager, and Cap Transfer "
    "commands require their configured role/channel."
)


def build_help_text() -> str:
    """Return the condensed, Discord-formatted help content."""
    return (
        ">>> **A.R.C. HELP**\n"
        "**STAFF** `!setup` `!setres` `!set @Role` `!say text` "
        "`!reset`\n"
        "**SLOTS** `!add Team / TAG / @Captain` `!confirm 03 04` "
        "`!remove 03` `!open` `!close` `!remind`\n"
        "**STATUS** `!slots [Scrim]` `!update [Scrim]` "
        "`!res`/`!lb` (posts leaderboard image) "
        f"`!resg1-{MAX_MATCHES} slot kills` "
        "(best team first; omit missed teams; review and confirm to save) "
        "`!editres <match>` (choose team, enter rank and kills)\n"
        "**ROOM** `!idpw room / minutes` `!idpwg1-25 room / minutes`\n"
        "**CAPTAINS** `!register Team / TAG [/ @Manager]` "
        "`!cap add|transfer|remove @User`\n"
        "_Use `!` for every command. Configured roles and channels apply._"
    )


class HelpView(discord.ui.View):
    """Owner-only controls for the condensed help message."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "This help panel belongs to another user.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Copy text",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
    )
    async def copy_text(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            HELP_COPY_TEXT,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Close",
        emoji="✖️",
        style=discord.ButtonStyle.danger,
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Help closed.",
            view=None,
        )


@bot.command(name="help", aliases=["h"])
async def help_command(ctx: commands.Context) -> None:
    view = HelpView(ctx.author.id)
    await send_private_command_feedback(
        ctx,
        build_help_text(),
        delete_after=None,
        view=view,
    )


@bot.group(name="admin", invoke_without_command=True, hidden=True)
@commands.is_owner()
async def admin_command(ctx: commands.Context) -> None:
    await send_private_command_feedback(
        ctx,
        "Use `!admin add @User`, `!admin remove @User`, or `!admin list`.",
        silent=False,
    )


@admin_command.command(name="add")
@commands.is_owner()
async def admin_add(ctx: commands.Context, user: discord.User) -> None:
    try:
        added = repository.authorize_admin(user.id)
    except ValueError as error:
        await send_private_command_feedback(ctx, str(error), silent=False)
        return
    message = (
        f"User `{user.id}` is now an authorized bot admin."
        if added
        else f"User `{user.id}` is already an authorized bot admin."
    )
    await send_private_command_feedback(ctx, message, silent=False)


@admin_command.command(name="remove")
@commands.is_owner()
async def admin_remove(ctx: commands.Context, user: discord.User) -> None:
    try:
        removed = repository.revoke_admin(user.id)
    except ValueError as error:
        await send_private_command_feedback(ctx, str(error), silent=False)
        return
    message = (
        f"User `{user.id}` has been removed from bot admins."
        if removed
        else f"User `{user.id}` was not an authorized bot admin."
    )
    await send_private_command_feedback(ctx, message, silent=False)


@admin_command.command(name="list")
@commands.is_owner()
async def admin_list(ctx: commands.Context) -> None:
    admin_ids = repository.list_authorized_admin_ids()
    if admin_ids:
        message = "Authorized bot admins:\n" + "\n".join(
            f"• `{user_id}`" for user_id in admin_ids
        )
    else:
        message = "No authorized bot admins."
    await send_private_command_feedback(ctx, message, silent=False)


def build_auth_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="💎 A.R.C. Authorization Manager",
        description="Choose a version to view and manage its guild authorizations.",
        color=discord.Color.blurple(),
    )


def authorization_time_left_text(
    expires_at: datetime | None, duration_days: int
) -> str:
    if expires_at is None:
        return "Unlimited" if duration_days == 0 else "Expiry unavailable"
    expires_at = expires_at.astimezone(timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return f"Expired on {expires_at.strftime('%Y-%m-%d %H:%M UTC')}"
    return subscription_remaining_text(expires_at)


async def build_auth_tier_embed(license_type: str) -> discord.Embed:
    if license_type not in LICENSE_TYPES:
        raise ValueError("Choose a Standard or Gold version.")
    authorizations = repository.list_authorizations(
        include_expired=True,
        license_type=license_type,
    )
    embed = discord.Embed(
        title=f"💎 {license_type} Version — Guild Authorizations",
        color=(
            discord.Color.gold()
            if license_type == "Gold"
            else discord.Color.green()
        ),
    )
    if not authorizations:
        embed.description = f"No guilds are assigned to the {license_type} version."
        return embed

    visible_authorizations = authorizations[:25]
    names = await asyncio.gather(
        *(
            authorized_guild_name(guild_id)
            for guild_id, _, _ in visible_authorizations
        )
    )
    for (guild_id, expires_at, duration_days), name in zip(
        visible_authorizations, names
    ):
        safe_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(name or "Name unavailable")
        )
        embed.add_field(
            name=f"Guild: {safe_name} (`{guild_id}`)"[:256],
            value=f"**Time left:** {authorization_time_left_text(expires_at, duration_days)}",
            inline=False,
        )
    if len(authorizations) > len(visible_authorizations):
        embed.set_footer(
            text=(
                f"{len(authorizations) - len(visible_authorizations)} more guilds "
                "are not shown. Remove accepts a guild ID."
            )
        )
    return embed


def parse_auth_duration(value: str) -> int:
    normalized = value.strip().casefold()
    if normalized in {"0", "unlimited", "illimité", "illimite"}:
        return 0
    try:
        duration_days = int(normalized)
    except ValueError as error:
        raise ValueError(
            "Duration must be a non-negative number of days or `unlimited`."
        ) from error
    if duration_days < 0:
        raise ValueError(
            "Duration must be a non-negative number of days or `unlimited`."
        )
    return duration_days


class AuthAdminPanelView(DurableView):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.selected_tier: str | None = None
        self.message: discord.Message | None = None
        self.rebuild()

    async def user_is_authorized(
        self,
        user: discord.abc.User,
        *,
        owner_only: bool = False,
    ) -> bool:
        if getattr(user, "id", None) != self.owner_id:
            return False
        is_owner = await bot.is_owner(user)
        if owner_only:
            return is_owner
        return is_owner or repository.is_admin_authorized(user.id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This authorization panel belongs to another administrator.",
                ephemeral=True,
            )
            return False
        if not await self.user_is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are no longer authorized to use this panel.",
                ephemeral=True,
            )
            return False
        return True

    def _add_button(
        self,
        label: str,
        style: discord.ButtonStyle,
        callback,
    ) -> None:
        button = discord.ui.Button(label=label, style=style)
        button.callback = callback
        self.add_item(button)

    def rebuild(self) -> None:
        self.clear_items()
        if self.selected_tier is None:
            for tier in LICENSE_TYPES:
                async def select_tier(
                    interaction: discord.Interaction,
                    selected_tier: str = tier,
                ) -> None:
                    await self.show_tier(interaction, selected_tier)

                self._add_button(
                    tier,
                    discord.ButtonStyle.primary,
                    select_tier,
                )

            async def close_panel(interaction: discord.Interaction) -> None:
                await interaction.response.edit_message(
                    content="Authorization panel closed.",
                    embed=None,
                    view=None,
                )

            self._add_button(
                "Close",
                discord.ButtonStyle.secondary,
                close_panel,
            )
            return

        selected_tier = self.selected_tier

        async def add_guild(interaction: discord.Interaction) -> None:
            if not await self.user_is_authorized(
                interaction.user,
                owner_only=True,
            ):
                await interaction.response.send_message(
                    "Only the bot owner can add or change a guild authorization.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(
                AuthAddGuildModal(self, selected_tier)
            )

        async def remove_guild(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                AuthRemoveGuildModal(self, selected_tier)
            )

        async def return_to_versions(interaction: discord.Interaction) -> None:
            self.selected_tier = None
            self.rebuild()
            await interaction.response.edit_message(
                content="",
                embed=build_auth_panel_embed(),
                view=self,
            )

        self._add_button("Add", discord.ButtonStyle.success, add_guild)
        self._add_button("Remove", discord.ButtonStyle.danger, remove_guild)
        self._add_button("Return", discord.ButtonStyle.secondary, return_to_versions)

    async def show_tier(
        self,
        interaction: discord.Interaction,
        license_type: str,
    ) -> None:
        self.selected_tier = license_type
        self.rebuild()
        await interaction.response.defer()
        embed = await build_auth_tier_embed(license_type)
        await interaction.edit_original_response(
            content="",
            embed=embed,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def refresh_panel(self) -> None:
        if self.message is None or self.selected_tier is None:
            return
        try:
            await self.message.edit(
                content="",
                embed=await build_auth_tier_embed(self.selected_tier),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not refresh the authorization panel.")

    async def on_timeout(self) -> None:
        disable_view_items(self)
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class AuthAddGuildModal(discord.ui.Modal):
    def __init__(self, panel: AuthAdminPanelView, license_type: str):
        super().__init__(title=f"Add {license_type} authorization", timeout=300)
        self.panel = panel
        self.license_type = license_type
        self.guild_id_input = discord.ui.TextInput(
            label="Guild ID",
            placeholder="Enter the Discord server ID",
            required=True,
            max_length=20,
        )
        self.duration_input = discord.ui.TextInput(
            label="Days or unlimited",
            placeholder="For example: 30 or unlimited",
            default="unlimited",
            required=True,
            max_length=24,
        )
        self.add_item(self.guild_id_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.panel.user_is_authorized(
            interaction.user,
            owner_only=True,
        ):
            await interaction.response.send_message(
                "Only the bot owner can add or change a guild authorization.",
                ephemeral=True,
            )
            return
        try:
            guild_id = int(str(self.guild_id_input.value).strip())
            duration_days = parse_auth_duration(str(self.duration_input.value))
            already_authorized = guild_id in repository.authorized_guild_ids
            repository.authorize_guild(
                guild_id,
                duration_days,
                license_type=self.license_type,
            )
        except (TypeError, ValueError) as error:
            await interaction.response.send_message(
                f"Could not authorize that guild: {error}",
                ephemeral=True,
            )
            return
        except SlotStorageError:
            logger.exception("Could not save guild authorization.")
            await interaction.response.send_message(
                "The authorization could not be saved. Please try again.",
                ephemeral=True,
            )
            return

        duration_text = (
            "with unlimited access"
            if duration_days == 0
            else f"for {duration_days} day(s)"
        )
        status = "updated" if already_authorized else "added"
        await interaction.response.defer(ephemeral=True)
        await self.panel.refresh_panel()
        await interaction.followup.send(
            f"Guild `{guild_id}` authorization {status} as **{self.license_type}** "
            f"{duration_text}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class AuthRemoveGuildModal(discord.ui.Modal):
    def __init__(self, panel: AuthAdminPanelView, license_type: str):
        super().__init__(title=f"Remove {license_type} authorization", timeout=300)
        self.panel = panel
        self.license_type = license_type
        self.guild_id_input = discord.ui.TextInput(
            label="Guild ID",
            placeholder=f"Enter a {license_type} guild ID",
            required=True,
            max_length=20,
        )
        self.add_item(self.guild_id_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.panel.user_is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are no longer authorized to use this panel.",
                ephemeral=True,
            )
            return
        try:
            guild_id = int(str(self.guild_id_input.value).strip())
            tier_authorizations = repository.list_authorizations(
                include_expired=True,
                license_type=self.license_type,
            )
        except (TypeError, ValueError) as error:
            await interaction.response.send_message(
                f"Could not remove that guild: {error}",
                ephemeral=True,
            )
            return
        if guild_id not in {item[0] for item in tier_authorizations}:
            await interaction.response.send_message(
                f"Guild `{guild_id}` is not assigned to the "
                f"**{self.license_type}** version.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            repository.revoke_guild(guild_id)
        except (TypeError, ValueError) as error:
            await interaction.response.send_message(
                f"Could not remove that guild: {error}",
                ephemeral=True,
            )
            return
        except SlotStorageError:
            logger.exception("Could not save guild revocation.")
            await interaction.response.send_message(
                "The authorization could not be removed. Please try again.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.panel.refresh_panel()
        await interaction.followup.send(
            f"Guild `{guild_id}` was removed from the **{self.license_type}** version.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


@bot.command(name="auth", hidden=True)
@auth_admin_required()
async def auth_command(ctx: commands.Context) -> None:
    view = AuthAdminPanelView(owner_id=ctx.author.id)
    view.message = await send_private_command_feedback(
        ctx,
        "",
        embed=build_auth_panel_embed(),
        view=view,
        delete_after=300,
        silent=False,
    )


async def authorized_guild_name(guild_id: int) -> str | None:
    guild = bot.get_guild(guild_id)
    if guild is not None:
        return guild.name
    try:
        guild = await bot.fetch_guild(guild_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return guild.name


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    original = getattr(error, "original", error)
    command_name = getattr(ctx.command, "qualified_name", "")
    if isinstance(original, SlotStorageError):
        logger.error("Save error", exc_info=original)
        await send_private_command_feedback(
            ctx,
            "I could not save that change. Please try again.",
            silent=False,
        )
    elif isinstance(original, UnauthorizedGuild):
        await send_private_command_feedback(
            ctx,
            "This server is not authorized to use this bot.",
            silent=False,
        )
    elif isinstance(error, UnauthorizedAuthAdmin):
        await send_private_command_feedback(
            ctx,
            "❌ **Access Denied.** You do not have permission to manage the "
            "server whitelist.",
            silent=False,
        )
    elif isinstance(error, commands.NotOwner):
        await send_private_command_feedback(
            ctx,
            "This developer command is restricted to the bot owner.",
            silent=False,
        )
    elif isinstance(error, commands.MissingRequiredArgument):
        if command_name in {"confirm", "remove", "say"}:
            await send_private_command_feedback(ctx, "", silent=True)
            return
        await send_private_command_feedback(
            ctx,
            "A required argument is missing. Use `!help` to check the command format.",
            silent=False,
        )
    elif isinstance(error, commands.BadArgument):
        await send_private_command_feedback(
            ctx,
            "One of the arguments is invalid. Use `!help` to check the command format.",
            silent=False,
        )
    elif isinstance(error, commands.MissingPermissions):
        await send_private_command_feedback(
            ctx,
            "You do not have permission to use this command.",
            silent=False,
        )
    elif isinstance(error, commands.NoPrivateMessage):
        await send_private_command_feedback(
            ctx,
            "This command can only be used inside a Discord server.",
            silent=False,
        )
    elif isinstance(error, commands.CommandNotFound):
        await delete_command_message(ctx)
    else:
        logger.exception("Command execution error", exc_info=error)
        await send_private_command_feedback(
            ctx,
            "The command could not be completed. Please try again.",
            silent=False,
        )


def install_setup_panel() -> None:
    global _setup_installed, _global_setup_installed
    import setup_panel
    import global_setup

    if not _setup_installed:
        setup_panel.install_setup(
            bot,
            repository,
            publish_scrim,
            log_action=send_scrim_log,
        )
        _setup_installed = True
    if not _global_setup_installed:
        global_setup.install_global_setup(bot, repository)
        _global_setup_installed = True


@bot.event
async def setup_hook() -> None:
    global _loaded
    if not _loaded:
        repository.load()
        _loaded = True
    install_setup_panel()
    bot.add_dynamic_items(ManagerActionButton, StaffActionButton)


async def migrate_legacy() -> None:
    if repository.legacy is None:
        return
    board = repository.legacy.get("public_board")
    if not isinstance(board, dict) or not board.get("channel_id"):
        logger.warning("Unclaimed v1 state: no resolvable public channel.")
        return
    try:
        channel = await get_channel(int(board["channel_id"]))
    except (discord.HTTPException, ValueError, TypeError):
        logger.exception("Retaining v1 state: public channel not found.")
        return
    guild = getattr(channel, "guild", None)
    if guild is None:
        logger.warning("Retaining v1 state: unable to determine the server.")
        return
    staff = None
    configured_id = os.getenv("STAFF_CHANNEL_ID", "").strip()
    if configured_id.isdigit():
        staff = guild.get_channel(int(configured_id))
    if staff is None:
        name = os.getenv("STAFF_CHANNEL_NAME", "staff").strip().lstrip("#")
        staff = discord.utils.get(guild.text_channels, name=name)
    if staff is None:
        logger.warning("Retaining v1 state: unable to resolve the staff channel.")
        return
    try:
        scrim = repository.claim_legacy(guild.id, staff.id)
        await publish_scrim(scrim)
        logger.info("Imported v1 state into scrim %s.", scrim.id)
    except Exception:
        logger.exception("v1 migration failed; state was retained.")


@bot.event
async def on_ready() -> None:
    if bot.user is not None:
        logger.info("Bot connected as %s (ID: %s)", bot.user, bot.user.id)
    await migrate_legacy()
    results = await asyncio.gather(
        *(
            publish_scrim(scrim)
            for scrim in list(repository.scrims.values())
            if repository.is_guild_authorized(scrim.guild_id)
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            logger.error("Board recovery failed.", exc_info=result)
    for scrim in list(repository.scrims.values()):
        if repository.is_guild_authorized(scrim.guild_id):
            await send_scrim_log_coverage_snapshot(scrim)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    """Leave immediately after notifying a guild outside the whitelist."""
    if repository.is_guild_authorized(guild.id):
        return

    me = guild.me
    candidates: list[discord.TextChannel] = []
    if isinstance(guild.system_channel, discord.TextChannel):
        candidates.append(guild.system_channel)
    candidates.extend(
        channel
        for channel in guild.text_channels
        if channel not in candidates
    )
    channel = next(
        (
            candidate
            for candidate in candidates
            if me is None
            or candidate.permissions_for(me).send_messages
        ),
        None,
    )
    embed = discord.Embed(
        description=(
            "❌ **Unauthorized Server.** A.R.C. is a private bot. "
            "To purchase or request access, please contact the developer."
        ),
        color=discord.Color.red(),
    )
    try:
        if channel is not None:
            await channel.send(embed=embed)
        else:
            logger.warning(
                "No sendable text channel found in unauthorized guild %s.",
                guild.id,
            )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Could not notify unauthorized guild %s before leaving.",
            guild.id,
        )
    finally:
        try:
            await guild.leave()
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not leave unauthorized guild %s.", guild.id)


def member_has_manager_role(member: object, guild_id: int) -> bool:
    """Check the server-wide Manager role configured through !set."""
    config = repository.get_server_config(guild_id)
    manager_role_id = (
        getattr(config, "manager_role_id", None)
        if config is not None
        else None
    )
    if manager_role_id is None and config is not None:
        # Current persisted configs store the !set Manager role as staff_role_id.
        manager_role_id = config.staff_role_id
    return bool(
        manager_role_id is not None
        and manager_role_id in {
            getattr(role, "id", None) for role in getattr(member, "roles", ())
        }
    )


async def scrim_category_ids(scrim: Scrim) -> set[int]:
    """Resolve the Discord categories containing a scrim's public/staff channels."""
    category_ids: set[int] = set()
    for channel_id in {scrim.public_channel_id, scrim.staff_channel_id}:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not resolve channel %s while routing !slots.",
                    channel_id,
                )
                continue
        category_id = getattr(channel, "category_id", None)
        if category_id is None:
            category = getattr(channel, "category", None)
            category_id = getattr(category, "id", None)
        if type(category_id) is int:
            category_ids.add(category_id)
    return category_ids


def build_slot_status_embed(scrim: Scrim) -> discord.Embed:
    """Build the compact status summary used by the !slots command."""
    counts = {
        STATUS_AVAILABLE: 0,
        STATUS_RESERVED: 0,
        STATUS_PENDING: 0,
        STATUS_CONFIRMED: 0,
    }
    for slot in scrim.slots.values():
        if slot.status in counts:
            counts[slot.status] += 1

    embed = discord.Embed(
        title=f"📊 Slot Status - {discord.utils.escape_markdown(scrim.name)}",
        description=(
            f"**Total Slots:** {len(scrim.slots)}\n\n"
            f"{scrim.emoji_available} **Free:** "
            f"{counts[STATUS_AVAILABLE]}\n"
            f"{scrim.emoji_reserved} **Reserved:** "
            f"{counts[STATUS_RESERVED]}\n"
            f"{scrim.emoji_pending} **Pending:** "
            f"{counts[STATUS_PENDING]}\n"
            f"{scrim.emoji_confirmed} **Confirmed:** "
            f"{counts[STATUS_CONFIRMED]}"
        ),
        color=discord.Color.blurple(),
    )
    available = [
        f"`{slot.number:02d}`"
        for slot in sorted(scrim.slots.values(), key=lambda item: item.number)
        if slot.status == STATUS_AVAILABLE
    ]
    embed.add_field(
        name=f"{scrim.emoji_available} Available Slots ({counts[STATUS_AVAILABLE]})",
        value=", ".join(available) if available else "None",
        inline=False,
    )

    status_labels = (
        (STATUS_RESERVED, "Reserved Teams", scrim.emoji_reserved),
        (STATUS_PENDING, "Pending Teams", scrim.emoji_pending),
        (STATUS_CONFIRMED, "Confirmed Teams", scrim.emoji_confirmed),
    )
    for status, field_name, status_emoji in status_labels:
        teams = []
        for slot in sorted(scrim.slots.values(), key=lambda item: item.number):
            if slot.status != status:
                continue
            team_name = (
                discord.utils.escape_mentions(
                    discord.utils.escape_markdown(
                        str(getattr(slot, "team_name", "") or "").strip()
                    )
                )
                or "Unnamed team"
            )
            tag = (
                discord.utils.escape_mentions(
                    discord.utils.escape_markdown(
                        str(getattr(slot, "tag", "") or "").strip()
                    )
                )
                or "No tag"
            )
            teams.append(f"`{slot.number:02d}` {team_name} · `{tag}`")
        embed.add_field(
            name=f"{status_emoji} {field_name} ({counts[status]})",
            value="\n".join(teams) if teams else "None",
            inline=False,
        )
    embed.set_footer(text="Public status view · team names and slot states only")
    return embed


async def send_slot_status_embed(ctx: commands.Context, scrim: Scrim) -> None:
    try:
        await ctx.send(
            embed=build_slot_status_embed(scrim),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception("Could not send the slot status summary for %s.", scrim.id)
    finally:
        await delete_command_message(ctx)


@dataclass(frozen=True)
class LeaderboardRow:
    slot_number: int
    team_name: str
    wins: int
    kills: int
    placement_points: int
    total_points: int


def calculate_leaderboard(scrim: Scrim) -> list[LeaderboardRow]:
    """Calculate totals for every registered team in a scrim."""
    placement_points = parse_placement_points(
        getattr(scrim, "placement_points_string", DEFAULT_PLACEMENT_POINTS_STRING)
    )
    registered_slots = [
        slot
        for slot in scrim.slots.values()
        if slot.status != STATUS_AVAILABLE and slot.team_name
    ]
    if len(placement_points) < len(registered_slots):
        placement_points.extend([0] * (len(registered_slots) - len(placement_points)))
    scores = getattr(scrim, "match_scores", {})
    rows: list[LeaderboardRow] = []
    for slot in registered_slots:
        wins = 0
        kills = 0
        placement_total = 0
        for score in scores.values():
            if score.slot_number != slot.number:
                continue
            if score.placement == 1:
                wins += 1
            kills += score.kills
            placement_index = score.placement - 1
            if 0 <= placement_index < len(placement_points):
                placement_total += placement_points[placement_index]
        total = kills * getattr(scrim, "kill_points_value", 1) + placement_total
        rows.append(
            LeaderboardRow(
                slot_number=slot.number,
                team_name=slot.team_name,
                wins=wins,
                kills=kills,
                placement_points=placement_total,
                total_points=total,
            )
        )
    ranked_rows = sorted(
        rows,
        key=lambda row: (
            -row.total_points,
            -row.placement_points,
            -row.kills,
            row.slot_number,
        ),
    )
    _, team_count = _leaderboard_scrim_profile(scrim)
    if team_count not in LEADERBOARD_TEAM_COUNTS:
        raise ValueError("Invalid leaderboard team count.")
    return ranked_rows[:team_count]


def build_results_publication_message(
    scrim: Scrim,
    rows: list[LeaderboardRow],
) -> str:
    """Format the approved results template using the leaderboard snapshot."""
    scores = getattr(scrim, "match_scores", {})
    registered_teams = sum(
        1
        for slot in getattr(scrim, "slots", {}).values()
        if slot.status != STATUS_AVAILABLE and slot.team_name
    )
    replacements = {
        "scrim": discord.utils.escape_markdown(
            str(getattr(scrim, "name", "Scrim"))
        ),
        "team_count": str(registered_teams),
        "match_count": str(
            len(
                {
                    score.match_number
                    for score in scores.values()
                }
            )
        ),
    }
    for rank in range(1, 4):
        row = rows[rank - 1] if len(rows) >= rank else None
        replacements.update(
            {
                f"top{rank}_team": (
                    discord.utils.escape_markdown(row.team_name)
                    if row is not None
                    else "—"
                ),
                f"top{rank}_points": (
                    str(row.total_points) if row is not None else "0"
                ),
                f"top{rank}_kills": str(row.kills) if row is not None else "0",
                f"top{rank}_wins": str(row.wins) if row is not None else "0",
            }
        )

    template = getattr(scrim, "operational_messages", {}).get(
        "publish_results",
        DEFAULT_OPERATIONAL_MESSAGES["publish_results"],
    )
    try:
        message = template.format(**replacements)
    except (KeyError, ValueError):
        logger.warning(
            "Invalid results message template for scrim %s; using the default.",
            getattr(scrim, "id", None),
        )
        message = DEFAULT_OPERATIONAL_MESSAGES["publish_results"].format(
            **replacements
        )
    return message if len(message) <= 2000 else f"{message[:1997]}..."


def parse_match_score_lines(
    input_string: str,
    *,
    match_number: int,
    scrim: Scrim,
) -> list[MatchScore]:
    """Parse ordered `slot kills` lines or legacy explicit-placement lines."""
    scores: list[MatchScore] = []
    seen_slots: set[int] = set()
    lines = [
        (line_number, line.strip())
        for line_number, line in enumerate(str(input_string).splitlines(), start=1)
        if line.strip()
    ]
    if not lines:
        return scores

    field_counts = {len(line.split()) for _, line in lines}
    if not field_counts.issubset({2, 3}):
        invalid_line = next(
            line_number
            for line_number, line in lines
            if len(line.split()) not in {2, 3}
        )
        raise ValueError(
            f"Line {invalid_line} must contain `slot kills` "
            "or legacy `slot kills placement`."
        )
    if len(field_counts) != 1:
        raise ValueError(
            "Do not mix ordered `slot kills` lines with legacy "
            "`slot kills placement` lines."
        )

    ordered_by_line = field_counts == {2}
    for placement, (line_number, line) in enumerate(lines, start=1):
        parts = line.split()
        try:
            values = [int(part) for part in parts]
        except ValueError:
            raise ValueError(
                f"Line {line_number} must contain only whole numbers."
            ) from None
        if ordered_by_line:
            slot_number, kills = values
        else:
            slot_number, kills, placement = values
        if slot_number in seen_slots:
            raise ValueError(
                f"Line {line_number} repeats slot {slot_number}."
            )
        if (
            slot_number not in scrim.slots
            or scrim.slots[slot_number].status == STATUS_AVAILABLE
        ):
            raise ValueError(
                f"Line {line_number} uses slot {slot_number}, "
                "which is not assigned to a team."
            )
        if kills < 0:
            raise ValueError(f"Line {line_number} has a negative kill count.")
        if placement < 1:
            raise ValueError(f"Line {line_number} has an invalid placement.")
        seen_slots.add(slot_number)
        scores.append(
            MatchScore(
                match_number=match_number,
                slot_number=slot_number,
                kills=kills,
                placement=placement,
            )
        )
    return scores


def _load_font(size: int, *, weight: int = 400):
    """Load a requested Montserrat weight from the bundled variable font."""
    font = ImageFont.truetype(str(LEADERBOARD_FONT_PATH), size)
    variation_name = LEADERBOARD_FONT_VARIATIONS.get(weight)
    if variation_name is None:
        font.set_variation_by_axes([weight])
    else:
        font.set_variation_by_name(variation_name)
    return font


def _fit_font(
    text: str,
    *,
    max_width: int,
    max_height: int,
    max_size: int = 64,
    min_size: int = 12,
    weight: int = 400,
):
    """Choose the largest requested Montserrat weight that fits its area."""
    for size in range(max_size, min_size - 1, -1):
        font = _load_font(size, weight=weight)
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
        if right - left <= max_width and bottom - top <= max_height:
            return font
    return _load_font(min_size, weight=weight)


def _leaderboard_field_ranges(
    left: int,
    total_width: int,
) -> tuple[tuple[int, int], ...]:
    """Scale the six leaderboard fields to the width of one table column."""
    base_total = sum(LEADERBOARD_FIELD_BASE_WIDTHS)
    exact_widths = [
        width * total_width / base_total
        for width in LEADERBOARD_FIELD_BASE_WIDTHS
    ]
    widths = [int(width) for width in exact_widths]
    remaining = total_width - sum(widths)
    order = sorted(
        range(len(widths)),
        key=lambda index: exact_widths[index] - widths[index],
        reverse=True,
    )
    for index in order[:remaining]:
        widths[index] += 1

    ranges = []
    field_left = left
    for field_width in widths:
        ranges.append((field_left, field_left + field_width))
        field_left += field_width
    return tuple(ranges)


def _fit_leaderboard_cell_text(
    text: object,
    *,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int = 8,
    weight: int = 400,
) -> tuple[str, ImageFont.FreeTypeFont]:
    """Shrink text to fit a cell, shortening only as a last resort."""
    text = " ".join(str(text).split())
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in range(max_size, min_size - 1, -1):
        font = _load_font(size, weight=weight)
        left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
        if right - left <= max_width and bottom - top <= max_height:
            return text, font

    font = _load_font(min_size, weight=weight)
    ellipsis = "…"
    for end in range(len(text), -1, -1):
        candidate = text[:end].rstrip() + ellipsis
        left, top, right, bottom = probe.textbbox((0, 0), candidate, font=font)
        if right - left <= max_width and bottom - top <= max_height:
            return candidate, font
    return "", font


def leaderboard_canvas_dimensions(
    team_count: int,
    orientation: str,
    header_height: int | None = None,
    footer_height: int | None = None,
) -> tuple[int, int]:
    """Return canvas dimensions with the fixed header and footer bands."""
    if team_count not in LEADERBOARD_TEAM_COUNTS:
        raise ValueError("Choose 16, 18, 20, 22, or 24 teams.")
    if orientation not in LEADERBOARD_ORIENTATIONS:
        raise ValueError("Choose a vertical or horizontal layout.")
    if header_height not in (None, DEFAULT_LEADERBOARD_HEADER_HEIGHT):
        raise ValueError(
            f"Leaderboard header height is fixed at "
            f"{DEFAULT_LEADERBOARD_HEADER_HEIGHT}px."
        )
    if footer_height not in (None, DEFAULT_LEADERBOARD_FOOTER_HEIGHT):
        raise ValueError(
            f"Leaderboard footer height is fixed at "
            f"{DEFAULT_LEADERBOARD_FOOTER_HEIGHT}px."
        )
    header_height = DEFAULT_LEADERBOARD_HEADER_HEIGHT
    footer_height = DEFAULT_LEADERBOARD_FOOTER_HEIGHT
    width = (
        LEADERBOARD_VERTICAL_WIDTH
        if orientation == "vertical"
        else LEADERBOARD_HORIZONTAL_WIDTH
    )
    visible_rows = (
        team_count
        if orientation == "vertical"
        else (team_count + 1) // 2
    )
    row_height = (
        LEADERBOARD_VERTICAL_ROW_HEIGHT
        if orientation == "vertical"
        else LEADERBOARD_HORIZONTAL_ROW_HEIGHT
    )
    height = (
        2 * LEADERBOARD_OUTER_MARGIN
        + header_height
        + LEADERBOARD_SECTION_GAP
        + LEADERBOARD_TABLE_HEADER_HEIGHT
        + visible_rows * row_height
        + LEADERBOARD_SECTION_GAP
        + footer_height
    )
    return width, height


def _build_empty_leaderboard_blueprint(scrim: Scrim) -> tuple[io.BytesIO, str]:
    """Create a blank background canvas at the scrim profile's exact size."""
    orientation, team_count = _leaderboard_scrim_profile(scrim)
    width, height = leaderboard_canvas_dimensions(
        team_count,
        orientation,
    )
    output = _load_leaderboard_background_canvas(
        LEADERBOARD_BACKGROUND,
        (width, height),
    )

    buffer = io.BytesIO()
    output.convert("RGB").save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    filename = (
        f"leaderboard-blueprint-{team_count}-{orientation}-"
        f"{width}x{height}.png"
    )
    return buffer, filename


def _generate_default_leaderboard_background(
    size: tuple[int, int],
) -> Image.Image:
    """Create a solid, high-contrast canvas when no bundled image exists."""
    return Image.new("RGBA", size, (13, 18, 26, 255))


def _load_leaderboard_background_canvas(
    background_path: Path,
    size: tuple[int, int],
) -> Image.Image:
    """Fit a background asset or generate the default if that asset is absent."""
    if background_path.is_file():
        with Image.open(background_path) as background:
            return ImageOps.fit(
                background.convert("RGB"),
                size,
                method=Image.Resampling.LANCZOS,
            ).convert("RGBA")
    if background_path == LEADERBOARD_BACKGROUND:
        return _generate_default_leaderboard_background(size)
    raise FileNotFoundError(
        f"Configured leaderboard background is missing: {background_path}"
    )


def leaderboard_blueprint_reference_label(scrim: Scrim) -> str:
    orientation, team_count = _leaderboard_scrim_profile(scrim)
    width, height = leaderboard_canvas_dimensions(team_count, orientation)
    return (
        f"dimensioned {team_count}-team {orientation.title()} reference "
        f"({width} × {height} px)"
    )


def _load_dimensioned_leaderboard_blueprint(
    scrim: Scrim,
) -> tuple[io.BytesIO, str]:
    """Load the dimensioned reference matching the active leaderboard profile."""
    orientation, team_count = _leaderboard_scrim_profile(scrim)
    expected_size = leaderboard_canvas_dimensions(team_count, orientation)
    path = LEADERBOARD_BLUEPRINT_DIR / (
        f"{orientation}-{team_count}-complete-dimensions.png"
    )
    with Image.open(path) as blueprint:
        if blueprint.size != expected_size:
            raise ValueError(
                f"The dimensioned {team_count}-team {orientation} reference "
                f"must be {expected_size[0]} × {expected_size[1]} px."
            )
        blueprint.load()
    buffer = io.BytesIO(path.read_bytes())
    buffer.seek(0)
    return (
        buffer,
        f"leaderboard-blueprint-{orientation}-{team_count}-dimensions.png",
    )


def _build_configured_leaderboard_image(
    scrim: Scrim,
    rows: list[LeaderboardRow],
    *,
    background_path: Path = LEADERBOARD_BACKGROUND,
) -> io.BytesIO:
    orientation, team_limit = _leaderboard_scrim_profile(scrim)
    header_height = DEFAULT_LEADERBOARD_HEADER_HEIGHT

    rows = rows[:team_limit]
    width, height = leaderboard_canvas_dimensions(
        team_limit,
        orientation,
    )
    footer_height = DEFAULT_LEADERBOARD_FOOTER_HEIGHT
    output = _load_leaderboard_background_canvas(
        background_path,
        (width, height),
    )
    margin = LEADERBOARD_OUTER_MARGIN
    text = _leaderboard_accent_rgb(
        _leaderboard_profile_accent_color(scrim)
    )

    table_top = margin + header_height + LEADERBOARD_SECTION_GAP
    draw = ImageDraw.Draw(output, "RGBA")
    date_label = datetime.now(
        timezone_for_name(getattr(scrim, "timezone", "UTC"))
    ).strftime("%d/%m/%Y")
    date_font = _load_font(LEADERBOARD_DATE_FONT_SIZE, weight=700)
    draw.text(
        (width - margin, margin),
        date_label,
        font=date_font,
        fill=text,
        anchor="rt",
    )
    if background_path == LEADERBOARD_BACKGROUND:
        scrim_title = str(getattr(scrim, "name", "") or "").strip()
        if scrim_title:
            title_center_x = width // 2
            title_half_width = min(
                title_center_x - margin,
                width - margin - title_center_x,
            )
            title_font = _fit_font(
                scrim_title,
                max_width=max(80, 2 * title_half_width),
                max_height=header_height - 24,
                max_size=64,
                min_size=18,
                weight=LEADERBOARD_BODY_FONT_WEIGHT,
            )
            draw.text(
                (title_center_x, margin + header_height // 2),
                scrim_title,
                font=title_font,
                fill=text,
                anchor="mm",
            )

    columns = 2 if orientation == "horizontal" else 1
    column_gap = 24 if columns == 2 else 0
    row_height = (
        LEADERBOARD_HORIZONTAL_ROW_HEIGHT
        if columns == 2
        else LEADERBOARD_VERTICAL_ROW_HEIGHT
    )
    column_width = (
        width - 2 * margin - column_gap * (columns - 1)
    ) // columns
    per_column_capacity = (
        (team_limit + 1) // 2 if columns == 2 else team_limit
    )
    split_index = per_column_capacity
    row_font = (
        LEADERBOARD_HORIZONTAL_ROW_FONT_SIZE
        if columns == 2
        else LEADERBOARD_VERTICAL_ROW_FONT_SIZE
    )
    row_top = table_top + LEADERBOARD_TABLE_HEADER_HEIGHT

    for column_index in range(columns):
        x = margin + column_index * (column_width + column_gap)
        if columns == 2:
            column_rows = (
                rows[:split_index]
                if column_index == 0
                else rows[split_index:]
            )
        else:
            column_rows = rows

        field_ranges = _leaderboard_field_ranges(x, column_width)
        field_centers = tuple(
            (field_left + field_right) / 2
            for field_left, field_right in field_ranges
        )
        for row_index in range(per_column_capacity):
            y = row_top + row_index * row_height
            text_y = y + row_height // 2
            rank = column_index * per_column_capacity + row_index + 1
            rank_text, rank_font = _fit_leaderboard_cell_text(
                f"{rank:02d}",
                max_width=field_ranges[0][1] - field_ranges[0][0] - 4,
                max_height=row_height - 8,
                max_size=row_font,
                weight=LEADERBOARD_ROW_FONT_WEIGHT,
            )
            draw.text(
                (field_centers[0], text_y),
                rank_text,
                font=rank_font,
                fill=text,
                anchor="mm",
            )
            if row_index >= len(column_rows):
                continue
            row = column_rows[row_index]
            team_text, team_font = _fit_leaderboard_cell_text(
                row.team_name,
                max_width=max(
                    1,
                    field_ranges[1][1]
                    - field_ranges[1][0]
                    - LEADERBOARD_TEAM_NAME_LEFT_PADDING
                    - LEADERBOARD_TEAM_NAME_LEFT_PADDING,
                ),
                max_height=row_height - 8,
                max_size=row_font,
                min_size=8,
                weight=LEADERBOARD_ROW_FONT_WEIGHT,
            )
            draw.text(
                (
                    field_ranges[1][0] + LEADERBOARD_TEAM_NAME_LEFT_PADDING,
                    text_y,
                ),
                team_text,
                font=team_font,
                fill=text,
                anchor="lm",
            )
            for field_index, value in enumerate(
                (
                    row.wins,
                    row.kills,
                    row.placement_points,
                    row.total_points,
                ),
                start=2,
            ):
                value_text, value_font = _fit_leaderboard_cell_text(
                    value,
                    max_width=(
                        field_ranges[field_index][1]
                        - field_ranges[field_index][0]
                        - 10
                    ),
                    max_height=row_height - 8,
                    max_size=row_font,
                    weight=LEADERBOARD_ROW_FONT_WEIGHT,
                )
                draw.text(
                    (field_centers[field_index], text_y),
                    value_text,
                    font=value_font,
                    fill=text,
                    anchor="mm",
                )
    buffer = io.BytesIO()
    output.convert("RGB").save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def build_leaderboard_image(
    scrim: Scrim,
    rows: list[LeaderboardRow] | None = None,
    *,
    background_path: Path | None = None,
) -> io.BytesIO:
    """Render the configured leaderboard as a PNG image."""
    if rows is None:
        rows = calculate_leaderboard(scrim)
    selected_background = (
        background_path
        if background_path is not None
        else _current_leaderboard_background_path(scrim)
    )
    return _build_configured_leaderboard_image(
        scrim,
        rows,
        background_path=selected_background,
    )


class SlotsScrimSelectView(discord.ui.View):
    """Let a Manager choose an active scrim for the !slots summary."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrims: list[Scrim],
    ) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.scrim_ids = {scrim.id for scrim in scrims}

        select = discord.ui.Select(
            placeholder="📊 Select a scrim to view...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=scrim.name[:100],
                    value=scrim.id,
                )
                for scrim in scrims
            ],
        )

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This scrim selector belongs to another user.",
                    ephemeral=True,
                )
                return
            if interaction.guild is None or interaction.guild.id != self.guild_id:
                await interaction.response.send_message(
                    "This selector is not valid in this server.",
                    ephemeral=True,
                )
                return
            if not member_has_manager_role(interaction.user, self.guild_id):
                await interaction.response.send_message(
                    "❌ You do not have permission to use this command.",
                    ephemeral=True,
                )
                self.stop()
                return

            selected_id = select.values[0]
            if selected_id not in self.scrim_ids:
                await interaction.response.send_message(
                    "That scrim selection is no longer available.",
                    ephemeral=True,
                )
                self.stop()
                return

            scrim = repository.get(selected_id)
            if (
                scrim is None
                or scrim.guild_id != self.guild_id
                or scrim.deleted
            ):
                await interaction.response.send_message(
                    "That scrim is no longer available.",
                    ephemeral=True,
                )
                self.stop()
                return

            try:
                await interaction.response.edit_message(
                    content=None,
                    embed=build_slot_status_embed(scrim),
                    view=None,
                )
            except discord.HTTPException:
                logger.exception("Could not render the selected !slots summary.")
            finally:
                self.stop()

        select.callback = callback
        self.add_item(select)


@bot.command(name="slots", aliases=["s"])
@commands.guild_only()
async def show_slots(ctx: commands.Context, *, name: str | None = None) -> None:
    """Show compact slot statistics for an active scrim."""
    if not member_has_manager_role(ctx.author, ctx.guild.id):
        await send_private_command_feedback(
            ctx,
            "❌ You do not have permission to use this command.",
            silent=False,
            delete_after=3,
        )
        return

    active_scrims = repository.list(ctx.guild.id)
    if not active_scrims:
        await send_private_command_feedback(
            ctx,
            "❌ No active scrims found.",
            silent=False,
            delete_after=3,
        )
        return

    if name:
        scrim = resolve_named_scrim(ctx.guild.id, name)
        if scrim is None:
            await send_private_command_feedback(
                ctx,
                "❌ No active scrim found with that name.",
                silent=False,
                delete_after=3,
            )
            return
        await send_slot_status_embed(ctx, scrim)
        return

    channel_scrim = resolve_channel_scrim(ctx)
    if channel_scrim is not None:
        await send_slot_status_embed(ctx, channel_scrim)
        return

    view = SlotsScrimSelectView(
        owner_id=ctx.author.id,
        guild_id=ctx.guild.id,
        scrims=active_scrims,
    )
    try:
        await ctx.send(
            "📊 Select a scrim to view its slot status:",
            view=view,
            delete_after=120,
        )
    except discord.HTTPException:
        logger.exception("Could not show the !slots scrim selector.")
    finally:
        await delete_command_message(ctx)


class UpdateScrimSelectView(discord.ui.View):
    """Let staff choose which active scrim should be published or refreshed."""

    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        scrims: list[Scrim],
    ) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.scrim_ids = {scrim.id for scrim in scrims}

        select = discord.ui.Select(
            placeholder="🔄 Select a scrim to update...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=scrim.name[:100],
                    value=scrim.id,
                )
                for scrim in scrims
            ],
        )

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This scrim selector belongs to another user.",
                    ephemeral=True,
                )
                return
            if interaction.guild is None or interaction.guild.id != self.guild_id:
                await interaction.response.send_message(
                    "This selector is not valid in this server.",
                    ephemeral=True,
                )
                return

            selected_id = select.values[0]
            if selected_id not in self.scrim_ids:
                await interaction.response.send_message(
                    "That scrim selection is no longer available.",
                    ephemeral=True,
                )
                self.stop()
                return

            scrim = repository.get(selected_id)
            if (
                scrim is None
                or scrim.guild_id != self.guild_id
                or scrim.deleted
            ):
                await interaction.response.send_message(
                    "That scrim is no longer available.",
                    ephemeral=True,
                )
                self.stop()
                return
            if not member_is_staff(interaction.user, scrim):
                await interaction.response.send_message(
                    "You do not have the authorized staff role for this scrim.",
                    ephemeral=True,
                )
                self.stop()
                return

            await interaction.response.defer(ephemeral=True)
            updated = await publish_scrim(scrim)
            self.stop()
            disable_view_items(self)
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("Could not close the !update selector.")
            await interaction.followup.send(
                "The slot board has been updated."
                if updated
                else "The slot board could not be updated.",
                ephemeral=True,
            )

        select.callback = callback
        self.add_item(select)


@bot.command(name="update", aliases=["u", "publier_slots"])
@commands.guild_only()
async def update_slots_command(
    ctx: commands.Context, *, name: str | None = None
) -> None:
    """Publish or refresh the full slot board for an active scrim."""
    active_scrims = repository.list(ctx.guild.id)
    if not active_scrims:
        await send_private_command_feedback(
            ctx,
            "No active scrims are configured for this server.",
            silent=True,
        )
        return

    if name:
        scrim = resolve_named_scrim(ctx.guild.id, name)
        if scrim is None:
            await send_private_command_feedback(
                ctx,
                "Scrim not found for this server.",
                silent=True,
            )
            return
        if not member_is_staff(ctx.author, scrim):
            await send_private_command_feedback(
                ctx,
                "You must have the authorized staff role to update this board.",
                silent=True,
            )
            return
        if await publish_scrim(scrim):
            await send_private_command_feedback(ctx, "The slot board has been updated.")
        return

    channel_scrim = resolve_channel_scrim(ctx)
    if channel_scrim is not None:
        if not member_is_staff(ctx.author, channel_scrim):
            await send_private_command_feedback(
                ctx,
                "You must have the authorized Staff role to update this board.",
                silent=True,
            )
            return
        if await publish_scrim(channel_scrim):
            await send_private_command_feedback(ctx, "The slot board has been updated.")
        return

    view = UpdateScrimSelectView(
        owner_id=ctx.author.id,
        guild_id=ctx.guild.id,
        scrims=active_scrims,
    )
    try:
        await ctx.send(
            "🔄 Select a scrim to update:",
            view=view,
            delete_after=120,
        )
    except discord.HTTPException:
        logger.exception("Could not show the !update scrim selector.")
    finally:
        await delete_command_message(ctx)


@bot.command(name="say", aliases=["announce"])
@commands.guild_only()
async def say_message(ctx: commands.Context, *, message: str) -> None:
    """Publish an anonymous bot message for authorized staff."""
    if not member_has_global_staff_role(ctx.author, ctx.guild.id):
        await send_private_command_feedback(
            ctx,
            "You do not have the Staff role authorized for this server.",
            silent=True,
        )
        return
    message = message.strip()
    if not message or len(message) > 2000:
        await send_private_command_feedback(ctx, "", silent=True)
        return
    if not await delete_command_message(ctx):
        logger.warning(
            "Could not delete the !say command in channel %s; announcement skipped.",
            ctx.channel.id,
        )
        return
    try:
        await ctx.send(
            message,
            allowed_mentions=discord.AllowedMentions(
                everyone=True,
                roles=True,
                users=True,
            ),
        )
    except discord.HTTPException:
        logger.exception(
            "Could not publish staff announcement in channel %s.",
            ctx.channel.id,
        )


@dataclass
class AddDraftEntry:
    original_line: str
    team_name: str
    slot_number: int
    member: discord.Member
    tag: str = ""


def add_preview_embed(
    scrim: Scrim,
    valid_entries: list[AddDraftEntry],
    error_entries: list[str],
    *,
    title: str = "🔍 Registration Preview",
    color: discord.Color = discord.Color.blurple(),
) -> discord.Embed:
    lines = []
    if valid_entries:
        lines.append("**Valid registrations**")
        lines.extend(
            f"✅ Slot {entry.slot_number:02d}: "
            f"{discord.utils.escape_mentions(entry.team_name)} "
            f"(Cap: <@{entry.member.id}>)"
            for entry in valid_entries
        )
    if error_entries:
        if lines:
            lines.append("")
        lines.append("**Entries requiring attention**")
        lines.extend(
            f"⚠️ Failed: {discord.utils.escape_mentions(error)}"
            for error in error_entries
        )
    if not lines:
        lines.append("No registrations were provided.")
    embed = discord.Embed(
        title=title,
        description="\n".join(lines)[:4096],
        color=color,
    )
    embed.set_footer(text=f"Scrim: {scrim.name}")
    return embed


async def resolve_add_member(
    ctx: commands.Context,
    member_text: str,
) -> discord.Member:
    """Resolve a mention or member reference without accepting arbitrary IDs."""
    mention_match = re.fullmatch(r"<@!?(\d+)>", member_text.strip())
    if mention_match is not None and ctx.guild is not None:
        member_id = int(mention_match.group(1))
        member = ctx.guild.get_member(member_id)
        if member is None:
            member = await ctx.guild.fetch_member(member_id)
        return member
    return await commands.MemberConverter().convert(ctx, member_text)


async def build_add_draft(
    ctx: commands.Context,
    scrim: Scrim,
    arguments: str,
) -> tuple[list[AddDraftEntry], list[str]]:
    """Parse the historical Team / TAG / @Captain form for one or many teams."""
    raw = arguments.strip()
    lines = raw.splitlines() if raw else []
    lines = [line.strip() for line in lines if line.strip()]
    valid_entries: list[AddDraftEntry] = []
    error_entries: list[str] = []
    requested_slots: set[int] = set()

    if not lines:
        return [], ["empty input - provide Team / TAG / @Captain"]

    for line in lines:
        parts = [part.strip() for part in line.split("/")]
        if len(parts) != 3:
            if len(lines) == 1:
                try:
                    team_name, tag, member_text = parse_add_arguments(line)
                except ValueError:
                    error_entries.append(f"{line} - invalid format")
                    continue
                slot_number = None
            else:
                error_entries.append(f"{line} - invalid format; use Team / TAG / @Captain")
                continue
        else:
            team_name, tag, member_text = parts
            if not team_name or not tag or not member_text:
                error_entries.append(f"{line} - invalid format")
                continue
            slot_number = None

        if not team_name or not member_text:
            error_entries.append(f"{line} - team name and captain are required")
            continue

        try:
            member = await resolve_add_member(ctx, member_text)
        except (commands.MemberNotFound, discord.NotFound, discord.Forbidden, discord.HTTPException):
            error_entries.append(f"{line} - captain not found")
            continue

        if slot_number is None:
            async with scrim.state_lock:
                slot_number = next(
                    (
                        slot.number
                        for slot in scrim.slots.values()
                        if slot_is_assignable(slot)
                        and slot.number not in requested_slots
                    ),
                    None,
                )
            if slot_number is None:
                error_entries.append(f"{line} - no available slot")
                continue

        requested_slots.add(slot_number)
        valid_entries.append(
            AddDraftEntry(
                original_line=line,
                team_name=team_name,
                slot_number=slot_number,
                member=member,
                tag=tag,
            )
        )
    return valid_entries, error_entries


async def apply_add_entries(
    scrim: Scrim,
    entries: list[AddDraftEntry],
) -> tuple[list[SlotSnapshot], list[str], list[str], bool]:
    """Persist validated entries atomically, then apply Discord side effects."""
    unavailable: list[str] = []
    snapshots: list[SlotSnapshot] = []
    async with scrim.state_lock:
        if not is_active(scrim):
            unavailable.append("The scrim is no longer active.")
        else:
            for entry in entries:
                slot = scrim.slots.get(entry.slot_number)
                if slot is None or not slot_is_assignable(slot):
                    unavailable.append(
                        f"Slot {entry.slot_number:02d} became occupied before confirmation."
                    )
            if not unavailable:
                with repository.transaction():
                    for entry in entries:
                        slot = scrim.slots[entry.slot_number]
                        slot.assignment_id += 1
                        slot.status = STATUS_RESERVED
                        slot.team_name = entry.team_name
                        slot.tag = entry.tag
                        slot.manager_id = entry.member.id
                        slot.captain_1_id = entry.member.id
                        slot.captain_2_id = None
                        snapshots.append(slot.snapshot())

    if unavailable:
        return snapshots, unavailable, [], False

    access_failures = []
    for entry in entries:
        if not await grant_manager_access(scrim, entry.member):
            access_failures.append(f"Slot {entry.slot_number:02d}")
        await send_scrim_log(
            scrim,
            "TEAM ADDED",
            f"Slot {entry.slot_number:02d} · team **{entry.team_name}** · "
            f"captain <@{entry.member.id}>",
        )
    board_refreshed = await refresh_public_slots(scrim)
    return snapshots, unavailable, access_failures, board_refreshed


def parse_registration_arguments(arguments: str) -> tuple[str, str, str | None]:
    """Parse ``!register Team Name / Tag [/ @Manager]``."""
    raw = arguments.strip()
    if "\n" in raw or "\r" in raw:
        raise ValueError(
            "`!register` accepts one team per command. Use `!add` for bulk "
            "staff registrations."
        )
    parts = [part.strip() for part in raw.split("/")]
    if len(parts) not in {2, 3} or not all(parts):
        raise ValueError(
            "Wrong format. Use `!register Team Name / Tag` or "
            "`!register Team Name / Tag / @Manager`. "
            "The manager mention is optional; without it, you become captain."
        )
    team_name, tag = parts[:2]
    manager_text = parts[2] if len(parts) == 3 else None
    if len(team_name) > 100 or len(tag) > 32:
        raise ValueError("The team name or tag is too long.")
    if any(character in team_name or character in tag for character in ("\r", "\n")):
        raise ValueError("The team name and tag must stay on one line.")
    if manager_text is not None and not re.fullmatch(
        r"<@!?\d+>", manager_text
    ):
        raise ValueError(
            "The manager must be a member mention, such as `@Manager`."
        )
    return team_name, tag, manager_text


async def apply_registration_entry(
    scrim: Scrim,
    entry: AddDraftEntry,
    *,
    registration_message_id: int | None = None,
) -> tuple[SlotSnapshot | None, str | None, bool, bool]:
    """Create a Reserved slot or a durable unassigned registration request."""
    snapshot: SlotSnapshot | None = None
    auto_accept = getattr(scrim, "registration_auto_accept", False) is True
    async with scrim.state_lock:
        if not is_active(scrim):
            return None, "The scrim is no longer active.", False, False
        slot = scrim.slots.get(entry.slot_number)
        if slot is None or not registration_slot_is_available(scrim, slot):
            return None, "The selected slot is no longer available.", False, False
        if auto_accept:
            with repository.transaction():
                slot.assignment_id += 1
                slot.status = STATUS_RESERVED
                slot.team_name = entry.team_name
                slot.tag = entry.tag
                slot.manager_id = entry.member.id
                slot.captain_1_id = entry.member.id
                slot.captain_2_id = None
                snapshot = slot.snapshot()
        else:
            request = RegistrationRequest(
                request_id=secrets.token_hex(8),
                slot_number=entry.slot_number,
                team_name=entry.team_name,
                tag=entry.tag,
                manager_id=entry.member.id,
                assignment_id=slot.assignment_id,
                registration_message_id=registration_message_id,
            )
            with repository.transaction():
                pending_registrations = getattr(
                    scrim, "pending_registrations", None
                )
                if pending_registrations is None:
                    pending_registrations = {}
                    scrim.pending_registrations = pending_registrations
                pending_registrations[request.request_id] = request
            snapshot = SlotSnapshot(
                number=request.slot_number,
                status=STATUS_PENDING,
                team_name=request.team_name,
                tag=request.tag,
                manager_id=request.manager_id,
                assignment_id=request.assignment_id,
                captain_1_id=request.manager_id,
            )

    if auto_accept:
        access_ok = await grant_manager_access(scrim, entry.member)
        board_refreshed = await refresh_public_slots(scrim)
        await send_scrim_log(
            scrim,
            "TEAM REGISTERED",
            f"Slot {entry.slot_number:02d} · team **{entry.team_name}** · "
            f"captain <@{entry.member.id}>",
        )
        return snapshot, None, access_ok, board_refreshed

    notified = await notify_staff_for_registration(scrim, request)
    await send_scrim_log(
        scrim,
        "TEAM REGISTRATION REQUEST",
        f"Slot {entry.slot_number:02d} · team **{entry.team_name}** · "
        f"captain <@{entry.member.id}>",
    )
    return snapshot, None, True, notified


class AddRegistrationView(DurableView):
    def __init__(
        self,
        ctx: commands.Context,
        scrim: Scrim,
        valid_entries: list[AddDraftEntry],
        error_entries: list[str],
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.scrim = scrim
        self.owner_id = ctx.author.id
        self.valid_entries = valid_entries
        self.error_entries = error_entries
        self.message: discord.Message | None = None
        self.completed = False
        if not valid_entries:
            self.confirm_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This registration preview belongs to another staff member.",
                ephemeral=True,
            )
            return False
        return True

    def final_embed(
        self,
        title: str,
        *,
        color: discord.Color,
        extra_lines: list[str] | None = None,
    ) -> discord.Embed:
        lines = list(extra_lines or [])
        if not lines:
            lines = [
                f"✅ Slot {entry.slot_number:02d}: "
                f"{discord.utils.escape_mentions(entry.team_name)} "
                f"(Cap: <@{entry.member.id}>)"
                for entry in self.valid_entries
            ]
        embed = discord.Embed(
            title=title,
            description="\n".join(lines)[:4096],
            color=color,
        )
        embed.set_footer(text=f"Scrim: {self.scrim.name}")
        return embed

    async def on_timeout(self) -> None:
        disable_view_items(self)
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=self.final_embed(
                        "⏱️ Registration Preview Expired",
                        color=discord.Color.orange(),
                        extra_lines=["No changes were made."],
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def _finish_cancel(self, interaction: discord.Interaction) -> None:
        self.completed = True
        disable_view_items(self)
        await interaction.response.edit_message(
            embed=self.final_embed(
                "❌ Registration Cancelled",
                color=discord.Color.red(),
                extra_lines=["No changes were made."],
            ),
            view=self,
        )
        self.stop()

    async def _finish_confirm(self, interaction: discord.Interaction) -> None:
        if self.completed:
            await interaction.response.send_message(
                "This registration preview has already been processed.",
                ephemeral=True,
            )
            return

        snapshots, unavailable, access_failures, board_refreshed = (
            await apply_add_entries(self.scrim, self.valid_entries)
        )
        if unavailable:
            self.completed = True
            disable_view_items(self)
            await interaction.response.edit_message(
                embed=self.final_embed(
                    "⚠️ Registration Not Completed",
                    color=discord.Color.orange(),
                    extra_lines=["No changes were made.", *unavailable],
                ),
                view=self,
            )
            self.stop()
            return

        self.completed = True
        disable_view_items(self)
        result_lines = [
            f"✅ Slot {snapshot.number:02d}: "
            f"{discord.utils.escape_mentions(snapshot.team_name)} "
            f"(Cap: <@{snapshot.captain_1_id}>)"
            for snapshot in snapshots
        ]
        if access_failures:
            result_lines.append(
                f"⚠️ Pending Captain access could not be completed for: "
            f"{', '.join(access_failures)}."
            )
        if not board_refreshed:
            result_lines.append("⚠️ The slot board could not be refreshed.")
        result_lines.extend(
            f"⚠️ Skipped: {error}" for error in self.error_entries
        )
        await interaction.response.edit_message(
            embed=self.final_embed(
                "✅ Registration Completed Successfully",
                color=discord.Color.green(),
                extra_lines=result_lines,
            ),
            view=self,
        )
        self.stop()

    @discord.ui.button(
        label="Confirm",
        style=discord.ButtonStyle.success,
        emoji="✅",
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._finish_confirm(interaction)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        emoji="❌",
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._finish_cancel(interaction)


@bot.command(name="register", aliases=["reg"])
@commands.guild_only()
async def register_team(ctx: commands.Context, *, arguments: str) -> None:
    """Register the invoking member's team through the configured public channel."""
    scrim = resolve_registration_scrim(ctx)
    if scrim is None:
        await send_private_command_feedback(
            ctx,
            "This command must be used in a configured team registration channel.",
            silent=False,
        )
        return
    if not getattr(scrim, "registration_open", True):
        await send_private_command_feedback(
            ctx,
            "Registrations are currently closed. Please wait for staff to reopen "
            "the registration channel.",
            silent=False,
        )
        return
    if not registration_role_allows(ctx, scrim):
        await send_private_command_feedback(
            ctx,
            "You do not have the role allowed to use `!register`.",
            silent=False,
        )
        return
    try:
        team_name, tag, manager_text = parse_registration_arguments(arguments)
    except ValueError as error:
        await send_private_registration_feedback(ctx, str(error))
        return
    manager = ctx.author
    if manager_text is not None:
        try:
            manager = await resolve_add_member(ctx, manager_text)
        except (
            commands.MemberNotFound,
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            await send_private_registration_feedback(
                ctx,
                "That manager mention could not be found in this server. "
                "Mention a current server member.",
            )
            return

    async with scrim.state_lock:
        slot_number = next(
            (
                slot.number
                for slot in scrim.slots.values()
                        if registration_slot_is_available(scrim, slot)
            ),
            None,
        )
    if slot_number is None:
        await send_private_command_feedback(
            ctx,
            "There are no available slots for this scrim.",
            silent=False,
        )
        return

    entry = AddDraftEntry(
        original_line=arguments.strip(),
        team_name=team_name,
        slot_number=slot_number,
        member=manager,
        tag=tag,
    )
    snapshot, error, access_ok, board_refreshed = await apply_registration_entry(
        scrim,
        entry,
        registration_message_id=getattr(getattr(ctx, "message", None), "id", None),
    )
    if error or snapshot is None:
        await send_private_command_feedback(
            ctx,
            error or "The registration could not be completed.",
            silent=False,
        )
        return

    if not access_ok or not board_refreshed:
        logger.warning(
            "Registration succeeded with follow-up warnings for scrim %s: "
            "access_ok=%s board_refreshed=%s",
            scrim.id,
            access_ok,
            board_refreshed,
        )
    message = getattr(ctx, "message", None)
    add_reaction = getattr(message, "add_reaction", None)
    if add_reaction is not None:
        try:
            await add_reaction(
                "✅"
                if getattr(scrim, "registration_auto_accept", False) is True
                else "🆗"
            )
        except discord.HTTPException:
            logger.exception("Could not acknowledge successful registration.")


@bot.command(name="add", aliases=["a"])
@commands.guild_only()
async def add_team(ctx: commands.Context, *, arguments: str) -> None:
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return
    valid_entries, error_entries = await build_add_draft(ctx, scrim, arguments)
    if len(valid_entries) < 2:
        if not valid_entries:
            details = "\n".join(f"⚠️ {error}" for error in error_entries)
            await send_private_command_feedback(
                ctx,
                details or "No valid registration was provided.",
                silent=False,
            )
            return
        snapshots, unavailable, access_failures, board_refreshed = (
            await apply_add_entries(scrim, valid_entries)
        )
        if unavailable:
            await send_private_command_feedback(
                ctx,
                "No changes were made.\n" + "\n".join(f"⚠️ {error}" for error in unavailable),
                silent=False,
            )
            return
        messages = [
            f"✅ Slot {snapshot.number:02d}: {snapshot.team_name} was added."
            for snapshot in snapshots
        ]
        messages.extend(f"⚠️ Skipped: {error}" for error in error_entries)
        if access_failures:
            messages.append(
                "⚠️ Pending Captain access could not be completed for: "
                + ", ".join(access_failures)
                + "."
            )
        if not board_refreshed:
            messages.append("⚠️ The slot board could not be refreshed.")
        if len(messages) > 1:
            await send_private_command_feedback(
                ctx,
                "\n".join(messages),
                silent=False,
            )
        else:
            await delete_command_message(ctx)
        return

    view = AddRegistrationView(ctx, scrim, valid_entries, error_entries)
    embed = add_preview_embed(scrim, valid_entries, error_entries)
    try:
        view.message = await ctx.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception("Could not send the registration preview.")
        return
    await delete_command_message(ctx)


def cap_feedback_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions(users=True, replied_user=False)


async def refresh_cap_board(scrim: Scrim) -> bool:
    try:
        return await publish_scrim(scrim)
    except Exception as error:
        logger.exception("Could not refresh the board after a captain change.", exc_info=error)
        return False


class CaptainSlotSelectView(discord.ui.View):
    def __init__(
        self,
        ctx: commands.Context,
        action: str,
        target_user: discord.Member,
        assignments: list[tuple[Scrim, Slot, int]],
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.owner_id = ctx.author.id
        self.guild_id = ctx.guild.id
        self.action = action
        self.target_user = target_user
        self.message: discord.Message | None = None
        self.assignments = {
            captain_slot_id(scrim, slot): (scrim, slot, position, slot.assignment_id)
            for scrim, slot, position in assignments
        }
        options = [
            discord.SelectOption(
                label=f"Slot {slot.number} - {slot.team_name}"[:100],
                value=slot_id,
            )
            for slot_id, (scrim, slot, _position, _generation) in self.assignments.items()
        ]
        select = discord.ui.Select(
            placeholder="Select a slot to apply this command...",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This slot selector belongs to another user.",
                    ephemeral=True,
                )
                return
            if interaction.guild is None or interaction.guild.id != self.guild_id:
                await interaction.response.send_message(
                    "This selector is not valid in this server.",
                    ephemeral=True,
                )
                return

            selected_id = select.values[0]
            captured = self.assignments.get(selected_id)
            if captured is None:
                await interaction.response.edit_message(
                    content="This slot selection is no longer available.",
                    view=None,
                )
                self.stop()
                return

            captured_scrim, captured_slot, _position, captured_generation = captured
            current = next(
                (
                    assignment
                    for assignment in captain_assignments(
                        self.guild_id, self.owner_id
                    )
                    if captain_slot_id(assignment[0], assignment[1]) == selected_id
                ),
                None,
            )
            if (
                current is None
                or current[0] is not captured_scrim
                or current[1] is not captured_slot
                or current[1].assignment_id != captured_generation
            ):
                await interaction.response.edit_message(
                    content="This slot assignment changed before the command was applied.",
                    view=None,
                )
                self.stop()
                return

            if not cap_channel_is_allowed(
                self.guild_id, interaction.channel_id, captured_scrim
            ):
                await interaction.response.send_message(
                    CAP_CHANNEL_RESTRICTION_MESSAGE,
                    ephemeral=True,
                )
                return
            if not cap_role_is_allowed(
                interaction.user, self.guild_id, captured_scrim
            ):
                await interaction.response.send_message(
                    CAP_ROLE_RESTRICTION_MESSAGE,
                    ephemeral=True,
                )
                return

            success, message = await execute_cap_action(
                self.action,
                self.ctx,
                self.target_user,
                current,
            )
            if success:
                content = (
                    f"✅ Command successfully applied to Slot "
                    f"{captured_slot.number} ({captured_slot.team_name})."
                )
            else:
                content = message or "The command could not be applied to this slot."
            await interaction.response.edit_message(content=content, view=None)
            self.stop()

        select.callback = callback
        self.add_item(select)

    async def on_timeout(self) -> None:
        disable_view_items(self)
        if self.message is not None:
            try:
                await self.message.edit(
                    content="This slot selector has expired.",
                    view=self,
                )
            except discord.HTTPException:
                pass


async def send_captain_slot_selector(
    ctx: commands.Context,
    action: str,
    target_user: discord.Member,
    assignments: list[tuple[Scrim, Slot, int]],
) -> None:
    view = CaptainSlotSelectView(ctx, action, target_user, assignments)
    send_kwargs = {
        "view": view,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    interaction = getattr(ctx, "interaction", None)
    if interaction is not None and not interaction.is_expired():
        send_kwargs["ephemeral"] = True
    else:
        send_kwargs["delete_after"] = 120
    try:
        view.message = await ctx.send(
            "Select a slot to apply this command...",
            **send_kwargs,
        )
    except discord.HTTPException:
        logger.exception("Could not send the captain slot selector.")
        return
    await delete_command_message(ctx)


async def captain_assignment_or_selection(
    ctx: commands.Context,
    action: str,
    target_user: discord.Member,
) -> tuple[Scrim, Slot, int] | None:
    assignments = captain_assignments(ctx.guild.id, ctx.author.id)
    if not assignments:
        await send_private_command_feedback(
            ctx,
            "❌ You are not a captain of any registered slot.",
            silent=False,
        )
        return None
    if len(assignments) == 1:
        return assignments[0]
    if len(assignments) > 25:
        await send_private_command_feedback(
            ctx,
            "You are registered as captain for too many slots to display in one selector.",
            silent=False,
        )
        return None
    await send_captain_slot_selector(ctx, action, target_user, assignments)
    return None


async def perform_cap_add(
    ctx: commands.Context,
    member: discord.Member,
    assignment: tuple[Scrim, Slot, int],
) -> tuple[bool, str | None]:
    scrim, slot, _position = assignment
    if slot.captain_2_id is not None:
        return (
            False,
            "❌ **Limit Reached.** Your team already has 2 captains. "
            "Use `!cap remove @User` first, or `!cap transfer @User` to swap completely.",
        )
    if member.id in {slot.captain_1_id, slot.captain_2_id}:
        return False, "That user is already a captain for your team."
    if captain_assignments(ctx.guild.id, member.id):
        return False, "That user is already registered as a captain for another team."

    async with scrim.state_lock:
        if (
            not is_active(scrim)
            or scrim.slots.get(slot.number) is not slot
            or (
                slot.captain_1_id != ctx.author.id
                and slot.captain_2_id != ctx.author.id
            )
            or slot.captain_2_id is not None
        ):
            return False, "This slot assignment changed before the command was applied."
        if not await grant_manager_access(scrim, member):
            return (
                False,
                "The co-captain could not be granted the Captain role and team access.",
            )
        with repository.transaction():
            slot.captain_2_id = member.id

    board_refreshed = await refresh_cap_board(scrim)
    message = (
        f"✅ **Co-Captain Added.** <@{member.id}> is now a co-captain for your team!"
    )
    if not board_refreshed:
        message += " The slot board could not be refreshed."
    await send_scrim_log(
        scrim,
        "CAPTAIN ADDED",
        f"Slot {slot.number:02d} · team **{slot.team_name}** · "
        f"actor <@{ctx.author.id}> · co-captain <@{member.id}>.",
    )
    return True, message


async def perform_cap_transfer(
    ctx: commands.Context,
    member: discord.Member,
    assignment: tuple[Scrim, Slot, int],
) -> tuple[bool, str | None]:
    scrim, slot, position = assignment
    if member.id == ctx.author.id:
        return False, "You cannot transfer the captain role to yourself."
    if member.id in {slot.captain_1_id, slot.captain_2_id}:
        return False, "That user is already a captain for your team."

    async with scrim.state_lock:
        if (
            not is_active(scrim)
            or scrim.slots.get(slot.number) is not slot
            or (
                slot.captain_1_id != ctx.author.id
                and slot.captain_2_id != ctx.author.id
            )
            or member.id in {slot.captain_1_id, slot.captain_2_id}
        ):
            return False, "This slot assignment changed before the command was applied."
        if not await grant_manager_access(scrim, member):
            return (
                False,
                "The leadership transfer could not grant the Captain role and team access.",
            )
        with repository.transaction():
            if position == 1:
                slot.captain_1_id = member.id
                slot.manager_id = member.id
            else:
                slot.captain_2_id = member.id
    await revoke_manager_access_if_unused(scrim, ctx.author.id, member=ctx.author)
    board_refreshed = await refresh_cap_board(scrim)
    message = f"✅ **Leadership Transferred** to <@{member.id}>."
    if not board_refreshed:
        message += " The slot board could not be refreshed."
    transferred_role = "primary captain" if position == 1 else "co-captain"
    await send_scrim_log(
        scrim,
        "CAPTAIN TRANSFERRED",
        f"Slot {slot.number:02d} · team **{slot.team_name}** · "
        f"actor <@{ctx.author.id}> · {transferred_role} transferred to <@{member.id}>.",
    )
    return True, message


async def perform_cap_remove(
    ctx: commands.Context,
    member: discord.Member,
    assignment: tuple[Scrim, Slot, int],
) -> tuple[bool, str | None]:
    scrim, slot, position = assignment
    if member.id == ctx.author.id:
        return False, "You cannot remove yourself with this command."
    if position == 1:
        if slot.captain_2_id != member.id:
            return False, "That user is not your co-captain for this team."
    elif slot.captain_1_id != member.id:
        return (
            False,
            "As co-captain, you can only remove the primary captain to take over.",
        )

    async with scrim.state_lock:
        if (
            not is_active(scrim)
            or scrim.slots.get(slot.number) is not slot
            or (
                position == 1
                and (
                    slot.captain_1_id != ctx.author.id
                    or slot.captain_2_id != member.id
                )
            )
            or (
                position == 2
                and (
                    slot.captain_2_id != ctx.author.id
                    or slot.captain_1_id != member.id
                )
            )
        ):
            return False, "This slot assignment changed before the command was applied."
        with repository.transaction():
            if position == 1:
                slot.captain_2_id = None
            else:
                slot.captain_1_id = ctx.author.id
                slot.manager_id = ctx.author.id
                slot.captain_2_id = None
    await revoke_manager_access_if_unused(scrim, member.id, member=member)
    if position == 1:
        message = (
            f"✅ **Co-Captain Removed.** <@{member.id}> is no longer a co-captain "
            "for your team."
        )
    else:
        message = (
            f"✅ **Captain Role Taken Over.** <@{member.id}> was removed and "
            "you are now the primary captain."
        )
    board_refreshed = await refresh_cap_board(scrim)
    if not board_refreshed:
        message += " The slot board could not be refreshed."
    removed_role = "co-captain" if position == 1 else "primary captain"
    await send_scrim_log(
        scrim,
        "CAPTAIN REMOVED",
        f"Slot {slot.number:02d} · team **{slot.team_name}** · "
        f"actor <@{ctx.author.id}> · removed {removed_role} <@{member.id}>.",
    )
    return True, message


async def execute_cap_action(
    action: str,
    ctx: commands.Context,
    target_user: discord.Member,
    assignment: tuple[Scrim, Slot, int],
) -> tuple[bool, str | None]:
    if action == "add":
        return await perform_cap_add(ctx, target_user, assignment)
    if action == "transfer":
        return await perform_cap_transfer(ctx, target_user, assignment)
    if action == "remove":
        return await perform_cap_remove(ctx, target_user, assignment)
    return False, "This captain command is not supported."


async def send_cap_action_feedback(
    ctx: commands.Context,
    success: bool,
    message: str | None,
) -> None:
    if message is None:
        await send_private_command_feedback(ctx, "", silent=True)
        return
    kwargs = {"allowed_mentions": cap_feedback_mentions()} if success else {}
    await send_private_command_feedback(ctx, message, **kwargs)


@bot.group(name="cap", invoke_without_command=True, hidden=True)
@commands.guild_only()
async def cap_command(ctx: commands.Context) -> None:
    if not await require_cap_channel(ctx):
        return
    await send_private_command_feedback(
        ctx,
        "Use `!cap add @User`, `!cap transfer @User`, "
        "or `!cap remove @User`.",
        silent=False,
    )


@cap_command.command(name="add")
@commands.guild_only()
async def cap_add(ctx: commands.Context, member: discord.Member) -> None:
    if not await require_cap_channel(ctx):
        return
    assignment = await captain_assignment_or_selection(ctx, "add", member)
    if assignment is None:
        return
    if not await require_cap_channel(ctx, assignment[0]):
        return
    success, message = await execute_cap_action("add", ctx, member, assignment)
    await send_cap_action_feedback(ctx, success, message)


@cap_command.command(name="transfer")
@commands.guild_only()
async def cap_transfer(ctx: commands.Context, member: discord.Member) -> None:
    if not await require_cap_channel(ctx):
        return
    assignment = await captain_assignment_or_selection(ctx, "transfer", member)
    if assignment is None:
        return
    if not await require_cap_channel(ctx, assignment[0]):
        return
    success, message = await execute_cap_action("transfer", ctx, member, assignment)
    await send_cap_action_feedback(ctx, success, message)


@cap_command.command(name="remove")
@commands.guild_only()
async def cap_remove(ctx: commands.Context, member: discord.Member) -> None:
    if not await require_cap_channel(ctx):
        return
    assignment = await captain_assignment_or_selection(ctx, "remove", member)
    if assignment is None:
        return
    if not await require_cap_channel(ctx, assignment[0]):
        return
    success, message = await execute_cap_action("remove", ctx, member, assignment)
    await send_cap_action_feedback(ctx, success, message)


def parse_slot_numbers(arguments: str) -> list[int]:
    """Parse unique space-separated slot numbers while preserving their order."""
    tokens = arguments.split()
    if not tokens or any(not token.isdigit() for token in tokens):
        raise ValueError("Slot numbers must be separated by spaces.")
    numbers: list[int] = []
    for token in tokens:
        number = int(token)
        if not 1 <= number <= 99:
            raise ValueError("Slot numbers must be between 01 and 99.")
        if number not in numbers:
            numbers.append(number)
    return numbers


def parse_add_arguments(arguments: str) -> tuple[str, str, str]:
    """Parse both the historical slash format and the newer whitespace format."""
    raw = arguments.strip()
    if "/" in raw:
        parts = [part.strip() for part in raw.split("/", 2)]
        if len(parts) != 3 or not all(parts):
            raise ValueError("Expected Team / Tag / Manager.")
        return parts[0], parts[1], parts[2]

    parts = raw.split()
    if len(parts) < 3:
        raise ValueError("Expected Team name TAG @Manager.")
    team_name, tag, manager_text = (
        " ".join(parts[:-2]).strip(),
        parts[-2].strip(),
        parts[-1].strip(),
    )
    if not team_name or not tag or not manager_text:
        raise ValueError("Expected Team name TAG @Manager.")
    return team_name, tag, manager_text


@bot.command(name="remind", aliases=["rem"])
@commands.guild_only()
async def remind_managers(ctx: commands.Context) -> None:
    """Mention managers whose assigned slots still need their confirmation."""
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return

    async with scrim.state_lock:
        reserved = [
            slot.snapshot()
            for slot in scrim.slots.values()
            if slot.status == STATUS_RESERVED and slot.manager_id is not None
        ]

    if not reserved:
        embed = discord.Embed(
            title="✅ No reminder needed",
            description="There are no Reserved slots waiting for captain confirmation.",
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Scrim: {scrim.name}")
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
            delete_after=30,
        )
        await delete_command_message(ctx)
        return

    if scrim.pending_role_id is None:
        await send_private_command_feedback(
            ctx,
            "This scrim has no Pending Captain Role configured.",
            silent=False,
        )
        return
    try:
        embed = discord.Embed(
            title="🔔 Slot Confirmation Reminder",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Slots waiting",
            value=", ".join(f"`{slot.number:02d}`" for slot in reserved),
            inline=False,
        )
        embed.set_footer(text=f"Scrim: {scrim.name}")
        await ctx.send(
            content=(
                "***Reserved teams: please Confirm or Cancel your Slot.***\n"
                f"<@&{scrim.pending_role_id}>"
            ),
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.HTTPException:
        logger.exception("Could not send manager reminders (%s).", scrim.id)
    await delete_command_message(ctx)


@bot.command(name="confirm", aliases=["cf"])
@commands.guild_only()
async def confirm_slot(ctx: commands.Context, *, slot_numbers: str) -> None:
    scrim = await require_staff_scrim(
        ctx,
        allow_public=True,
        silent=False,
    )
    if scrim is None:
        return
    try:
        requested_numbers = parse_slot_numbers(slot_numbers)
    except ValueError:
        await send_private_command_feedback(
            ctx,
            "Invalid slot number. Use `!confirm <slot>` with a valid slot number.",
        )
        return
    confirmed: list[SlotSnapshot] = []
    async with scrim.state_lock:
        if not is_active(scrim):
            await send_private_command_feedback(
                ctx,
                "This scrim no longer exists.",
            )
            return
        slots = [scrim.slots.get(number) for number in requested_numbers]
        if any(
            slot is None
            or slot.status not in {STATUS_RESERVED, STATUS_PENDING}
            for slot in slots
        ):
            await send_private_command_feedback(
                ctx,
                "Only Reserved or Pending slots can be confirmed.",
            )
            return
        with repository.transaction():
            for slot in slots:
                slot.status = STATUS_CONFIRMED
                confirmed.append(slot.snapshot())
    manager_confirmations = [
        slot for slot in confirmed if slot.manager_id is not None
    ]
    role_updates = await asyncio.gather(
        *(
            confirm_captain_role(scrim, slot.manager_id)
            for slot in manager_confirmations
        )
    )
    await refresh_public_slots(scrim)
    slot_details = ", ".join(
        f"{slot.number:02d} · team **{slot.team_name}**" for slot in confirmed
    )
    result_lines = [f"✅ Confirmed slot(s): {slot_details}."]
    failed_role_updates = [
        slot.number
        for slot, role_updated in zip(manager_confirmations, role_updates)
        if not role_updated
    ]
    if failed_role_updates:
        result_lines.append(
            "⚠️ Captain roles could not be synchronized for slot(s): "
            + ", ".join(f"{number:02d}" for number in failed_role_updates)
            + "."
        )
    await send_private_command_feedback(
        ctx,
        "\n".join(result_lines),
        delete_after=3,
    )
    await send_scrim_log(
        scrim,
        "SLOTS FORCE CONFIRMED",
        slot_details,
    )


@bot.command(name="remove", aliases=["rm"])
@commands.guild_only()
async def remove_team(ctx: commands.Context, *, slot_numbers: str) -> None:
    scrim = await require_staff_scrim(ctx)
    if scrim is None:
        return
    try:
        requested_numbers = parse_slot_numbers(slot_numbers)
    except ValueError:
        await send_private_command_feedback(ctx, "", silent=True)
        return
    removed: list[SlotSnapshot] = []
    async with scrim.state_lock:
        if not is_active(scrim):
            await send_private_command_feedback(ctx, "", silent=True)
            return
        slots = [scrim.slots.get(number) for number in requested_numbers]
        if any(slot is None or slot_is_assignable(slot) for slot in slots):
            await send_private_command_feedback(ctx, "", silent=True)
            return
        with repository.transaction():
            for slot in slots:
                removed.append(slot.snapshot())
                slot.clear()
    for captain_id in {
        captain_id
        for slot in removed
        for captain_id in (slot.captain_1_id, slot.captain_2_id)
        if captain_id is not None
    }:
        await revoke_manager_access_if_unused(scrim, captain_id)
    await refresh_public_slots(scrim)
    slot_details = ", ".join(
        f"{slot.number:02d} · team **{slot.team_name}**" for slot in removed
    )
    await send_scrim_log(
        scrim,
        "SLOTS FORCE REMOVED",
        slot_details,
    )
    await delete_command_message(ctx)


class StaffActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"slots:staff:(?P<scrim>[a-f0-9]{16}):(?P<number>[0-9]{1,2}):(?P<assignment>[0-9]+):(?P<action>confirm|release)(?P<registration>:reg)?",
):
    def __init__(
        self,
        scrim_id: str,
        number: int,
        assignment_id: int,
        confirm: bool,
        *,
        registration_request: bool = False,
    ):
        self.scrim_id = scrim_id
        self.number, self.assignment_id, self.confirm = number, assignment_id, confirm
        self.registration_request = registration_request
        suffix = ":reg" if registration_request else ""
        super().__init__(discord.ui.Button(
            label="Confirm" if confirm else "Remove",
            style=discord.ButtonStyle.success if confirm else discord.ButtonStyle.danger,
            emoji="✅" if confirm else "❌",
            custom_id=f"slots:staff:{scrim_id}:{number}:{assignment_id}:"
                      f"{'confirm' if confirm else 'release'}{suffix}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(
            match["scrim"], int(match["number"]), int(match["assignment"]),
            match["action"] == "confirm",
            registration_request=match["registration"] == ":reg",
        )

    async def callback(self, interaction):
        scrim = repository.get(self.scrim_id)
        if scrim is None or self.number not in scrim.slots:
            await interaction.response.send_message("Unknown scrim or slot.", ephemeral=True)
            return
        if self.registration_request:
            request = next(
                (
                    request
                    for request in getattr(scrim, "pending_registrations", {}).values()
                    if request.slot_number == self.number
                    and request.assignment_id == self.assignment_id
                ),
                None,
            )
            if request is None:
                await interaction.response.send_message(
                    "This registration request is no longer active.",
                    ephemeral=True,
                )
                return
            snapshot = SlotSnapshot(
                number=request.slot_number,
                status=STATUS_PENDING,
                team_name=request.team_name,
                tag=request.tag,
                manager_id=request.manager_id,
                assignment_id=request.assignment_id,
                captain_1_id=request.manager_id,
            )
            view = SlotReviewView(
                scrim,
                snapshot,
                registration_request=True,
                registration_request_id=request.request_id,
            )
        else:
            snapshot = scrim.slots[self.number].snapshot()
            view = SlotReviewView(scrim, snapshot)
            view.slot = SlotSnapshot(
                number=snapshot.number,
                status=snapshot.status,
                team_name=snapshot.team_name,
                tag=snapshot.tag,
                manager_id=snapshot.manager_id,
                assignment_id=self.assignment_id,
            )
        try:
            if await view.interaction_check(interaction):
                await view.finish_review(interaction, self.confirm)
        except Exception as error:
            await view.on_error(interaction, error, self)


install_setup_panel()


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("The DISCORD_TOKEN environment variable is missing.")
    bot.run(token)


if __name__ == "__main__":
    main()