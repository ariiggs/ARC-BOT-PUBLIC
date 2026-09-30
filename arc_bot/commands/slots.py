"""Slot-board commands shared by the local bot entry points.

The feature deliberately receives its entry-point module as an owner.  This
keeps repositories, publishers, and helper functions isolated when both bot
trees are imported in one interpreter.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from arc_bot.storage.slot_storage import SlotStorageError
from arc_bot.utils.discord_views import delete_message_or_clear, disable_view_items
from arc_bot.views.base import ExpiringView
from scrim_state import STATUS_AVAILABLE, STATUS_CONFIRMED, STATUS_PENDING, STATUS_RESERVED

_owner = None


def bind_owner(owner):
    global _owner
    _owner = owner
    return owner


def _o(name):
    return getattr(_owner, name)


def build_slot_status_embed(scrim):
    counts = {
        STATUS_AVAILABLE: 0, STATUS_RESERVED: 0,
        STATUS_PENDING: 0, STATUS_CONFIRMED: 0,
    }
    for slot in scrim.slots.values():
        if slot.status in counts:
            counts[slot.status] += 1
    embed = discord.Embed(
        title=f"📊 Slot Status - {discord.utils.escape_markdown(scrim.name)}",
        description=(
            f"**Total Slots:** {len(scrim.slots)}\n\n"
            f"{scrim.emoji_available} **Free:** {counts[STATUS_AVAILABLE]}\n"
            f"{scrim.emoji_reserved} **Reserved:** {counts[STATUS_RESERVED]}\n"
            f"{scrim.emoji_pending} **Pending:** {counts[STATUS_PENDING]}\n"
            f"{scrim.emoji_confirmed} **Confirmed:** {counts[STATUS_CONFIRMED]}"
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
        value=", ".join(available) if available else "None", inline=False,
    )
    for status, label, emoji in (
        (STATUS_RESERVED, "Reserved Teams", scrim.emoji_reserved),
        (STATUS_PENDING, "Pending Teams", scrim.emoji_pending),
        (STATUS_CONFIRMED, "Confirmed Teams", scrim.emoji_confirmed),
    ):
        teams = []
        for slot in sorted(scrim.slots.values(), key=lambda item: item.number):
            if slot.status != status:
                continue
            team = discord.utils.escape_mentions(
                discord.utils.escape_markdown(str(getattr(slot, "team_name", "") or "").strip())
            ) or "Unnamed team"
            tag = discord.utils.escape_mentions(
                discord.utils.escape_markdown(str(getattr(slot, "tag", "") or "").strip())
            ) or "No tag"
            teams.append(f"`{slot.number:02d}` {team} · `{tag}`")
        embed.add_field(
            name=f"{emoji} {label} ({counts[status]})",
            value="\n".join(teams) if teams else "None", inline=False,
        )
    embed.set_footer(text="Public status view · team names and slot states only")
    return embed


async def send_slot_status_embed(ctx, scrim):
    try:
        await ctx.send(embed=build_slot_status_embed(scrim),
                       allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        _o("logger").exception("Could not send the slot status summary for %s.", scrim.id)
    finally:
        await _o("delete_command_message")(ctx)


async def _clear_selector_message(message, *, context):
    """Use the shared remover, retaining compatibility with edit-only mocks."""
    if getattr(message, "delete", None) is None:
        edit = getattr(message, "edit", None)
        if edit is not None:
            await edit(
                content="This slot-board update has ended.",
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return
    await delete_message_or_clear(
        message,
        log_context=context,
        fallback_content="This slot-board selector has ended.",
    )


class SlotsScrimSelectView(ExpiringView):
    def __init__(self, *, owner_id, guild_id, scrims):
        super().__init__(timeout=120)
        self.owner_id, self.guild_id = owner_id, guild_id
        self.scrim_ids = {scrim.id for scrim in scrims}
        select = discord.ui.Select(
            placeholder="📊 Select a scrim to view...", min_values=1, max_values=1,
            options=[discord.SelectOption(label=scrim.name[:100], value=scrim.id)
                     for scrim in scrims],
        )

        async def callback(interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This scrim selector belongs to another user.", ephemeral=True)
                return
            if interaction.guild is None or interaction.guild.id != self.guild_id:
                await interaction.response.send_message(
                    "This selector is not valid in this server.", ephemeral=True)
                return
            if not _o("member_has_manager_role")(interaction.user, self.guild_id):
                await interaction.response.send_message(
                    "❌ You do not have permission to use this command.", ephemeral=True)
                self.stop()
                return
            selected_id = select.values[0]
            if selected_id not in self.scrim_ids:
                await interaction.response.send_message(
                    "That scrim selection is no longer available.", ephemeral=True)
                self.stop()
                return
            scrim = _o("repository").get(selected_id)
            if scrim is None or scrim.guild_id != self.guild_id or scrim.deleted:
                await interaction.response.send_message(
                    "That scrim is no longer available.", ephemeral=True)
                self.stop()
                return
            try:
                await interaction.response.edit_message(
                    content=None, embed=build_slot_status_embed(scrim), view=None)
            except discord.HTTPException:
                _o("logger").exception("Could not render the selected !slots summary.")
            finally:
                self.stop()
        select.callback = callback
        self.add_item(select)


async def show_slots(ctx, *, name=None):
    if not _o("member_has_manager_role")(ctx.author, ctx.guild.id):
        await _o("send_private_command_feedback")(
            ctx, "❌ You do not have permission to use this command.",
            silent=False, delete_after=20)
        return
    active_scrims = _o("repository").list(ctx.guild.id)
    if not active_scrims:
        await _o("send_private_command_feedback")(
            ctx, "❌ No active scrims found.", silent=False, delete_after=20)
        return
    if name:
        scrim = _o("resolve_named_scrim")(ctx.guild.id, name)
        if scrim is None:
            await _o("send_private_command_feedback")(
                ctx, "❌ No active scrim found with that name.",
                silent=False, delete_after=20)
            return
        await send_slot_status_embed(ctx, scrim)
        return
    channel_scrim = _o("resolve_channel_scrim")(ctx)
    if channel_scrim is not None:
        await send_slot_status_embed(ctx, channel_scrim)
        return
    view = SlotsScrimSelectView(owner_id=ctx.author.id, guild_id=ctx.guild.id,
                                scrims=active_scrims)
    try:
        await ctx.send("📊 Select a scrim to view its slot status:",
                       view=view, delete_after=150)
    except discord.HTTPException:
        _o("logger").exception("Could not show the !slots scrim selector.")
    finally:
        await _o("delete_command_message")(ctx)


class UpdateScrimSelectView(ExpiringView):
    def __init__(self, *, owner_id, guild_id, scrims):
        super().__init__(timeout=120)
        self.owner_id, self.guild_id = owner_id, guild_id
        self.scrim_ids = {scrim.id for scrim in scrims}
        self.processing = False
        select = discord.ui.Select(
            placeholder="🔄 Select a scrim to update...", min_values=1, max_values=1,
            options=[discord.SelectOption(label=scrim.name[:100], value=scrim.id)
                     for scrim in scrims],
        )

        async def callback(interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This scrim selector belongs to another user.", ephemeral=True)
                return
            if interaction.guild is None or interaction.guild.id != self.guild_id:
                await interaction.response.send_message(
                    "This selector is not valid in this server.", ephemeral=True)
                return
            selected_id = select.values[0]
            if selected_id not in self.scrim_ids:
                await interaction.response.send_message(
                    "That scrim selection is no longer available.", ephemeral=True)
                self.stop()
                await _clear_selector_message(
                    getattr(interaction, "message", None),
                    context="stale !update selector",
                )
                return
            scrim = _o("repository").get(selected_id)
            if scrim is None or scrim.guild_id != self.guild_id or scrim.deleted:
                await interaction.response.send_message(
                    "That scrim is no longer available.", ephemeral=True)
                self.stop()
                await _clear_selector_message(
                    getattr(interaction, "message", None),
                    context="inactive !update selector",
                )
                return
            if not _o("member_is_staff")(interaction.user, scrim):
                await interaction.response.send_message(
                    "You do not have the authorized staff role for this scrim.",
                    ephemeral=True)
                self.stop()
                await _clear_selector_message(
                    getattr(interaction, "message", None),
                    context="unauthorized !update selector",
                )
                return
            if self.processing:
                await interaction.response.send_message(
                    "This slot-board update is already being processed.", ephemeral=True)
                return
            self.processing = True
            try:
                await interaction.response.defer(ephemeral=True)
                updated = await _o("publish_scrim")(scrim)
                result_message = ("The slot board has been updated." if updated
                                  else "The slot board could not be updated.")
            except (OSError, ValueError, SlotStorageError, discord.HTTPException):
                _o("logger").exception("Could not update the slot board for scrim %s.", scrim.id)
                result_message = "The slot board could not be updated."
            finally:
                self.stop()
                disable_view_items(self)
                await _clear_selector_message(
                    getattr(interaction, "message", None),
                    context="completed !update selector",
                )
            await interaction.followup.send(result_message, ephemeral=True)
        select.callback = callback
        self.add_item(select)


async def update_slots_command(ctx, *, name=None):
    active_scrims = _o("repository").list(ctx.guild.id)
    if not active_scrims:
        await _o("send_private_command_feedback")(
            ctx, "No active scrims are configured for this server.", silent=False)
        return
    if name:
        scrim = _o("resolve_named_scrim")(ctx.guild.id, name)
        if scrim is None:
            await _o("send_private_command_feedback")(
                ctx, "Scrim not found for this server.", silent=False)
            return
        if not _o("member_is_staff")(ctx.author, scrim):
            await _o("send_private_command_feedback")(
                ctx, "You must have the authorized staff role to update this board.",
                silent=False)
            return
        if await _o("publish_scrim")(scrim):
            await _o("send_private_command_feedback")(ctx, "✅ The slot board has been updated.")
        else:
            await _o("send_private_command_feedback")(
                ctx, "❌ The board was not updated. Check the bot's channel permissions "
                "and try `!update` again.", silent=False, delete_after=30)
        return
    channel_scrim = _o("resolve_channel_scrim")(ctx)
    if channel_scrim is not None:
        if not _o("member_is_staff")(ctx.author, channel_scrim):
            await _o("send_private_command_feedback")(
                ctx, "You must have the authorized Staff role to update this board.",
                silent=False)
            return
        if await _o("publish_scrim")(channel_scrim):
            await _o("send_private_command_feedback")(ctx, "✅ The slot board has been updated.")
        else:
            await _o("send_private_command_feedback")(
                ctx, "❌ The board was not updated. Check the bot's channel permissions "
                "and try `!update` again.", silent=False, delete_after=30)
        return
    view = UpdateScrimSelectView(owner_id=ctx.author.id, guild_id=ctx.guild.id,
                                 scrims=active_scrims)
    try:
        await ctx.send("🔄 Select a scrim to update:", view=view, delete_after=150)
    except discord.HTTPException:
        _o("logger").exception("Could not show the !update scrim selector.")
    finally:
        await _o("delete_command_message")(ctx)


def install_slots_commands(bot, owner):
    bind_owner(owner)
    bot.command(name="slots", aliases=["s"])(commands.guild_only()(show_slots))
    bot.command(name="update", aliases=["u", "publier_slots"])(
        commands.guild_only()(update_slots_command))
    return {
        "SlotsScrimSelectView": SlotsScrimSelectView,
        "UpdateScrimSelectView": UpdateScrimSelectView,
        "show_slots": show_slots,
        "update_slots_command": update_slots_command,
        "build_slot_status_embed": build_slot_status_embed,
    }