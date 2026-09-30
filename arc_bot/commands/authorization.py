"""Authorization and bot-admin commands shared by the ARC bot editions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable

import discord
from discord.ext import commands

from arc_bot.domain.license_labels import license_display_name
from arc_bot.storage.slot_storage import SlotStorageError
from arc_bot.utils.discord_views import delete_message_or_clear, disable_view_items
from arc_bot.views.base import DurableView
from scrim_state import LICENSE_TYPES


class UnauthorizedGuild(commands.CheckFailure):
    """Raised when a command is used from a guild outside the whitelist."""


class UnauthorizedAuthAdmin(commands.CheckFailure):
    """Raised when a user without bot-admin access invokes !auth."""


_bot = None
_repository = None
_product = "beta"
_feedback: Callable | None = None
_delete_command: Callable | None = None
_logger = None
_guild_name = None


def configure(bot, repository, product: str, feedback, delete_command, logger, guild_name=None):
    """Bind this command set to one bot's runtime dependencies."""
    global _bot, _repository, _product, _feedback, _delete_command, _logger, _guild_name
    _bot, _repository, _product = bot, repository, product
    _feedback, _delete_command, _logger = feedback, delete_command, logger
    _guild_name = guild_name or authorized_guild_name


def _repo():
    return _repository() if callable(_repository) else _repository


def arc_product_label(product: str) -> str:
    return "A.R.C. Beta" if product == "beta" else "A.R.C. Public"


async def whitelist_check(ctx: commands.Context) -> bool:
    command_name = getattr(ctx.command, "qualified_name", "")
    if (command_name == "auth" or command_name.startswith("auth ")
            or command_name == "admin" or command_name.startswith("admin ")
            or command_name in {"sub", "status"}):
        return True
    if ctx.guild is None or _repo().is_guild_authorized(ctx.guild.id):
        return True
    raise UnauthorizedGuild()


async def auth_admin_check(ctx: commands.Context) -> bool:
    # Only the Discord bot owner may manage guild authorizations. Guild ownership
    # and previously delegated bot-admin IDs do not grant access to !auth.
    if await _bot.is_owner(ctx.author):
        return True
    raise UnauthorizedAuthAdmin()


def auth_admin_required():
    return commands.check(auth_admin_check)


def build_auth_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="💎 A.R.C. Authorization Manager",
        description=f"Managing **{arc_product_label(_product)}**. Choose a license tier.",
        color=discord.Color.blurple(),
    )


