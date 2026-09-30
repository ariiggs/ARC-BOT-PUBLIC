"""Staff operational-message panel and anonymous announcements.

The entry point supplies its runtime objects through ``install`` so this
module remains local to each bot tree (and never imports an entry point).
"""
from __future__ import annotations

import discord
from discord.ext import commands

from arc_bot.utils.discord_views import delete_message_or_clear
from arc_bot.views.base import ExpiringView

_d = {}
_installed = False
_owner = None


def install(bot, *, repository, logger, constants, views, callbacks, owner=None):
    global _d, _installed, _owner
    _owner = owner
    _d = dict(repository=repository, logger=logger, **constants, **views, **callbacks)
    if _installed:
        return
    _installed = True
    if bot.get_command("msg") is None:
        bot.add_command(commands.Command(commands.guild_only()(configure_operational_message), name="msg"))
    if bot.get_command("say") is None:
        bot.add_command(commands.Command(commands.guild_only()(say_message), name="say", aliases=["announce"]))


def _repository():
    return getattr(_owner, "repository", _d["repository"])


def _panel_text(scrim):
    if scrim is None:
        return ("**Operational Messages**\nSelect a scrim, then choose which "
                "`!open`/`!close` messages or results publication template to edit.")
    return (f"**Operational Messages — {discord.utils.escape_markdown(scrim.name)}**\n"
            "Choose **Registrations** or **Slots** to edit open/close messages, "
            "or **Results** to edit the `!res` publication.\n"
            "Results placeholders: `{scrim}`, `{team_count}`, `{match_count}`; "
            "for ranks 1–3 use `{top1_team}`, `{top1_points}`, `{top1_kills}`, "
            "`{top1_wins}` (replace `1` with `2` or `3`). Missing ranks show "
            "`—` and zeroes.")


def _preview(message):
    message = message.replace("```", "'''")
    return f"{message[:897]}..." if len(message) > 900 else message


