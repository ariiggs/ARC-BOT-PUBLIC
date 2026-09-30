"""Lifecycle commands shared by one bot instance.

The module is deliberately bound to an owner module.  This keeps the root and
Public bots independent while retaining the long-standing ``main`` patch
seams used by integrations and tests.
"""

from __future__ import annotations

import discord
from discord.ext import commands

_owner = None
_perform_impl = None
ResetConfirmationView = None


async def perform_scrim_reset(scrim, invoking_channel):
    """Compatibility entry point bound to the importing bot."""
    return await _perform_impl(scrim, invoking_channel)


def install_lifecycle_commands(bot, owner):
    """Install reset/open/close once, resolving dependencies from ``owner``."""
    global _owner, _perform_impl, ResetConfirmationView
    _owner = owner
    _perform_impl = owner.perform_scrim_reset
    if bot.get_command("reset") is not None:
        return (
            perform_scrim_reset,
            ResetConfirmationView or owner.ResetConfirmationView,
            bot.get_command("reset"),
            bot.get_command("open"),
            bot.get_command("close"),
        )

    class ResetConfirmationView(owner.DurableView):
        def __init__(self, scrim, invoking_channel_id: int):
            super().__init__(timeout=60)
            self.scrim = scrim
            self.scrim_id = scrim.id
            self.guild_id = scrim.guild_id
            self.invoking_channel_id = invoking_channel_id
            self.message = None
            self.completed = False

        async def interaction_check(self, interaction):
            current = owner.repository.get(self.scrim_id)
            allowed = (
                current is self.scrim
                and owner.is_active(self.scrim)
                and interaction.guild is not None
                and interaction.guild.id == self.guild_id
                and interaction.channel_id == self.invoking_channel_id
                and interaction.message is not None
                and (
                    self.message is None
                    or interaction.message.id == self.message.id
                )
                and owner.member_is_staff(interaction.user, self.scrim)
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
                    owner.logger.exception(
                        "Could not reject an invalid reset confirmation."
                    )
            return allowed

        async def on_timeout(self):
            if self.completed:
                return
            self.completed = True
            owner.disable_view_items(self)
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
            label="Confirm Reset", emoji="⚠️", style=discord.ButtonStyle.danger
        )
        async def confirm_reset(self, interaction, button):
            if not await self.interaction_check(interaction):
                return
            if self.completed:
                await interaction.response.send_message(
                    "This reset confirmation is no longer active.", ephemeral=True
                )
                return
            self.completed = True
            owner.disable_view_items(self)
            self.stop()
            await interaction.response.defer()
            result = await owner.perform_scrim_reset(self.scrim, interaction.channel)
            if self.message is not None:
                try:
                    await self.message.edit(content=result, view=self)
                except discord.NotFound:
                    try:
                        await interaction.followup.send(result, ephemeral=True)
                    except discord.HTTPException:
                        owner.logger.exception(
                            "Could not send the reset result privately."
                        )
                except discord.HTTPException:
                    owner.logger.exception(
                        "Could not update the reset confirmation message."
                    )

        @discord.ui.button(
            label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary
        )
        async def cancel_reset(self, interaction, button):
            if not await self.interaction_check(interaction):
                return
            if self.completed:
                await interaction.response.send_message(
                    "This reset confirmation is no longer active.", ephemeral=True
                )
                return
            self.completed = True
            owner.disable_view_items(self)
            self.stop()
            await interaction.response.edit_message(
                content="Reset cancelled. No slots were changed.", view=self
            )

    @bot.command(name="reset")
    @commands.guild_only()
    async def reset_slots(ctx):
        scrim = await owner.require_staff_scrim(ctx, allow_public=True)
        if scrim is None:
            return
        confirmation = ResetConfirmationView(scrim, ctx.channel.id)
        reset_channel_label = "staff and public"
        if getattr(scrim, "registration_channel_id", None) is not None:
            reset_channel_label += ", and registration"
        confirmation.message = await owner.send_private_command_feedback(
            ctx,
            (
                f"⚠️ Reset **{discord.utils.escape_markdown(scrim.name)}**? "
                f"This clears every slot and the configured {reset_channel_label} "
                "channels, keeping pinned messages. "
                "Click **Confirm Reset** to continue."
            ),
            view=confirmation,
            delete_after=120,
        )

    @bot.command(name="open", aliases=["o"])
    @commands.guild_only()
    async def open_scrim(ctx):
        registration_scrim = owner.resolve_registration_scrim(ctx)
        if registration_scrim is not None:
            if await owner.require_registration_staff_channel(ctx) is None:
                return
            missing = owner.pause_scrim_if_unconfigured(registration_scrim)
            if missing:
                await owner.send_private_command_feedback(
                    ctx,
                    "This scrim cannot be opened while required settings are missing: "
                    + ", ".join(missing)
                    + ". Complete the !setup configuration first.",
                    silent=False,
                )
                return
            if not await owner.set_registration_channel_open(ctx, registration_scrim, True):
                await owner.send_private_command_feedback(
                    ctx,
                    "The registration channel could not be opened. Check the configured "
                    "registration role and channel permissions.",
                    silent=False,
                )
                return
            await owner.delete_command_message(ctx)
            await owner.send_scrim_log(
                registration_scrim, "REGISTRATIONS OPENED",
                "The configured registration role can now send messages.",
            )
            await owner.send_operational_message(ctx, registration_scrim, "open_registration")
            await owner.send_private_command_feedback(
                ctx, "✅ Registration is now open.", delete_command=False
            )
            return
        scrim = await owner.require_staff_scrim(ctx, allow_public=True)
        if scrim is None:
            return
        async with scrim.state_lock:
            if not owner.is_active(scrim):
                await owner.send_private_command_feedback(
                    ctx, "This scrim no longer exists.", silent=True
                )
                return
            with owner.repository.transaction():
                scrim.is_open = True
        board_refreshed = await owner.refresh_public_slots(scrim)
        await owner.delete_command_message(ctx)
        await owner.send_scrim_log(
            scrim, "SCRIM OPENED",
            "Manager interactions were opened." if board_refreshed
            else "Open state saved, but the public board could not be refreshed.",
        )
        if not board_refreshed:
            await owner.send_private_command_feedback(
                ctx,
                "The scrim is open, but its public board could not be refreshed. "
                "Check the bot's channel permissions, then run `!update`.",
                delete_command=False, delete_after=30,
            )
            return
        await owner.send_operational_message(ctx, scrim, "open_slots")
        await owner.send_private_command_feedback(
            ctx, "✅ Slot interactions are now open.", delete_command=False
        )

    @bot.command(name="close", aliases=["c"])
    @commands.guild_only()
    async def close_scrim(ctx):
        registration_scrim = owner.resolve_registration_scrim(ctx)
        if registration_scrim is not None:
            if await owner.require_registration_staff_channel(ctx) is None:
                return
            if not await owner.set_registration_channel_open(ctx, registration_scrim, False):
                await owner.send_private_command_feedback(
                    ctx,
                    "The registration channel could not be closed. Check the configured "
                    "registration role and channel permissions.",
                    silent=False,
                )
                return
            await owner.delete_command_message(ctx)
            await owner.send_scrim_log(
                registration_scrim, "REGISTRATIONS CLOSED",
                "The configured registration role can no longer send messages.",
            )
            await owner.send_operational_message(ctx, registration_scrim, "close_registration")
            return
        scrim = await owner.require_staff_scrim(ctx, allow_public=True)
        if scrim is None:
            return
        async with scrim.state_lock:
            if not owner.is_active(scrim):
                await owner.send_private_command_feedback(
                    ctx, "This scrim no longer exists.", silent=True
                )
                return
            with owner.repository.transaction():
                scrim.is_open = False
        controls_removed = await owner.remove_public_controls(scrim)
        await owner.send_scrim_log(
            scrim, "SCRIM CLOSED",
            "Manager interactions were closed." if controls_removed
            else "Closed state saved, but the public controls could not be removed.",
        )
        await owner.delete_command_message(ctx)
        if not controls_removed:
            await owner.send_private_command_feedback(
                ctx,
                "The scrim is closed, but the public board may still show old controls. "
                "They should no longer work; check permissions and run `!update`.",
                delete_command=False, delete_after=30,
            )
            return
        await owner.send_operational_message(ctx, scrim, "close_slots")

    globals()["ResetConfirmationView"] = ResetConfirmationView
    return perform_scrim_reset, ResetConfirmationView, reset_slots, open_scrim, close_scrim