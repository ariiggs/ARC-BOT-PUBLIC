"""Shared discord.py client and gateway-intent configuration."""

from __future__ import annotations

import discord
from discord.ext import commands


MESSAGE_CACHE_LIMIT = 100


def create_intents() -> discord.Intents:
    """Return only gateway intents used by the bot's command and role flows."""
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.members = True
    intents.message_content = True
    return intents


def create_bot(
    command_prefix: str,
    *,
    intents: discord.Intents | None = None,
    help_command: commands.HelpCommand | None = None,
) -> commands.Bot:
    """Build a bot with bounded message history and no retained member cache."""
    active_intents = create_intents() if intents is None else intents
    return commands.Bot(
        command_prefix=command_prefix,
        intents=active_intents,
        help_command=help_command,
        max_messages=MESSAGE_CACHE_LIMIT,
        member_cache_flags=discord.MemberCacheFlags.none(),
        chunk_guilds_at_startup=False,
    )