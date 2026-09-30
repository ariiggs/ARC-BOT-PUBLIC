"""Discord message operations shared by command and setup modules."""

from __future__ import annotations

from typing import Any

import discord


async def delete_command_message(ctx: Any) -> None:
    """Delete the invoking command message when Discord permits it."""
    message = getattr(ctx, "message", None)
    delete = getattr(message, "delete", None)
    if delete is None:
        return
    try:
        await delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass