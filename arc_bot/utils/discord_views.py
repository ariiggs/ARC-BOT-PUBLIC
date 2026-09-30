"""Small Discord UI helpers shared by interactive panels."""

from __future__ import annotations

import logging

import discord


logger = logging.getLogger("pung-scrim-bot")


def disable_view_items(view: discord.ui.View) -> None:
    """Disable normal and reconstructed dynamic controls explicitly."""
    for item in view.children:
        if isinstance(item, discord.ui.DynamicItem):
            item = item.item
        if hasattr(item, "disabled"):
            item.disabled = True


async def delete_message_or_clear(
    message: discord.Message | None,
    *,
    log_context: str,
    fallback_content: str | None = None,
) -> bool:
    """Remove a completed interaction message, clearing it if deletion fails."""
    if message is None:
        return False
    delete = getattr(message, "delete", None)
    if delete is None:
        return False
    try:
        await delete()
        return True
    except discord.NotFound:
        return True
    except discord.HTTPException:
        logger.exception("Could not delete %s.", log_context)
        if fallback_content is not None:
            try:
                await message.edit(
                    content=fallback_content,
                    embed=None,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not clear %s.", log_context)
        return False