def authorization_time_left_text(expires_at: datetime | None, duration_days: int) -> str:
    if expires_at is None:
        return "Unlimited" if duration_days == 0 else "Expiry unavailable"
    expires_at = expires_at.astimezone(timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return f"Expired on {expires_at.strftime('%Y-%m-%d %H:%M UTC')}"
    # Kept local to avoid coupling this module to the subscription command.
    delta = expires_at - datetime.now(timezone.utc)
    days = delta.days
    hours = delta.seconds // 3600
    return f"{days} day(s), {hours} hour(s)"


def authorization_rows(license_type: str):
    return _repo().list_authorizations(include_expired=True, license_type=license_type)


async def authorized_guild_name(guild_id: int) -> str | None:
    guild = _bot.get_guild(guild_id)
    if guild is not None:
        return guild.name
    try:
        guild = await _bot.fetch_guild(guild_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return guild.name


async def build_auth_tier_embed(license_type: str) -> discord.Embed:
    if license_type not in LICENSE_TYPES:
        raise ValueError("Choose an ARC Go, ARC Pro, or ARC Pro Max version.")
    display_name = license_display_name(license_type)
    rows = authorization_rows(license_type)
    embed = discord.Embed(
        title=f"💎 {arc_product_label(_product)} — {display_name} Guild Authorizations",
        color=(discord.Color.gold() if license_type == "Gold"
               else discord.Color.blue() if license_type == "Diamond"
               else discord.Color.green()),
    )
    if not rows:
        embed.description = f"No guilds are assigned to the {display_name} version of {arc_product_label(_product)}."
        return embed
    visible = rows[:25]
    names = await asyncio.gather(*(_guild_name(row[0]) for row in visible))
    for (guild_id, expires_at, duration_days), name in zip(visible, names):
        safe = discord.utils.escape_mentions(discord.utils.escape_markdown(name or "Name unavailable"))
        embed.add_field(name=f"Guild: {safe} (`{guild_id}`)"[:256],
                        value=f"**Time left:** {authorization_time_left_text(expires_at, duration_days)}",
                        inline=False)
    if len(rows) > len(visible):
        embed.set_footer(text=f"{len(rows) - len(visible)} more guilds are not shown. Remove accepts a guild ID.")
    return embed


def parse_auth_duration(value: str) -> int:
    normalized = value.strip().casefold()
    if normalized in {"0", "unlimited", "illimité", "illimite"}:
        return 0
    try:
        days = int(normalized)
    except ValueError as error:
        raise ValueError("Duration must be 0–100,000 days or `unlimited`.") from error
    if days < 0 or days > 100_000:
        raise ValueError("Duration must be 0–100,000 days or `unlimited`.")
    return days


class AuthAdminPanelView(DurableView):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id, self.selected_tier, self.message = owner_id, None, None
        self.timeout_notice = "⏱️ This private authorization panel expired. Run `!auth` again to reopen it in your DMs."
        self.rebuild()

    async def user_is_authorized(self, user):
        if getattr(user, "id", None) != self.owner_id:
            return False
        return await _bot.is_owner(user)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This authorization panel belongs to another administrator.", ephemeral=True)
            return False
        if not await self.user_is_authorized(interaction.user):
            await interaction.response.send_message("You are no longer authorized to use this panel.", ephemeral=True)
            return False
        return True

    def _add_button(self, label, style, callback):
        button = discord.ui.Button(label=label, style=style)
        button.callback = callback
        self.add_item(button)

    def rebuild(self):
        self.clear_items()
        async def close(interaction):
            self.stop()
            await interaction.response.defer()
            await delete_message_or_clear(self.message or getattr(interaction, "message", None),
                                          log_context="closed authorization panel",
                                          fallback_content="Authorization panel closed.")
        if self.selected_tier is None:
            for tier in LICENSE_TYPES:
                async def select(interaction, selected_tier=tier):
                    await self.show_tier(interaction, selected_tier)
                self._add_button(license_display_name(tier), discord.ButtonStyle.primary, select)
            self._add_button("Close", discord.ButtonStyle.secondary, close)
            return
        tier = self.selected_tier
        async def add(interaction):
            if not await self.user_is_authorized(interaction.user):
                await interaction.response.send_message("Only the bot owner can add or change a guild authorization.", ephemeral=True)
                return
            await interaction.response.send_modal(AuthAddGuildModal(self, tier))
        async def remove(interaction):
            await interaction.response.send_modal(AuthRemoveGuildModal(self, tier))
        async def back(interaction):
            self.selected_tier = None
            self.rebuild()
            await interaction.response.edit_message(content="", embed=build_auth_panel_embed(), view=self)
        self._add_button("Add", discord.ButtonStyle.success, add)
        self._add_button("Remove", discord.ButtonStyle.danger, remove)
        self._add_button("Return", discord.ButtonStyle.secondary, back)
        self._add_button("Close", discord.ButtonStyle.secondary, close)

    async def show_tier(self, interaction, license_type):
        self.selected_tier = license_type
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(content="", embed=await build_auth_tier_embed(license_type),
                                                 view=self, allowed_mentions=discord.AllowedMentions.none())

    async def refresh_panel(self):
        if self.message is None or self.selected_tier is None:
            return
        try:
            await self.message.edit(content="", embed=await build_auth_tier_embed(self.selected_tier),
                                    view=self, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            _logger.exception("Could not refresh the authorization panel.")

    async def on_timeout(self):
        disable_view_items(self)
        await delete_message_or_clear(self.message, log_context="expired authorization panel",
                                      fallback_content=self.timeout_notice)


class AuthAddGuildModal(discord.ui.Modal):
    def __init__(self, panel, license_type):
        super().__init__(title=f"Add {arc_product_label(_product)} {license_display_name(license_type)}", timeout=300)
        self.panel, self.product, self.license_type = panel, _product, license_type
        self.guild_id_input = discord.ui.TextInput(label="Guild ID", placeholder="Enter the Discord server ID", max_length=20)
        self.duration_input = discord.ui.TextInput(label="Days or unlimited", placeholder="For example: 30 or unlimited", default="unlimited", max_length=24)
        self.add_item(self.guild_id_input); self.add_item(self.duration_input)

    async def on_submit(self, interaction):
        if not await self.panel.user_is_authorized(interaction.user):
            await interaction.response.send_message("Only the bot owner can add or change a guild authorization.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = int(str(self.guild_id_input.value).strip())
            days = parse_auth_duration(str(self.duration_input.value))
            already = guild_id in _repo().authorized_guild_ids
            _repo().authorize_guild(guild_id, days, license_type=self.license_type)
        except (TypeError, ValueError) as error:
            await interaction.followup.send(f"Could not authorize that guild: {error}", ephemeral=True); return
        except SlotStorageError:
            _logger.exception("Could not save guild authorization.")
            await interaction.followup.send("The authorization could not be saved. Please try again.", ephemeral=True); return
        await self.panel.refresh_panel()
        access = "with unlimited access" if days == 0 else f"for {days} day(s)"
        status = "updated" if already else "added"
        await interaction.followup.send(f"Guild `{guild_id}` authorization {status} for **{arc_product_label(self.product)} — {license_display_name(self.license_type)}** {access}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class AuthRemoveGuildModal(discord.ui.Modal):
    def __init__(self, panel, license_type):
        super().__init__(title=f"Remove {arc_product_label(_product)} {license_display_name(license_type)}", timeout=300)
        self.panel, self.product, self.license_type = panel, _product, license_type
        self.guild_id_input = discord.ui.TextInput(label="Guild ID", placeholder=f"Enter an {license_display_name(license_type)} guild ID", max_length=20)
        self.add_item(self.guild_id_input)

    async def on_submit(self, interaction):
        if not await self.panel.user_is_authorized(interaction.user):
            await interaction.response.send_message("You are no longer authorized to use this panel.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = int(str(self.guild_id_input.value).strip())
            if guild_id not in {row[0] for row in authorization_rows(self.license_type)}:
                await interaction.followup.send(f"Guild `{guild_id}` is not assigned to the **{license_display_name(self.license_type)}** version of **{arc_product_label(self.product)}**.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none()); return
            removed = _repo().revoke_guild(guild_id)
        except (TypeError, ValueError) as error:
            await interaction.followup.send(f"Could not remove that guild: {error}", ephemeral=True); return
        except SlotStorageError:
            _logger.exception("Could not save guild revocation.")
            await interaction.followup.send("The authorization could not be removed. Please try again.", ephemeral=True); return
        if not removed:
            await interaction.followup.send(f"Guild `{guild_id}` is no longer assigned to **{arc_product_label(self.product)}**.", ephemeral=True); return
        await self.panel.refresh_panel()
        await interaction.followup.send(f"Guild `{guild_id}` was removed from the **{arc_product_label(self.product)} — {license_display_name(self.license_type)}** version.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def install_authorization_commands(bot, repository, product, feedback, delete_command, logger, guild_name=None):
    configure(bot, repository, product, feedback, delete_command, logger, guild_name)
    bot.add_check(whitelist_check)

    @bot.group(name="admin", invoke_without_command=True, hidden=True)
    @commands.is_owner()
    async def admin_command(ctx):
        await _feedback(ctx, "Use `!admin add @User`, `!admin remove @User`, or `!admin list`.")

    @admin_command.command(name="add")
    @commands.is_owner()
    async def admin_add(ctx, user: discord.User):
        try:
            added = _repo().authorize_admin(user.id)
        except ValueError as error:
            await _feedback(ctx, str(error))
            return
        await _feedback(ctx, f"User `{user.id}` is now an authorized bot admin."
                        if added else f"User `{user.id}` is already an authorized bot admin.")

    @admin_command.command(name="remove")
    @commands.is_owner()
    async def admin_remove(ctx, user: discord.User):
        try:
            removed = _repo().revoke_admin(user.id)
        except ValueError as error:
            await _feedback(ctx, str(error))
            return
        await _feedback(ctx, f"User `{user.id}` has been removed from bot admins."
                        if removed else f"User `{user.id}` was not an authorized bot admin.")

    @admin_command.command(name="list")
    @commands.is_owner()
    async def admin_list(ctx):
        admin_ids = _repo().list_authorized_admin_ids()
        message = ("Authorized bot admins:\n" + "\n".join(f"• `{user_id}`" for user_id in admin_ids)
                   if admin_ids else "No authorized bot admins.")
        await _feedback(ctx, message)

    @bot.command(name="auth", hidden=True)
    @auth_admin_required()
    async def auth_command(ctx):
        view = AuthAdminPanelView(owner_id=ctx.author.id)
        try:
            view.message = await ctx.author.send(embed=build_auth_panel_embed(), view=view,
                                                 delete_after=330, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await _feedback(ctx, "I could not DM the authorization panel. Enable DMs from this server and run `!auth` again.", delete_after=30)
        finally:
            await _delete_command(ctx)
    return auth_command, admin_command, admin_add, admin_remove, admin_list
