"""Server-wide onboarding and staff configuration for the Discord bot."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord.ext import commands

from slot_storage import SlotStorageError


logger = logging.getLogger(__name__)

CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_~]+:\d+>")
UNICODE_EMOJI_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2}|"
    r"[\u2300-\u27BF\U0001F000-\U0001FAFF]"
    r"(?:[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200D[\u2300-\u27BF\U0001F000-\U0001FAFF]"
    r"(?:[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF])?)*"
    r"(?:\uFE0F|\u20E3)?)"
)


def extract_raw_emoji(content: str) -> str | None:
    """Return the first custom or Unicode emoji in a user response."""
    custom = CUSTOM_EMOJI_RE.search(content)
    if custom is not None:
        return custom.group(0)
    unicode_emoji = UNICODE_EMOJI_RE.search(content)
    return unicode_emoji.group(0) if unicode_emoji is not None else None


async def _delete_command_message(ctx: commands.Context) -> None:
    message = getattr(ctx, "message", None)
    delete = getattr(message, "delete", None)
    if delete is None:
        return
    try:
        await delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


GUIDE = (
    "**PUBG Mobile Scrim Manager — User Guide**\n\n"
    "**Server setup**\n"
    "1. The server owner runs `!set @Staff`.\n"
    "2. Staff members use `!setup` to configure individual scrims.\n"
    "3. Configure each scrim's Manager role separately in `!setup`.\n\n"
    "**Roles**\n"
    "• **Head Staff**: create, modify, and delete scrims, publish boards, and access logs.\n"
    "• **Staff**: daily slot operations with `!add`, `!open`, `!close`, `!confirm`, `!remove`, and `!reset`.\n"
    "• **Managers**: use the Confirm and Cancel buttons on their assigned public slot.\n\n"
    "**Scrim workflow**\n"
    "• Staff creates a scrim with `!setup`, chooses its first slot and "
    "number of slots (up to 25), and selects its public, staff, logs, and history channels.\n"
    "• The selected scrim's **✏️ Edit** panel can change its slot range later; "
    "a range cannot exclude an active team.\n"
    "• New scrims start closed. Staff runs `!open` in the public slots channel when registrations may begin.\n"
    "• Staff uses `!add Team / TAG / @Captain`; multiple lines can be "
    "confirmed together and receive the first available slots.\n"
    "• The manager receives access to the public channel and confirms or cancels their slot.\n"
    "• Staff validates pending requests from the private staff channel.\n"
    "• `!close` quietly removes the public controls; `!open` restores them.\n\n"
    "**Useful commands**\n"
    "`!set <@Role>` — define the global scrims Staff role (server owner only)\n"
    "`!setup` — create, publish, or delete scrims (configured Staff role only)\n"
    "`!slots [Scrim Name]` — publish or refresh a board (Head Staff or Staff)\n"
    "`!help` — show this command summary"
)


def _safe_role(role: object) -> bool:
    return (
        isinstance(role, discord.Role)
        and not role.is_default()
        and not role.managed
    )


async def _resolve_text_channel(
    selected: object, guild: discord.Guild
) -> discord.TextChannel | None:
    """Resolve a ChannelSelect value to the real guild channel object."""
    channel_id = getattr(selected, "id", None)
    if type(channel_id) is not int:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    if (
        not isinstance(channel, discord.TextChannel)
        or channel.guild is None
        or getattr(channel.guild, "id", None) != guild.id
    ):
        return None
    return channel


async def _send_setup_error(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def install_global_setup(bot, repository) -> None:
    """Install the global ``!set`` onboarding and editing flow."""

    def user_is_head_or_admin(user: object, guild_id: int) -> bool:
        permissions = getattr(user, "guild_permissions", None)
        if permissions is not None and getattr(permissions, "administrator", False):
            return True
        config = repository.get_server_config(guild_id)
        return bool(
            config
            and config.head_staff_role_id in {
                getattr(role, "id", None)
                for role in getattr(user, "roles", ())
            }
        )

    def global_content(config) -> str:
        return (
            "**Global server configuration**\n"
            f"Head Staff role: <@&{config.head_staff_role_id}>\n"
            f"Staff role: <@&{config.staff_role_id}>\n"
            f"Private logs channel: <#{config.logs_channel_id}>\n\n"
            "Use **✏️ Edit** to change one setting without repeating the full setup."
        )

    def global_configuration_details(config) -> str:
        return (
            f"Head Staff role: <@&{config.head_staff_role_id}>\n"
            f"Staff role: <@&{config.staff_role_id}>\n"
            f"Private logs channel: <#{config.logs_channel_id}>"
        )

    async def write_global_log(
        guild: discord.Guild, action: str, details: str
    ) -> None:
        config = repository.get_server_config(guild.id)
        if config is None:
            return
        channel = await _resolve_text_channel(
            guild.get_channel(config.logs_channel_id), guild
        )
        if channel is None:
            return
        try:
            await channel.send(
                f"**{action}**\n{details}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not write global configuration log")

    async def save_global_change(
        interaction: discord.Interaction,
        *,
        head_staff_role_id: int | None = None,
        staff_role_id: int | None = None,
        logs_channel_id: int | None = None,
    ) -> None:
        current = repository.get_server_config(interaction.guild.id)
        if current is None:
            await interaction.edit_original_response(
                content="Global configuration no longer exists.", view=None
            )
            return
        next_head = (
            current.head_staff_role_id
            if head_staff_role_id is None
            else head_staff_role_id
        )
        next_staff = (
            current.staff_role_id if staff_role_id is None else staff_role_id
        )
        next_logs = (
            current.logs_channel_id
            if logs_channel_id is None
            else logs_channel_id
        )
        try:
            config = repository.save_server_config(
                interaction.guild.id,
                head_staff_role_id=next_head,
                staff_role_id=next_staff,
                logs_channel_id=next_logs,
            )
        except (SlotStorageError, ValueError) as error:
            await interaction.edit_original_response(
                content=f"The change could not be saved. {error}",
                view=GlobalEditView(interaction.user.id, interaction.guild.id),
            )
            return

        changed = []
        if next_head != current.head_staff_role_id:
            changed.append(
                f"Head Staff role: <@&{current.head_staff_role_id}> → "
                f"<@&{next_head}>"
            )
        if next_staff != current.staff_role_id:
            changed.append(
                f"Staff role: <@&{current.staff_role_id}> → <@&{next_staff}>"
            )
        if next_logs != current.logs_channel_id:
            changed.append(
                f"Private logs channel: <#{current.logs_channel_id}> → "
                f"<#{next_logs}>"
            )
        await write_global_log(
            interaction.guild,
            "GLOBAL CONFIGURATION UPDATED",
            "Changed:\n" + "\n".join(changed) if changed else "No fields changed.",
        )
        await interaction.edit_original_response(
            content=global_content(config),
            view=GlobalConfigurationView(interaction.user.id, interaction.guild.id),
        )

    class GlobalBoundView(discord.ui.View):
        def __init__(
            self, owner_id: int, guild_id: int, *, timeout: float = 900
        ):
            super().__init__(timeout=timeout)
            self.owner_id = owner_id
            self.guild_id = guild_id

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            user = interaction.user
            allowed = bool(
                interaction.guild is not None
                and interaction.guild.id == self.guild_id
                and repository.is_guild_authorized(self.guild_id)
                and user.id == self.owner_id
                and user_is_head_or_admin(user, self.guild_id)
            )
            if not allowed:
                await _send_setup_error(
                    interaction,
                    "Only the administrator or Head Staff member who opened this "
                    "configuration can use it.",
                )
            return allowed

        async def on_error(
            self,
            interaction: discord.Interaction,
            error: Exception,
            item: discord.ui.Item[Any],
        ) -> None:
            logger.exception("Global setup interaction failed", exc_info=error)
            await _send_setup_error(
                interaction,
                "The global configuration could not be saved. Nothing was changed.",
            )

    class HeadStaffRoleView(GlobalBoundView):
        def __init__(self, owner_id: int, guild_id: int):
            super().__init__(owner_id, guild_id)
            picker = discord.ui.RoleSelect(
                placeholder="Select the Head Staff role",
                min_values=1,
                max_values=1,
            )

            async def callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                role = picker.values[0]
                if not _safe_role(role):
                    await _send_setup_error(
                        interaction,
                        "Select a regular server role, not @everyone or a managed role.",
                    )
                    return
                await interaction.response.edit_message(
                    content=(
                        f"Head Staff role selected: {role.mention}\n"
                        "Now select the daily Staff role."
                    ),
                    view=StaffRoleView(
                        self.owner_id,
                        self.guild_id,
                        role.id,
                    ),
                )

            picker.callback = callback
            self.add_item(picker)

    class StaffRoleView(GlobalBoundView):
        def __init__(
            self,
            owner_id: int,
            guild_id: int,
            head_staff_role_id: int,
        ):
            super().__init__(owner_id, guild_id)
            self.head_staff_role_id = head_staff_role_id
            picker = discord.ui.RoleSelect(
                placeholder="Select the daily Staff role",
                min_values=1,
                max_values=1,
            )

            async def callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                role = picker.values[0]
                if not _safe_role(role):
                    await _send_setup_error(
                        interaction,
                        "Select a regular server role, not @everyone or a managed role.",
                    )
                    return
                if role.id == self.head_staff_role_id:
                    await _send_setup_error(
                        interaction,
                        "Head Staff and Staff must be two different roles.",
                    )
                    return
                await interaction.response.edit_message(
                    content=(
                        f"Staff role selected: {role.mention}\n"
                        "Now select the private logs channel."
                    ),
                    view=LogsChannelView(
                        self.owner_id,
                        self.guild_id,
                        self.head_staff_role_id,
                        role.id,
                    ),
                )

            picker.callback = callback
            self.add_item(picker)

    class LogsChannelView(GlobalBoundView):
        def __init__(
            self,
            owner_id: int,
            guild_id: int,
            head_staff_role_id: int,
            staff_role_id: int,
        ):
            super().__init__(owner_id, guild_id)
            self.head_staff_role_id = head_staff_role_id
            self.staff_role_id = staff_role_id
            picker = discord.ui.ChannelSelect(
                placeholder="Select the private logs channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )

            async def callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                guild = interaction.guild
                channel = await _resolve_text_channel(picker.values[0], guild)
                if channel is None:
                    await _send_setup_error(
                        interaction,
                        "Select an existing text channel in this server.",
                    )
                    return
                bot_member = guild.me
                permissions = channel.permissions_for(bot_member) if bot_member else None
                missing = [
                    label
                    for attribute, label in (
                        ("view_channel", "View Channel"),
                        ("send_messages", "Send Messages"),
                        ("read_message_history", "Read Message History"),
                        ("manage_channels", "Manage Channels"),
                    )
                    if permissions is None or not getattr(permissions, attribute, False)
                ]
                if missing:
                    await _send_setup_error(
                        interaction,
                        f"I need {', '.join(missing)} in {channel.mention} "
                        "to protect and write the logs channel.",
                    )
                    return
                try:
                    await channel.set_permissions(
                        guild.default_role,
                        view_channel=False,
                        read_message_history=False,
                        send_messages=False,
                        reason="Protect scrim logs channel",
                    )
                    await channel.set_permissions(
                        guild.owner,
                        view_channel=True,
                        read_message_history=True,
                        send_messages=False,
                        reason="Allow server owner to view scrim logs",
                    )
                    head_role = guild.get_role(self.head_staff_role_id)
                    if head_role is None:
                        raise ValueError("The selected Head Staff role no longer exists.")
                    await channel.set_permissions(
                        head_role,
                        view_channel=True,
                        read_message_history=True,
                        send_messages=False,
                        reason="Allow Head Staff to view scrim logs",
                    )
                    if bot_member is not None:
                        await channel.set_permissions(
                            bot_member,
                            view_channel=True,
                            read_message_history=True,
                            send_messages=True,
                            reason="Allow the bot to write scrim logs",
                        )
                    config = repository.save_server_config(
                        guild.id,
                        head_staff_role_id=self.head_staff_role_id,
                        staff_role_id=self.staff_role_id,
                        logs_channel_id=channel.id,
                    )
                    await write_global_log(
                        guild,
                        "GLOBAL CONFIGURATION CREATED",
                        global_configuration_details(config),
                    )
                except (discord.HTTPException, SlotStorageError, ValueError) as error:
                    logger.exception("Could not save global server configuration")
                    await _send_setup_error(
                        interaction,
                        "The global configuration could not be saved. "
                        "Check the channel permissions and try again.",
                    )
                    return

                await interaction.response.edit_message(
                    content=(
                        "✅ **Global configuration saved.**\n"
                        f"Head Staff: <@&{config.head_staff_role_id}>\n"
                        f"Staff: <@&{config.staff_role_id}>\n"
                        f"Private logs: <#{config.logs_channel_id}>\n\n"
                        "Choose what to do next:"
                    ),
                    view=GlobalCompletionView(
                        self.owner_id,
                        self.guild_id,
                    ),
                )

            picker.callback = callback
            self.add_item(picker)

    class GlobalConfigurationView(GlobalBoundView):
        def __init__(self, owner_id: int, guild_id: int):
            super().__init__(owner_id, guild_id)
            edit = discord.ui.Button(
                label="Edit", emoji="✏️", style=discord.ButtonStyle.primary
            )
            guide = discord.ui.Button(
                label="Read the user guide",
                emoji="📖",
                style=discord.ButtonStyle.secondary,
            )

            async def edit_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                config = repository.get_server_config(self.guild_id)
                if config is None:
                    await interaction.response.edit_message(
                        content="Global configuration no longer exists.", view=None
                    )
                    return
                await interaction.response.send_message(
                    global_content(config),
                    view=GlobalEditView(self.owner_id, self.guild_id),
                    ephemeral=True,
                )

            async def guide_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.send_message(GUIDE, ephemeral=True)

            edit.callback = edit_callback
            guide.callback = guide_callback
            self.add_item(edit)
            self.add_item(guide)

    class GlobalEditView(GlobalBoundView):
        def __init__(self, owner_id: int, guild_id: int):
            super().__init__(owner_id, guild_id, timeout=600)

            head = discord.ui.Button(
                label="Head Staff role", emoji="🎭",
                style=discord.ButtonStyle.secondary, row=0
            )
            staff = discord.ui.Button(
                label="Staff role", emoji="👥",
                style=discord.ButtonStyle.secondary, row=0
            )
            logs = discord.ui.Button(
                label="Logs channel", emoji="📝",
                style=discord.ButtonStyle.secondary, row=1
            )
            close = discord.ui.Button(
                label="Close", style=discord.ButtonStyle.danger, row=2
            )

            async def role_callback(
                interaction: discord.Interaction, field: str, label: str
            ) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.send_message(
                        f"Select the {label} role.",
                        view=GlobalRolePicker(self, field),
                        ephemeral=True,
                    )

            async def logs_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.send_message(
                        "Select the private logs channel.",
                        view=GlobalLogsPicker(self),
                        ephemeral=True,
                    )

            async def close_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.edit_message(
                        content="Global configuration editing closed.", view=None
                    )

            head.callback = lambda interaction: role_callback(
                interaction, "head_staff_role_id", "Head Staff"
            )
            staff.callback = lambda interaction: role_callback(
                interaction, "staff_role_id", "Staff"
            )
            logs.callback = logs_callback
            close.callback = close_callback
            for item in (head, staff, logs, close):
                self.add_item(item)

    class GlobalRolePicker(GlobalBoundView):
        def __init__(self, edit_view: GlobalEditView, field: str):
            super().__init__(
                edit_view.owner_id, edit_view.guild_id, timeout=300
            )
            self.edit_view = edit_view
            self.field = field
            picker = discord.ui.RoleSelect(
                placeholder="Select a server role",
                min_values=1,
                max_values=1,
            )

            async def callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                role = picker.values[0]
                if not _safe_role(role):
                    await _send_setup_error(
                        interaction,
                        "Select a regular server role, not @everyone or a managed role.",
                    )
                    return
                config = repository.get_server_config(self.guild_id)
                if config is None:
                    await _send_setup_error(
                        interaction, "Global configuration no longer exists."
                    )
                    return
                if (
                    self.field == "head_staff_role_id"
                    and role.id == config.staff_role_id
                ) or (
                    self.field == "staff_role_id"
                    and role.id == config.head_staff_role_id
                ):
                    await _send_setup_error(
                        interaction,
                        "Head Staff and Staff must be two different roles.",
                    )
                    return
                await interaction.response.defer()
                await save_global_change(
                    interaction, **{self.field: role.id}
                )

            picker.callback = callback
            self.add_item(picker)

    class GlobalLogsPicker(GlobalBoundView):
        def __init__(self, edit_view: GlobalEditView):
            super().__init__(
                edit_view.owner_id, edit_view.guild_id, timeout=300
            )
            picker = discord.ui.ChannelSelect(
                placeholder="Select the private logs channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )

            async def callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                channel = await _resolve_text_channel(
                    picker.values[0], interaction.guild
                )
                if channel is None:
                    await _send_setup_error(
                        interaction,
                        "Select an existing text channel in this server.",
                    )
                    return
                await interaction.response.defer()
                await save_global_change(
                    interaction, logs_channel_id=channel.id
                )

            picker.callback = callback
            self.add_item(picker)

    class GlobalCompletionView(GlobalBoundView):
        def __init__(self, owner_id: int, guild_id: int):
            super().__init__(owner_id, guild_id)
            create = discord.ui.Button(
                label="Create my first event / scrim",
                emoji="🏆",
                style=discord.ButtonStyle.success,
            )
            guide = discord.ui.Button(
                label="Read the user guide",
                emoji="📖",
                style=discord.ButtonStyle.secondary,
            )
            edit = discord.ui.Button(
                label="Edit configuration",
                emoji="✏️",
                style=discord.ButtonStyle.secondary,
            )

            async def create_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                wizard = getattr(bot, "start_scrim_wizard", None)
                if wizard is None:
                    await _send_setup_error(
                        interaction,
                        "The scrim wizard is unavailable. Please run `!setup`.",
                    )
                    return
                await wizard(interaction)

            async def guide_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                await interaction.response.send_message(GUIDE, ephemeral=True)

            async def edit_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                config = repository.get_server_config(self.guild_id)
                if config is None:
                    await _send_setup_error(
                        interaction, "Global configuration no longer exists."
                    )
                    return
                await interaction.response.edit_message(
                    content=global_content(config),
                    view=GlobalConfigurationView(self.owner_id, self.guild_id),
                )

            create.callback = create_callback
            guide.callback = guide_callback
            edit.callback = edit_callback
            self.add_item(create)
            self.add_item(guide)
            self.add_item(edit)

    @bot.command(name="set")
    @commands.guild_only()
    async def set_command(
        ctx: commands.Context[commands.Bot], role: discord.Role
    ) -> None:
        owner = getattr(ctx.guild, "owner", None)
        owner_id = getattr(ctx.guild, "owner_id", None)
        is_owner = (
            (owner is not None and owner.id == ctx.author.id)
            or owner_id == ctx.author.id
        )
        if not is_owner:
            await ctx.send(
                "Only the server owner can use `!set`.",
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await _delete_command_message(ctx)
            return
        if not _safe_role(role) or role.guild.id != ctx.guild.id:
            await ctx.send(
                "Select a regular role from this server, not @everyone or a managed role.",
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await _delete_command_message(ctx)
            return
        try:
            config = repository.save_scrims_staff_role(ctx.guild.id, role.id)
        except (SlotStorageError, ValueError):
            logger.exception("Could not save the global scrims Staff role.")
            await ctx.send(
                "The scrims Staff role could not be saved. Nothing was changed.",
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await _delete_command_message(ctx)
            return
        await ctx.send(
            f"Scrims Staff role configured as {role.mention}. "
            "Only members with this role can use `!setup`, `!idpw`, `!reset`, and `!remind`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await _delete_command_message(ctx)