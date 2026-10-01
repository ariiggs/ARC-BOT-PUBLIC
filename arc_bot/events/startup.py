"""Instance-bound startup and event handlers shared by the bot trees.

This module deliberately knows nothing about either bot's filesystem layout or
optional features.  The caller supplies its module namespace as the dependency
container, and Beta may inject its scanner installer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import ModuleType
from typing import Awaitable, Callable

import discord
from discord.ext import commands

from arc_bot.storage.slot_storage import SlotStorageError

logger = logging.getLogger("pung-scrim-bot")


def install_events(
    deps: ModuleType,
    *,
    scanner_setup: Callable[[object], Awaitable[None]] | None = None,
) -> None:
    """Install lifecycle handlers once, bound to one bot's dependencies."""
    bot = deps.bot
    if getattr(bot, "_arc_startup_events_installed", False):
        return
    bot._arc_startup_events_installed = True

    async def on_command_error(
        ctx: commands.Context, error: commands.CommandError
    ) -> None:
        original = getattr(error, "original", error)
        command_name = getattr(ctx.command, "qualified_name", "")
        if isinstance(original, SlotStorageError):
            logger.error("Save error", exc_info=original)
            await deps.send_private_command_feedback(
                ctx, "I could not save that change. Please try again.", silent=False
            )
        elif isinstance(original, deps.UnauthorizedGuild):
            await deps.send_private_command_feedback(
                ctx, "This server is not authorized to use this bot.", silent=False
            )
        elif isinstance(error, deps.UnauthorizedAuthAdmin):
            await deps.send_private_command_feedback(
                ctx,
                "❌ **Access Denied.** You do not have permission to manage the "
                "server whitelist.",
                silent=False,
            )
        elif isinstance(error, commands.NotOwner):
            await deps.send_private_command_feedback(
                ctx, "This developer command is restricted to the bot owner.", silent=False
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            usage = {
                "confirm": "Use `!confirm <slot> [<slot> ...]`.",
                "remove": "Use `!remove <slot> [<slot> ...]`.",
                "say": "Use `!say <message>`.",
            }
            await deps.send_private_command_feedback(
                ctx,
                usage.get(
                    command_name,
                    "A required argument is missing. Use `!help` to check the command format.",
                ),
                silent=False,
            )
        elif isinstance(error, commands.BadArgument):
            await deps.send_private_command_feedback(
                ctx,
                "One of the arguments is invalid. Use `!help` to check the command format.",
                silent=False,
            )
        elif isinstance(error, commands.MissingPermissions):
            await deps.send_private_command_feedback(
                ctx, "You do not have permission to use this command.", silent=False
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await deps.send_private_command_feedback(
                ctx, "This command can only be used inside a Discord server.", silent=False
            )
        elif isinstance(error, commands.CommandNotFound):
            await deps.delete_command_message(ctx)
        else:
            logger.exception("Command execution error", exc_info=error)
            await deps.send_private_command_feedback(
                ctx, "The command could not be completed. Please try again.", silent=False
            )

    async def setup_hook() -> None:
        if not deps._loaded:
            deps.repository.load()
            deps._loaded = True
        deps.install_setup_panel()
        ban_system = getattr(deps, "ban_system", None)
        if ban_system is not None:
            await ban_system.start()
        for scrim in deps.repository.scrims.values():
            if (
                not scrim.deleted
                and scrim.registration_review_message_id is not None
                and scrim.staff_channel_id is not None
            ):
                bot.add_view(
                    deps.RegistrationQueueView(scrim.id),
                    message_id=scrim.registration_review_message_id,
                )
        if scanner_setup is not None:
            await scanner_setup(deps)
        bot.add_dynamic_items(deps.ManagerActionButton, deps.StaffActionButton)

    async def migrate_legacy() -> None:
        if deps.repository.legacy is None:
            return
        board = deps.repository.legacy.get("public_board")
        if not isinstance(board, dict) or not board.get("channel_id"):
            logger.warning("Unclaimed v1 state: no resolvable public channel.")
            return
        try:
            channel = await deps.get_channel(int(board["channel_id"]))
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
            scrim = deps.repository.claim_legacy(guild.id, staff.id)
            logger.info("Imported v1 state into scrim %s.", scrim.id)
        except Exception:
            logger.exception("v1 migration failed; state was retained.")

    async def restore_authorized_runtime_state() -> None:
        await deps.idpw_commands.restore_active_idpw(deps)
        authorized = [
            scrim for scrim in list(deps.repository.scrims.values())
            if deps.repository.is_guild_authorized(scrim.guild_id)
        ]
        results = await asyncio.gather(
            *(deps.publish_scrim(scrim) for scrim in authorized),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("Board recovery failed.", exc_info=result)
        for scrim in authorized:
            reconcile_roles = getattr(
                deps, "reconcile_scrim_captain_roles", None
            )
            if reconcile_roles is not None:
                try:
                    await reconcile_roles(scrim)
                except Exception:
                    logger.exception(
                        "Captain role recovery failed for scrim %s.", scrim.id
                    )
            await deps.send_scrim_log_coverage_snapshot(scrim)

    async def on_ready() -> None:
        if bot.user is not None:
            logger.info("Bot connected as %s (ID: %s)", bot.user, bot.user.id)
        await migrate_legacy()
        await restore_authorized_runtime_state()

    async def on_guild_join(guild: discord.Guild) -> None:
        if deps.repository.is_guild_authorized(guild.id):
            return
        me = guild.me
        candidates: list[discord.TextChannel] = []
        if isinstance(guild.system_channel, discord.TextChannel):
            candidates.append(guild.system_channel)
        candidates.extend(channel for channel in guild.text_channels if channel not in candidates)
        channel = next(
            (candidate for candidate in candidates
             if me is None or candidate.permissions_for(me).send_messages),
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
                logger.warning("No sendable text channel found in unauthorized guild %s.", guild.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not notify unauthorized guild %s before leaving.", guild.id)
        finally:
            try:
                await guild.leave()
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not leave unauthorized guild %s.", guild.id)

    bot.add_listener(on_command_error, "on_command_error")
    bot.setup_hook = setup_hook
    bot.add_listener(on_ready, "on_ready")
    bot.add_listener(on_guild_join, "on_guild_join")