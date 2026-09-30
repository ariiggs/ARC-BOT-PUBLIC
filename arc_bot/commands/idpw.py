"""ID/password announcements and their delayed reminder lifecycle.

The implementation is deliberately dependency-injected.  Both bot trees bind
their own module namespace, which keeps the historical monkeypatch seams
(``main.repository``, ``main.require_staff_scrim`` etc.) intact without making
the standalone Public tree import its root ``main`` module.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
from discord.ext import commands

from scrim_state import MAX_MATCHES, timezone_for_name
from arc_bot.storage.slot_storage import SlotStorageError

active_idpw: dict[str, dict[str, object]] = {}
active_idpw_locks: dict[str, asyncio.Lock] = {}


def _d(owner, name):
    return getattr(owner, name)


def _active_state(owner):
    return getattr(owner, "active_idpw", active_idpw)


def _active_locks(owner):
    return getattr(owner, "active_idpw_locks", active_idpw_locks)


def _format_idpw_reminder(*, title, message, confirmed_role_id):
    lines = [f"# **{title}**", f"**{message}**"]
    if confirmed_role_id is not None:
        lines.append(f"<@&{confirmed_role_id}>")
    return "\n".join(lines)


def _format_idpw_announcement(*, match_number, room_id, password, start_time,
                              confirmed_role_id, map_name=None,
                              start_label="Start", separate_role_mention=False):
    detail = []
    if map_name:
        detail.append(f"Map : {discord.utils.escape_markdown(map_name)}")
    detail.extend((f"ID : `{discord.utils.escape_markdown(room_id)}`",
                   f"PW : {discord.utils.escape_markdown(password)}",
                   f"{start_label} : {start_time}"))
    lines = [f"# __**{'Match ' + str(match_number) if match_number is not None else 'Next Match'}**__"]
    if separate_role_mention:
        lines.append("")
    lines.append(f"**{chr(10).join(detail)}**")
    if confirmed_role_id is not None:
        if separate_role_mention:
            lines.append("")
        lines.append(f"<@&{confirmed_role_id}>")
    return "\n".join(lines)


def _track_active_idpw_message(owner, scrim_id, state, message):
    states = _active_state(owner)
    if states.get(scrim_id) is not state:
        return False
    state.setdefault("messages", []).append(message)
    return True


async def _send_scheduled_idpw_reminder(owner, scrim, state, channel, *,
                                        start_timestamp, stage,
                                        confirmed_role_id):
    due = start_timestamp - (180 if stage == "three_minute" else 60)
    delay = due - _d(owner, "time").time()
    if delay > 0:
        await asyncio.sleep(delay)
    title, text = (("The match starts in 3 minutes", "Please prepare.")
                   if stage == "three_minute"
                   else ("FINAL CALL", "The match begins in 1 minute."))
    mentions = discord.AllowedMentions(everyone=False, users=False,
                                        roles=confirmed_role_id is not None,
                                        replied_user=False)
    retry_delays = (5, 15, 30)
    for attempt in range(4):
        if _active_state(owner).get(scrim.id) is not state:
            return
        if not _d(owner, "repository").is_guild_authorized(scrim.guild_id):
            await _d(owner, "clear_active_idpw")(scrim.id)
            return
        remaining = start_timestamp - _d(owner, "time").time()
        if remaining <= 0:
            return
        try:
            message = await channel.send(
                content=_format_idpw_reminder(title=title, message=text,
                                               confirmed_role_id=confirmed_role_id),
                allowed_mentions=mentions)
        except (discord.Forbidden, discord.HTTPException):
            if attempt == 3:
                return
            await asyncio.sleep(min(retry_delays[attempt], remaining))
            continue
        if not _track_active_idpw_message(owner, scrim.id, state, message):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return
        try:
            _d(owner, "repository").set_idpw_reminder(scrim.id, stage, message.id)
        except (SlotStorageError, ValueError):
            pass
        return


async def restore_active_idpw(owner):
    repository = _d(owner, "repository")
    states = _active_state(owner)
    for config in tuple(repository.idpw_configs.values()):
        if (config.start_timestamp is None or config.announcement_message_id is None
                or config.announcement_channel_id is None
                or config.scrim_id in states):
            continue
        scrim = repository.get(config.scrim_id)
        if scrim is None or not repository.is_guild_authorized(scrim.guild_id):
            continue
        try:
            channel = await _d(owner, "configured_text_channel")(
                scrim, config.announcement_channel_id)
            if channel is None:
                continue
            announcement = await channel.fetch_message(config.announcement_message_id)
        except discord.NotFound:
            await _d(owner, "clear_active_idpw")(scrim.id)
            continue
        except (discord.Forbidden, discord.HTTPException):
            continue
        messages = [announcement]
        for message_id in (config.three_minute_reminder_message_id,
                           config.one_minute_reminder_message_id):
            if message_id is None:
                continue
            try:
                messages.append(await channel.fetch_message(message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        state = {"message": announcement, "messages": messages, "tasks": []}
        states[scrim.id] = state
        remaining = config.start_timestamp - _d(owner, "time").time()
        if config.three_minute_reminder_message_id is None and remaining >= 180:
            state["tasks"].append(asyncio.create_task(_send_scheduled_idpw_reminder(
                owner, scrim, state, channel, start_timestamp=config.start_timestamp,
                stage="three_minute", confirmed_role_id=scrim.confirmed_role_id)))
        if config.one_minute_reminder_message_id is None and remaining > 0:
            state["tasks"].append(asyncio.create_task(_send_scheduled_idpw_reminder(
                owner, scrim, state, channel, start_timestamp=config.start_timestamp,
                stage="one_minute", confirmed_role_id=scrim.confirmed_role_id)))


async def clear_active_idpw(owner, scrim_id=None, *, preserve_message_id=None):
    repository = _d(owner, "repository")
    states = _active_state(owner)
    ids = ([scrim_id] if scrim_id is not None else
           list(set(states) | set(repository.idpw_configs)))
    for current_id in ids:
        state = states.pop(current_id, {})
        for task in state.get("tasks", []):
            if not task.done():
                task.cancel()
        messages = list(state.get("messages", []))
        message = state.get("message")
        if message is not None and all(getattr(m, "id", None) != getattr(message, "id", None)
                                       for m in messages):
            messages.append(message)
        config = repository.get_idpw_config(current_id)
        announcement_id = getattr(config, "announcement_message_id", None)
        if config is not None and announcement_id is not None:
            if all(getattr(m, "id", None) != announcement_id for m in messages):
                scrim = repository.get(current_id)
                if scrim is not None:
                    try:
                        channel = await _d(owner, "configured_text_channel")(
                            scrim, config.announcement_channel_id)
                        if channel is not None:
                            messages.append(await channel.fetch_message(announcement_id))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
        for item in messages:
            if getattr(item, "id", None) == preserve_message_id:
                continue
            try:
                await item.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        try:
            repository.set_idpw_announcement(current_id, None)
        except (SlotStorageError, ValueError):
            pass


async def _parse_idpw_input(owner, ctx, input_string, command_name):
    scrim = await _d(owner, "require_staff_scrim")(ctx, allow_public=True)
    if scrim is None:
        return None
    repository = _d(owner, "repository")
    config = repository.get_idpw_config(scrim.id)
    if config is None:
        await _d(owner, "send_private_command_feedback")(ctx,
            "ID/password distribution is not configured. Configure ID/PW in `!setup` first.")
        return None
    parts = [part.strip() for part in input_string.split("/")]
    dynamic = str(getattr(scrim, "pw_type", "fixed")).casefold() == "dynamic"
    expected = 3 if dynamic else 2
    fmt = (f"❌ Format: `{command_name} <room_id> / <password> / <minutes>`"
           if dynamic else f"❌ Format: `{command_name} <room_id> / <minutes>`")
    if len(parts) != expected or any(not part for part in parts):
        await _d(owner, "send_private_command_feedback")(ctx, fmt)
        return None
    try:
        minutes = int(parts[-1])
    except ValueError:
        await _d(owner, "send_private_command_feedback")(ctx, "❌ Minutes must be a whole number.")
        return None
    if minutes < 1:
        await _d(owner, "send_private_command_feedback")(ctx, "❌ Minutes must be at least 1.")
        return None
    return scrim, config, parts[0], minutes, (parts[1] if dynamic else
        getattr(scrim, "fixed_pw", "") or config.fixed_password)


async def _publish_idpw(owner, ctx, lobby_id, minutes, requested_match=None, *,
                        password=None, scrim=None, config=None, specific_match=False):
    if scrim is None:
        scrim = await _d(owner, "require_staff_scrim")(ctx, allow_public=True)
        if scrim is None:
            return
    lock = _active_locks(owner).setdefault(scrim.id, asyncio.Lock())
    if lock.locked():
        await _d(owner, "send_private_command_feedback")(ctx,
            "Another ID/PW update is in progress for this scrim. Wait and retry.",
            delete_after=30)
        return
    async with lock:
        await _publish_idpw_locked(owner, ctx, lobby_id, minutes, requested_match,
            password=password, scrim=scrim, config=config, specific_match=specific_match)


def _match_map_for_scrim(scrim, number):
    maps = getattr(scrim, "maps", ()) if getattr(scrim, "match_maps", None) is None else scrim.match_maps
    if isinstance(maps, str):
        maps = [x.strip() for x in maps.strip("[]").split(",") if x.strip()]
    if not isinstance(maps, (list, tuple)) or number is None or not 0 <= number - 1 < len(maps):
        return None
    return maps[number - 1].strip() if isinstance(maps[number - 1], str) else None


async def _publish_idpw_locked(owner, ctx, lobby_id, minutes, requested_match=None, *,
                               password=None, scrim=None, config=None, specific_match=False):
    repository = _d(owner, "repository")
    if config is None:
        config = repository.get_idpw_config(scrim.id)
    if config is None:
        await _d(owner, "send_private_command_feedback")(ctx, "ID/password distribution is not configured.")
        return
    if specific_match and (requested_match is None or not 1 <= requested_match <= scrim.max_matches
                           or not _match_map_for_scrim(scrim, requested_match)):
        await _d(owner, "send_private_command_feedback")(ctx,
            "Set up Matches & Maps and assign a map to this match in `!setup` before using `!idpwgN`.")
        return
    channel = await _d(owner, "configured_text_channel")(scrim, config.target_channel_id)
    if channel is None:
        await _d(owner, "send_private_command_feedback")(ctx, "The configured ID/password channel could not be found or used.")
        return
    start = int(_d(owner, "time").time()) + minutes * 60
    local = datetime.fromtimestamp(start, tz=timezone_for_name(config.timezone_name))
    password = password if password is not None else getattr(scrim, "fixed_pw", "") or config.fixed_password
    content = _format_idpw_announcement(match_number=requested_match if specific_match else None,
        room_id=lobby_id, password=password, start_time=local.strftime("%H:%M"),
        confirmed_role_id=scrim.confirmed_role_id,
        map_name=_match_map_for_scrim(scrim, requested_match) if specific_match else None,
        start_label="Start Time", separate_role_mention=specific_match)
    try:
        message = await channel.send(content=content, allowed_mentions=discord.AllowedMentions(
            everyone=False, users=False, roles=True, replied_user=False))
        repository.set_idpw_run(scrim.id, announcement_message_id=message.id,
            announcement_channel_id=channel.id, start_timestamp=start)
    except (discord.Forbidden, discord.HTTPException):
        await _d(owner, "send_private_command_feedback")(ctx, "The MATCH ACCESS message could not be sent to the configured channel.")
        return
    except (SlotStorageError, ValueError):
        await message.delete()
        await _d(owner, "send_private_command_feedback")(ctx,
            "The announcement was sent, but its reminder schedule could not be saved. The previous announcement was kept; please retry after checking storage.",
            delete_after=30)
        return
    await _d(owner, "clear_active_idpw")(
        scrim.id, preserve_message_id=message.id
    )
    state = {"message": message, "messages": [message], "tasks": []}
    _active_state(owner)[scrim.id] = state
    if specific_match:
        with repository.transaction():
            scrim.current_match_counter = requested_match % scrim.max_matches + 1
    tasks = state["tasks"]
    if minutes > 3:
        tasks.append(asyncio.create_task(_send_scheduled_idpw_reminder(owner, scrim, state, channel,
            start_timestamp=start, stage="three_minute", confirmed_role_id=scrim.confirmed_role_id)))
    tasks.append(asyncio.create_task(_send_scheduled_idpw_reminder(owner, scrim, state, channel,
        start_timestamp=start, stage="one_minute", confirmed_role_id=scrim.confirmed_role_id)))
    await _d(owner, "delete_command_message")(ctx)


async def _publish_idpwg(owner, ctx, game_number, input_string):
    parsed = await _parse_idpw_input(owner, ctx, input_string, f"!idpwg{game_number}")
    if parsed is None:
        return
    scrim, config, room, minutes, password = parsed
    await _publish_idpw(owner, ctx, room, minutes, requested_match=game_number,
                        password=password, scrim=scrim, config=config, specific_match=True)


def install_idpw_commands(bot, owner):
    """Bind commands once and return the public compatibility callables."""
    bot.remove_command("idpw")
    for number in range(1, MAX_MATCHES + 1):
        bot.remove_command(f"idpwg{number}")
    @bot.command(name="idpw")
    @commands.guild_only()
    async def distribute_idpw(ctx, *, input_string):
        parsed = await _parse_idpw_input(owner, ctx, input_string, "!idpw")
        if parsed:
            scrim, config, room, minutes, password = parsed
            await _publish_idpw(owner, ctx, room, minutes, password=password,
                                scrim=scrim, config=config)
    for number in range(1, MAX_MATCHES + 1):
        async def handler(ctx, *, input_string, _number=number):
            await _publish_idpwg(owner, ctx, _number, input_string)
        handler.__name__ = f"distribute_idpwg{number}"
        bot.command(name=f"idpwg{number}")(handler)
    return SimpleNamespace(
        _format_idpw_reminder=_format_idpw_reminder,
        _format_idpw_announcement=_format_idpw_announcement,
        _parse_idpw_input=lambda ctx, text, name: _parse_idpw_input(owner, ctx, text, name),
        _publish_idpw=lambda *a, **kw: _publish_idpw(owner, *a, **kw),
        _publish_idpwg=lambda *a, **kw: _publish_idpwg(owner, *a, **kw),
        restore_active_idpw=lambda: restore_active_idpw(owner),
        clear_active_idpw=lambda *a, **kw: clear_active_idpw(owner, *a, **kw),
        _send_scheduled_idpw_reminder=lambda *a, **kw: _send_scheduled_idpw_reminder(owner, *a, **kw),
    )