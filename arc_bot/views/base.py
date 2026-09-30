"""Shared timeout and persistence behavior for interactive Discord views."""

from __future__ import annotations

import logging

import discord

from arc_bot.storage.slot_storage import SlotStorageError
from arc_bot.utils.discord_views import disable_view_items


logger = logging.getLogger("pung-scrim-bot")


class ExpiringView(discord.ui.View):
    """Retire timed controls visibly instead of leaving a dead panel behind."""

    timeout_notice = "⏱️ This panel expired. Run the command again to reopen it."

    async def on_timeout(self) -> None:
        disable_view_items(self)
        self.stop()
        message = getattr(self, "message", None)
        if message is None:
            return
        content = getattr(message, "content", None) or ""
        notice = self.timeout_notice
        if notice not in content:
            content = f"{content}\n\n{notice}" if content else notice
        try:
            await message.edit(
                content=content,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden):
            return
        except discord.HTTPException:
            logger.exception("Could not retire an expired interactive panel.")


class DurableView(ExpiringView):
    """Show a safe failure message when a persistent action cannot be saved."""

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