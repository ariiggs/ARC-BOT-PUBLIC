"""Gestionnaire multi-scrim de slots PUBG Mobile."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

from slot_storage import SlotStateStore, SlotStorageError
from scrim_state import (
    STATUS_AVAILABLE,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_RESERVED,
    MAX_MATCHES,
    Scrim,
    ScrimRepository,
    Slot,
    SlotSnapshot,
    timezone_for_name,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pung-scrim-bot")

FIGURE_SPACE = "\u2007"
SUPPORT_SERVER_URL = "https://discord.gg/S8uaGEJGv8"

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
    allowed_channel_ids = (
        (scrim.public_channel_id, scrim.staff_channel_id)
        if scrim is not None and allow_public
        else (scrim.public_channel_id,)
        if scrim is not None and public_only
        else (scrim.staff_channel_id,)
        if scrim is not None
        else ()
    )
    if scrim is None or ctx.channel.id not in allowed_channel_ids:
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


async def purge_channel_messages(channel) -> bool:
    """Clear a channel in batches, reporting failure without aborting reset."""
    purge = getattr(channel, "purge", None)
    if purge is None:
        return False
    try:
        while True:
            deleted = await purge(limit=100)
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
        config = repository.get_idpw_config(current_id)
        scrim = repository.get(current_id)
        if message is None and config is not None and scrim is not None:
            try:
                channel = await configured_text_channel(
                    scrim, config.target_channel_id
                )
                fetch_message = getattr(channel, "fetch_message", None)
                if fetch_message is not None and config.announcement_message_id:
                    message = await fetch_message(config.announcement_message_id)
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Could not resolve the MATCH ACCESS message for scrim %s.",
                    current_id,
                )

        if message is not None:
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
    matches = [
        scrim for scrim in repository.list(ctx.guild.id)
        if ctx.channel.id in (
            (scrim.staff_channel_id,) if staff_only
            else (scrim.public_channel_id, scrim.staff_channel_id)
        )
    ]
    return matches[0] if len(matches) == 1 else None


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
            f"🏆 **Résumé du Scrim — {discord.utils.escape_markdown(scrim.name)}**\n"
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
    "❌ **Command restricted.** Please use the designated captain management "
    "channel for this command."
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


async def require_cap_channel(
    ctx: commands.Context, scrim: Scrim | None = None
) -> bool:
    channel_id = getattr(ctx.channel, "id", None)
    allowed = cap_channel_is_allowed(ctx.guild.id, channel_id, scrim)
    if allowed:
        return True
    await send_private_command_feedback(
        ctx,
        CAP_CHANNEL_RESTRICTION_MESSAGE,
        silent=False,
    )
    return False


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
    def __init__(self, scrim: Scrim, slot: SlotSnapshot) -> None:
        super().__init__(timeout=None)
        self.scrim = scrim
        self.slot = slot
        self.add_item(StaffActionButton(scrim.id, slot.number, slot.assignment_id, True))
        self.add_item(StaffActionButton(scrim.id, slot.number, slot.assignment_id, False))

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
        async with self.scrim.state_lock:
            if is_active(self.scrim):
                current = self.scrim.slots[self.slot.number]
                if (
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
            text = (
                f"🟢 Team **{result.team_name}** has been confirmed."
                if confirm
                else f"Team **{result.team_name}** has been released."
            )
            if confirm and not await confirm_captain_role(
                self.scrim, result.manager_id
            ):
                text += " The confirmed captain role could not be updated."
            if not await refresh_public_slots(self.scrim):
                text += " The public board could not be updated."
            await send_scrim_log(
                self.scrim,
                "STAFF VALIDATION" if confirm else "STAFF RELEASE",
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
                scrim.current_match_counter = 1
        for manager_id in manager_ids:
            await revoke_manager_access_if_unused(scrim, manager_id)
        await send_scrim_log(scrim, "SCRIM RESET", "All slots were reset by staff.")
        if not is_active(scrim):
            return "This scrim no longer exists. The reset was stopped."

        channels_to_clear = []
        for channel_id in (scrim.staff_channel_id, scrim.public_channel_id):
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
            for channel_id in (scrim.staff_channel_id, scrim.public_channel_id)
        )
        if updated and channels_cleared:
            return "✅ All slots are now available and both scrim channels were cleared."
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
    confirmation_message = await send_private_command_feedback(
        ctx,
        (
            f"⚠️ Reset **{discord.utils.escape_markdown(scrim.name)}**? "
            "This clears every slot and both configured scrim channels. "
            "Click **Confirm Reset** to continue."
        ),
        view=confirmation,
        delete_after=60,
    )
    confirmation.message = confirmation_message


@bot.command(name="open")
@commands.guild_only()
async def open_scrim(ctx: commands.Context) -> None:
    """Open manager interactions for the scrim configured in its public channel."""
    scrim = await require_staff_scrim(ctx, public_only=True)
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


@bot.command(name="close")
@commands.guild_only()
async def close_scrim(ctx: commands.Context) -> None:
    """Close manager interactions for the scrim configured in its public channel."""
    scrim = await require_staff_scrim(ctx, public_only=True)
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
    message_content = (
        f"# Match {match_number}\n\n"
        f"ID : `{discord.utils.escape_markdown(lobby_id)}`\n"
        f"PW : {discord.utils.escape_markdown(password)}\n"
        f"Start Time : {heure_formatee}\n\n"
        f"<@&{scrim.confirmed_role_id}>"
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
        active_idpw[scrim.id] = {"message": message, "tasks": []}
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
                await target_channel.send(
                    f"<@&{scrim.confirmed_role_id}> "
                    "The match starts in 3 minutes. Please prepare.",
                    allowed_mentions=role_mentions,
                )
                await asyncio.sleep(2 * 60)
            elif minutes > 1:
                await asyncio.sleep((minutes - 1) * 60)
            if minutes >= 1:
                await target_channel.send(
                    f"<@&{scrim.confirmed_role_id}> "
                    "Final call. The match begins in 1 minute.",
                    allowed_mentions=role_mentions,
                )
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
    lines = [f"**Match {game_number}**"]
    map_name = _match_map_for_scrim(scrim, game_number)
    if map_name is not None:
        lines.append(f"Map : {discord.utils.escape_markdown(map_name)}")
    lines.extend(
        (
            f"**ID :** `{discord.utils.escape_markdown(room_id)}`",
            f"**PW :** {discord.utils.escape_markdown(password)}",
            f"**Start :** {local_start.strftime('%H:%M')}",
        )
    )
    if scrim.confirmed_role_id is not None:
        lines.append(f"<@&{scrim.confirmed_role_id}>")
    message_content = "\n".join(lines)
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
        active_idpw[scrim.id] = {"message": message, "tasks": []}
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


def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="A.R.C. Bot - Command Center",
        description=(
            "Manage scrims, match access, registrations, and captain approvals "
            "from Discord."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🛠️ Staff & Setup",
        value=(
            "`!setup` — initialize scrims and configure roles, channels, maps, and ID/PW.\n"
            "`!set <@Role>` — define the global Staff role for admin commands.\n"
            "`!say <message>` — publish an announcement as the bot for Staff.\n"
            "`!export` — generate a complete registration list usable anywhere.\n"
            "`!reset` — archive the last slot state in History, then clear registrations."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎮 Match Management",
        value=(
            "`!slots [Scrim Name]` — display real-time registration status; one scrim "
            "is selected automatically and multiple scrims use a dropdown.\n"
            "`!update [Scrim Name]` — publish or refresh the selected slot board.\n"
            "`!idpw <room_id> / <minutes>` — send room details and schedule alerts.\n"
            "`!idpwg[1-25] <room_id> / <minutes>` — fixed-password match access.\n"
            "`!idpwg[1-25] <room_id> / <password> / <minutes>` — dynamic-password access."
        ),
        inline=False,
    )
    embed.add_field(
        name="👤 Players",
        value=(
            "`!add Team / TAG / @Captain` — register a team in the first free slot.\n"
            "`!remind` — remind reserved managers to confirm or cancel.\n"
            "`✅ Confirm` / `❌ Cancel` — manager actions on the public board.\n"
            "`!cap add`, `!cap transfer`, `!cap remove` — manage team captains."
        ),
        inline=False,
    )
    embed.add_field(
        name="✅ Staff Decisions",
        value=(
            "`!confirm <slot> [slot ...]` — confirm one or more pending slots.\n"
            "`!remove <slot> [slot ...]` — release one or more registered slots.\n"
            "`!open` / `!close` — enable or disable manager board actions."
        ),
        inline=False,
    )
    embed.set_footer(text="Use !help <command> for Discord's command-specific details.")
    return embed


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await send_private_command_feedback(
        ctx,
        "",
        embed=build_help_embed(),
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


@bot.group(name="auth", invoke_without_command=True, hidden=True)
@auth_admin_required()
async def auth_command(ctx: commands.Context) -> None:
    await send_private_command_feedback(
        ctx,
        "Use `!auth add <Guild_ID> <Days|unlimited>`, "
        "`!auth remove <Guild_ID>`, or `!auth list`.",
        silent=False,
    )


@auth_command.command(name="add")
@auth_admin_required()
async def auth_add(
    ctx: commands.Context, guild_id: int, duration: str
) -> None:
    normalized_duration = duration.strip().casefold()
    if normalized_duration in {"0", "unlimited", "illimité", "illimite"}:
        duration_days = 0
    else:
        try:
            duration_days = int(normalized_duration)
        except ValueError:
            await send_private_command_feedback(
                ctx,
                "Duration must be a non-negative number of days or `unlimited`.",
                silent=False,
            )
            return
    try:
        replaced = repository.is_guild_authorized(guild_id)
        repository.authorize_guild(guild_id, duration_days)
    except ValueError as error:
        await send_private_command_feedback(ctx, str(error), silent=False)
        return
    if duration_days == 0:
        duration_label = "with unlimited access"
    else:
        duration_label = f"for {duration_days} day(s)"
    message = (
        f"Guild `{guild_id}` authorization was replaced {duration_label}."
        if replaced
        else f"Guild `{guild_id}` is now authorized {duration_label}."
    )
    await send_private_command_feedback(ctx, message, silent=False)


@auth_command.command(name="remove")
@auth_admin_required()
async def auth_remove(ctx: commands.Context, guild_id: int) -> None:
    try:
        removed = repository.revoke_guild(guild_id)
    except ValueError as error:
        await send_private_command_feedback(ctx, str(error), silent=False)
        return
    message = (
        f"Guild `{guild_id}` has been revoked."
        if removed
        else f"Guild `{guild_id}` was not authorized."
    )
    await send_private_command_feedback(ctx, message, silent=False)


async def authorized_guild_name(guild_id: int) -> str | None:
    guild = bot.get_guild(guild_id)
    if guild is not None:
        return guild.name
    try:
        guild = await bot.fetch_guild(guild_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return guild.name


def build_auth_list_embed(
    authorizations: list[tuple[int, datetime | None, int]],
    names: dict[int, str | None],
) -> discord.Embed:
    """Render the bot-admin subscription list using the !sub embed style."""
    if not authorizations:
        embed = discord.Embed(
            title="💎 **A.R.C. Subscription Status**",
            color=discord.Color.red(),
        )
        embed.add_field(name="Status", value="🔴 Expired", inline=False)
        embed.add_field(name="Time Remaining", value="No active subscriptions.", inline=False)
        return embed

    has_unlimited = any(
        duration_days == 0 and expires_at is None
        for _, expires_at, duration_days in authorizations
    )
    embed = discord.Embed(
        title="💎 **A.R.C. Subscription Status**",
        color=discord.Color.gold() if has_unlimited else discord.Color.green(),
    )
    for guild_id, expires_at, duration_days in authorizations[:25]:
        name = names.get(guild_id) or "Name unavailable"
        safe_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(name)
        )
        if duration_days == 0 and expires_at is None:
            status = "🟢 Active"
            remaining = "♾️ Lifetime / Unlimited"
        elif expires_at is not None and expires_at > datetime.now(timezone.utc):
            status = "🟢 Active"
            remaining = subscription_remaining_text(expires_at)
        elif expires_at is not None:
            status = "🔴 Expired"
            remaining = f"Expired on {expires_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        else:
            status = "🔴 Expired"
            remaining = "Expired"
        embed.add_field(
            name=f"{safe_name} (`{guild_id}`)",
            value=f"**Status:** {status}\n**Time Remaining:** {remaining}",
            inline=False,
        )
    if len(authorizations) > 25:
        embed.set_footer(text=f"{len(authorizations) - 25} additional subscriptions not shown.")
    return embed


@auth_command.command(name="list")
@auth_admin_required()
async def auth_list(ctx: commands.Context) -> None:
    authorizations = repository.list_authorizations()
    names = {
        guild_id: await authorized_guild_name(guild_id)
        for guild_id, _, _ in authorizations
    }
    await send_private_command_feedback(
        ctx,
        "",
        embed=build_auth_list_embed(authorizations, names),
        view=SubscriptionStatusView(),
        delete_after=60,
    )


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
            f"{scrim.emoji_confirmed} **Confirmed:** "
            f"{counts[STATUS_CONFIRMED] + counts[STATUS_PENDING]}"
        ),
        color=discord.Color.blurple(),
    )
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


@bot.command(name="slots")
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

    current_category_id = getattr(ctx.channel, "category_id", None)
    matching_scrims = [
        scrim
        for scrim in active_scrims
        if current_category_id is not None
        and current_category_id in await scrim_category_ids(scrim)
    ]
    if matching_scrims:
        await send_slot_status_embed(ctx, matching_scrims[0])
        return

    if len(active_scrims) == 1:
        await send_slot_status_embed(ctx, active_scrims[0])
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


@bot.command(name="update", aliases=["publier_slots"])
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

    if len(active_scrims) == 1:
        scrim = active_scrims[0]
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


@bot.command(name="say")
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


def build_export_messages(scrim: Scrim) -> list[str]:
    """Build copy-friendly code blocks with captain IDs for every assigned team."""
    blocks = []
    for slot in sorted(scrim.slots.values(), key=lambda item: item.number):
        if (
            slot.status == STATUS_AVAILABLE
            or not slot.team_name
            or slot.manager_id is None
        ):
            continue
        team_name = " ".join(slot.team_name.split()).replace("```", "")
        tag = (
            " ".join(slot.tag.split()).replace("```", "")
            if slot.tag
            else f"S{slot.number:02d}"
        )
        captain_ids = [f"<@{slot.manager_id}>"]
        if slot.captain_2_id is not None:
            captain_ids.append(f"<@{slot.captain_2_id}>")
        blocks.append(f"```text\n{team_name} {tag} {' '.join(captain_ids)}\n```")

    return blocks


class ExportScrimSelectView(discord.ui.View):
    """Let staff choose which active scrim should be exported."""

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
            placeholder="📂 Select a Scrim to export...",
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
            if not member_is_staff_in_guild(interaction.user, self.guild_id):
                await interaction.response.send_message(
                    "You do not have permission to export scrims.",
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

            messages = build_export_messages(scrim)
            if not messages:
                await interaction.response.send_message(
                    "Aucune équipe enregistrée.",
                    ephemeral=True,
                )
                self.stop()
                return

            try:
                await interaction.response.send_message(
                    messages[0],
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                for message in messages[1:]:
                    await interaction.followup.send(
                        message,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except discord.HTTPException:
                logger.exception("Could not export teams for scrim %s.", scrim.id)
            finally:
                self.stop()

        select.callback = callback
        self.add_item(select)


@bot.command(name="export")
@commands.guild_only()
async def export_teams(ctx: commands.Context) -> None:
    """Show a server-wide scrim selector and export the selected scrim."""
    if ctx.guild is None:
        return
    if not member_is_staff_in_guild(ctx.author, ctx.guild.id):
        await send_private_command_feedback(
            ctx,
            "You do not have permission to export scrims.",
            silent=True,
        )
        return

    scrims = repository.list(ctx.guild.id)
    if not scrims:
        await send_private_command_feedback(
            ctx,
            "No active scrims are configured for this server.",
        )
        return

    view = ExportScrimSelectView(
        owner_id=ctx.author.id,
        guild_id=ctx.guild.id,
        scrims=scrims,
    )
    try:
        await ctx.send(
            "📂 Select a scrim to export:",
            view=view,
            delete_after=120,
        )
    except discord.HTTPException:
        logger.exception("Could not show the scrim export selector.")
    finally:
        await delete_command_message(ctx)


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


@bot.command(name="add")
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


@bot.command(name="remind")
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
                "***Please Confirm / Cancel your Slot !***\n"
                f"<@&{scrim.pending_role_id}>"
            ),
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.HTTPException:
        logger.exception("Could not send manager reminders (%s).", scrim.id)
    await delete_command_message(ctx)


@bot.command(name="confirm")
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


@bot.command(name="remove")
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
    template=r"slots:staff:(?P<scrim>[a-f0-9]{16}):(?P<number>[0-9]{1,2}):(?P<assignment>[0-9]+):(?P<action>confirm|release)",
):
    def __init__(self, scrim_id: str, number: int, assignment_id: int, confirm: bool):
        self.scrim_id = scrim_id
        self.number, self.assignment_id, self.confirm = number, assignment_id, confirm
        super().__init__(discord.ui.Button(
            label="Confirm" if confirm else "Remove",
            style=discord.ButtonStyle.success if confirm else discord.ButtonStyle.danger,
            emoji="✅" if confirm else "❌",
            custom_id=f"slots:staff:{scrim_id}:{number}:{assignment_id}:"
                      f"{'confirm' if confirm else 'release'}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(
            match["scrim"], int(match["number"]), int(match["assignment"]),
            match["action"] == "confirm",
        )

    async def callback(self, interaction):
        scrim = repository.get(self.scrim_id)
        if scrim is None or self.number not in scrim.slots:
            await interaction.response.send_message("Unknown scrim or slot.", ephemeral=True)
            return
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