class OperationalMessageView(ExpiringView):
    def __init__(self, owner_id, guild_id, *, selected_id=None):
        super().__init__(timeout=900)
        self.owner_id, self.guild_id, self.selected_id = owner_id, guild_id, selected_id
        self.message = None
        self.rebuild()

    def scrims(self): return _repository().list(self.guild_id)
    def selected_scrim(self):
        s = _repository().get(self.selected_id) if self.selected_id else None
        return s if s and s.guild_id == self.guild_id else None
    def authorized(self, interaction):
        return bool(interaction.guild and interaction.guild.id == self.guild_id
                    and interaction.user.id == self.owner_id
                    and _d["member_is_staff_in_guild"](interaction.user, self.guild_id))
    async def interaction_check(self, interaction):
        if self.authorized(interaction): return True
        text = "This message panel belongs to another staff member, or you no longer have staff access."
        if interaction.response.is_done(): await interaction.followup.send(text, ephemeral=True)
        else: await interaction.response.send_message(text, ephemeral=True)
        return False
    async def on_error(self, interaction, error, item):
        _d["logger"].exception("Operational message panel interaction failed", exc_info=error)
        text = ("The message could not be saved. Nothing was changed."
                if isinstance(error, (_d["SlotStorageError"], ValueError))
                else "Something went wrong. Please try again.")
        if interaction.response.is_done(): await interaction.followup.send(text, ephemeral=True)
        else: await interaction.response.send_message(text, ephemeral=True)
    def content(self): return _panel_text(self.selected_scrim())
    def embed(self):
        scrim = self.selected_scrim()
        embed = discord.Embed(title="Operational Messages", description=(
            "Select a scrim below to edit messages sent by `!open`, `!close`, and `!res`."
            if scrim is None else f"**{discord.utils.escape_markdown(scrim.name)}**\n"
            "The current templates are shown below. Choose Registrations or Slots to edit "
            "open/close messages, or Results to edit the `!res` publication."
        ), color=discord.Color.blurple())
        if scrim is not None:
            messages = getattr(scrim, "operational_messages", {})
            for key in _d["OPERATIONAL_MESSAGE_KEYS"]:
                embed.add_field(name=_d["OPERATIONAL_MESSAGE_LABELS"][key],
                                value=_preview(messages.get(key, _d["DEFAULT_OPERATIONAL_MESSAGES"][key])),
                                inline=False)
        return embed
    def rebuild(self):
        self.clear_items()
        scrims, selected = self.scrims(), self.selected_scrim()
        if selected is None and self.selected_id is not None: self.selected_id = None
        if scrims:
            selector = discord.ui.Select(placeholder="Select a scrim...", min_values=1, max_values=1,
                options=[discord.SelectOption(label=discord.utils.escape_markdown(s.name)[:100],
                value=s.id, default=s.id == self.selected_id) for s in scrims], row=0)
            async def select_callback(interaction):
                if not await self.interaction_check(interaction): return
                self.selected_id = selector.values[0]; self.rebuild()
                await interaction.response.edit_message(content=self.content(), embed=self.embed(), view=self)
            selector.callback = select_callback; self.add_item(selector)
        selected = self.selected_scrim()
        reg = discord.ui.Button(label="Registrations", emoji="📝", style=discord.ButtonStyle.primary,
            disabled=selected is None or getattr(selected, "registration_channel_id", None) is None, row=1)
        slots = discord.ui.Button(label="Slots", emoji="🎮", style=discord.ButtonStyle.primary,
            disabled=selected is None, row=1)
        results = discord.ui.Button(label="Results", emoji="🏆", style=discord.ButtonStyle.primary,
            disabled=selected is None, row=1)
        async def open_modal(interaction, modal):
            if await self.interaction_check(interaction): await interaction.response.send_modal(modal)
        reg.callback = lambda i: open_modal(i, OperationalMessageModal(self, "registration"))
        slots.callback = lambda i: open_modal(i, OperationalMessageModal(self, "slots"))
        results.callback = lambda i: open_modal(i, ResultsMessageModal(self))
        for item in (reg, slots, results): self.add_item(item)
        reset = discord.ui.Button(label="Reset Selected", emoji="↩️", style=discord.ButtonStyle.danger,
                                  disabled=selected is None, row=2)
        close = discord.ui.Button(label="Close", emoji="✖️", style=discord.ButtonStyle.secondary, row=2)
        async def reset_callback(interaction):
            if not await self.interaction_check(interaction): return
            scrim = self.selected_scrim()
            if scrim is None:
                await interaction.response.send_message("Select a scrim first.", ephemeral=True); return
            try: _repository().update_operational_messages(scrim.id, self.guild_id, dict(_d["DEFAULT_OPERATIONAL_MESSAGES"]))
            except (_d["SlotStorageError"], ValueError):
                _d["logger"].exception("Could not reset operational messages")
                await interaction.response.send_message("The messages could not be reset. Nothing was changed.", ephemeral=True); return
            self.rebuild()
            await interaction.response.edit_message(content=self.content(), embed=self.embed(), view=self)
        async def close_callback(interaction):
            if not await self.interaction_check(interaction): return
            self.stop(); await interaction.response.send_message("Operational message panel closed.", ephemeral=True)
            await delete_message_or_clear(getattr(interaction, "message", None),
                log_context="closed operational-message panel", fallback_content="This operational-message panel has ended.")
        reset.callback, close.callback = reset_callback, close_callback
        self.add_item(reset); self.add_item(close)
    async def refresh_message(self):
        self.rebuild()
        if self.message is None: return
        try: await self.message.edit(content=self.content(), embed=self.embed(), view=self,
                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: _d["logger"].exception("Could not refresh the operational message panel")


class OperationalMessageModal(discord.ui.Modal):
    open_message = discord.ui.TextInput(label="Message after !open", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    close_message = discord.ui.TextInput(label="Message after !close", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    def __init__(self, panel, category):
        super().__init__(title=("Edit registration messages" if category == "registration" else "Edit slot messages")[:45], timeout=300)
        self.panel, self.category = panel, category
        scrim = panel.selected_scrim()
        if scrim:
            m, prefix = getattr(scrim, "operational_messages", {}), category
            self.open_message.default = m.get(f"open_{prefix}", _d["DEFAULT_OPERATIONAL_MESSAGES"][f"open_{prefix}"])
            self.close_message.default = m.get(f"close_{prefix}", _d["DEFAULT_OPERATIONAL_MESSAGES"][f"close_{prefix}"])
    async def on_submit(self, interaction):
        if not self.panel.authorized(interaction):
            await interaction.response.send_message("This message panel is no longer available to you.", ephemeral=True); return
        scrim = self.panel.selected_scrim()
        if scrim is None: await interaction.response.send_message("That scrim no longer exists.", ephemeral=True); return
        prefix = self.category
        try: _repository().update_operational_messages(scrim.id, self.panel.guild_id,
            {f"open_{prefix}": str(self.open_message.value).strip(), f"close_{prefix}": str(self.close_message.value).strip()})
        except (_d["SlotStorageError"], ValueError) as error:
            await interaction.response.send_message(f"The messages could not be saved. {error}", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); await self.panel.refresh_message()
        await interaction.followup.send("✅ The operational messages were saved.", ephemeral=True)
    async def on_error(self, interaction, error):
        _d["logger"].exception("Operational message modal failed", exc_info=error)
        text = "The messages could not be saved. Nothing was changed."
        if interaction.response.is_done(): await interaction.followup.send(text, ephemeral=True)
        else: await interaction.response.send_message(text, ephemeral=True)


class ResultsMessageModal(discord.ui.Modal):
    template = discord.ui.TextInput(label="Results publication message", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    def __init__(self, panel):
        super().__init__(title="Edit results publication", timeout=300); self.panel = panel
        scrim = panel.selected_scrim()
        if scrim: self.template.default = getattr(scrim, "operational_messages", {}).get("publish_results", _d["DEFAULT_OPERATIONAL_MESSAGES"]["publish_results"])
    async def on_submit(self, interaction):
        if not self.panel.authorized(interaction):
            await interaction.response.send_message("This message panel is no longer available to you.", ephemeral=True); return
        scrim = self.panel.selected_scrim()
        if scrim is None: await interaction.response.send_message("That scrim no longer exists.", ephemeral=True); return
        try: _repository().update_operational_message(scrim.id, self.panel.guild_id, "publish_results", str(self.template.value).strip())
        except (_d["SlotStorageError"], ValueError) as error:
            await interaction.response.send_message(f"The results message could not be saved. {error}", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); await self.panel.refresh_message()
        await interaction.followup.send("✅ The results publication message was saved.", ephemeral=True)
    async def on_error(self, interaction, error):
        _d["logger"].exception("Results message modal failed", exc_info=error)
        text = "The results message could not be saved. Nothing was changed."
        if interaction.response.is_done(): await interaction.followup.send(text, ephemeral=True)
        else: await interaction.response.send_message(text, ephemeral=True)


async def configure_operational_message(ctx):
    if not _d["member_is_staff_in_guild"](ctx.author, ctx.guild.id):
        await _d["send_private_command_feedback"](ctx, "You do not have the configured Staff role to use `!msg`.", silent=False); return
    selected = _d["resolve_operational_scrim"](ctx)
    panel = OperationalMessageView(ctx.author.id, ctx.guild.id, selected_id=selected.id if selected else None)
    try:
        panel.message = await ctx.send(panel.content(), embed=panel.embed(), view=panel, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException: _d["logger"].exception("Could not open the operational message panel"); return
    await _d["delete_command_message"](ctx)


async def say_message(ctx, *, message: str):
    if not _d["member_has_global_staff_role"](ctx.author, ctx.guild.id):
        await _d["send_private_command_feedback"](ctx, "You do not have the Staff role authorized for this server.", silent=True); return
    message = message.strip()
    if not message or len(message) > 2000:
        await _d["send_private_command_feedback"](ctx, "", silent=True); return
    if not await _d["delete_command_message"](ctx):
        _d["logger"].warning("Could not delete the !say command in channel %s; announcement skipped.", ctx.channel.id); return
    try: await ctx.send(message, allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True))
    except discord.HTTPException: _d["logger"].exception("Could not publish staff announcement in channel %s.", ctx.channel.id)