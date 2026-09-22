"""Private, staff-bound management UI for configured scrims."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from global_setup import extract_raw_emoji
from slot_storage import SlotStorageError
from scrim_state import (
    DEFAULT_IDPW_TIMEZONE,
    DEFAULT_MATCH_MAPS,
    DEFAULT_SLOT_END,
    DEFAULT_SLOT_START,
    IDPW_TIMEZONE_CHOICES,
    MAX_MATCHES,
    PASSWORD_TYPES,
    normalize_map_pool,
    normalize_match_configuration,
    validate_slot_range,
)


logger = logging.getLogger(__name__)


async def _delete_command_message(ctx: commands.Context) -> None:
    message = getattr(ctx, "message", None)
    delete = getattr(message, "delete", None)
    if delete is None:
        return
    try:
        await delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def _safe_name(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def _short_name(value: str, limit: int = 20) -> str:
    return (
        _safe_name(value).replace("\r", " ").replace("\n", " ")[:limit].rstrip("\\")
    )


def _channel_ref(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "not configured"


def _role_ref(role_id: int | None) -> str:
    return f"<@&{role_id}>" if role_id else "not configured"


def _scrim_configuration_details(scrim, repository) -> str:
    idpw = repository.get_idpw_config(scrim.id)
    timezone_name = getattr(scrim, "timezone", DEFAULT_IDPW_TIMEZONE)
    password_mode = getattr(scrim, "pw_type", "fixed").title()
    maps = getattr(scrim, "maps", list(DEFAULT_MATCH_MAPS))
    max_matches = getattr(scrim, "max_matches", len(maps))
    current_match = getattr(scrim, "current_match_counter", 1)
    map_details = (
        f"**{max_matches} Matches Configured** ({', '.join(maps)})"
        if maps
        else f"**{max_matches} Matches Configured** (custom maps not configured)"
    )
    return (
        f"Name: **{_safe_name(scrim.name)}**\n"
        f"Public channel: {_channel_ref(scrim.public_channel_id)}\n"
        f"Staff channel: {_channel_ref(scrim.staff_channel_id)}\n"
        f"Captain management channel: {_channel_ref(scrim.cap_channel_id)}\n"
        f"Logs channel: {_channel_ref(scrim.logs_channel_id)}\n"
        f"History channel: {_channel_ref(scrim.history_channel_id)}\n"
        f"Staff role: {_role_ref(scrim.staff_role_id)}\n"
        f"**Pending Captain Role:** {_role_ref(scrim.pending_role_id)}\n"
        f"**Confirmed Captain Role:** {_role_ref(scrim.confirmed_role_id)}\n"
        f"Slots: {scrim.slot_start:02d}–{scrim.slot_end:02d}\n"
        f"Password mode: **{password_mode}**\n"
        f"Timezone: `{timezone_name}`\n"
        f"Map rotation: {map_details}\n"
        f"Current match: **{current_match}** / {max_matches}\n"
        f"ID/PW target: "
        f"{_channel_ref(idpw.target_channel_id) if idpw else 'not configured'}"
    )


def _slot_range_from_count(start_value: object, count_value: object) -> tuple[int, int]:
    try:
        start = int(str(start_value).strip())
        count = int(str(count_value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError(
            "The first slot and number of slots must be whole numbers."
        ) from error
    if count < 1:
        raise ValueError("The number of slots must be at least 1.")
    end = start + count - 1
    validate_slot_range(start, end)
    return start, end


def _match_configuration_from_values(
    count_value: object, maps_value: object
) -> tuple[int, list[str]]:
    try:
        count = int(str(count_value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError("The number of games must be a whole number.") from error
    maps = [
        item.strip()
        for item in str(maps_value).split(",")
        if item.strip()
    ]
    if not maps:
        if not 1 <= count <= MAX_MATCHES:
            raise ValueError(
                f"The number of games must be between 1 and {MAX_MATCHES}."
            )
        return count, []
    try:
        return count, normalize_map_pool(maps)
    except ValueError as error:
        raise ValueError(
            "Enter unique map names separated by commas."
        ) from error


def _interaction_is_authorized(
    interaction: discord.Interaction, owner_id: int, guild_id: int
) -> bool:
    guild = interaction.guild
    user = interaction.user
    permissions = getattr(user, "guild_permissions", None)
    return (
        guild is not None
        and guild.id == guild_id
        and user.id == owner_id
        and permissions is not None
        and getattr(permissions, "administrator", False)
    )


async def _deny(interaction: discord.Interaction) -> None:
    text = (
        "This setup control belongs to the staff member who opened it. "
        "Run `!setup` yourself if you have the configured Staff role."
    )
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _send_error(interaction: discord.Interaction, error: Exception) -> None:
    logger.exception("Setup interaction failed", exc_info=error)
    if isinstance(error, SlotStorageError):
        text = "The change could not be saved. Nothing was changed. Please try again."
    else:
        text = "Something went wrong. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Could not report a setup interaction error")


def _channel_permissions_error(
    guild: discord.Guild,
    public_channel: discord.TextChannel,
    staff_channel: discord.TextChannel,
    *extra_channels: tuple[discord.TextChannel, str],
) -> str | None:
    member = guild.me
    if member is None:
        return "I could not determine my server permissions. Please try again."

    required = ("view_channel", "send_messages", "read_message_history")
    for channel, label in (
        (public_channel, "public"),
        (staff_channel, "staff"),
        *extra_channels,
    ):
        permissions = channel.permissions_for(member)
        missing = [
            permission.replace("_", " ").title()
            for permission in required
            if not getattr(permissions, permission, False)
        ]
        if missing:
            return (
                f"I am missing {', '.join(missing)} in the {label} channel "
                f"{channel.mention}."
            )

    if not public_channel.permissions_for(member).manage_messages:
        return (
            f"I need Manage Messages in the public channel {public_channel.mention} "
            "so processed `!add` messages can be deleted."
        )
    return None


def install_setup(bot, repository, publish_scrim, log_action=None) -> None:
    """Install the guild-only ``!setup`` command on *bot*."""

    def setup_authorized(
        interaction: discord.Interaction, owner_id: int, guild_id: int
    ) -> bool:
        if (
            interaction.guild is None
            or interaction.guild.id != guild_id
            or interaction.user.id != owner_id
            or not repository.is_guild_authorized(guild_id)
        ):
            return False
        config = repository.get_server_config(guild_id)
        return bool(
            config
            and config.staff_role_id is not None
            and config.staff_role_id in {
                getattr(role, "id", None)
                for role in getattr(interaction.user, "roles", ())
            }
        )

    def user_is_staff(user: object, guild_id: int) -> bool:
        config = repository.get_server_config(guild_id)
        return bool(
            config
            and config.staff_role_id is not None
            and config.staff_role_id in {
                getattr(role, "id", None) for role in getattr(user, "roles", ())
            }
        )

    async def retire_message(
        guild: discord.Guild,
        channel_id: int | None,
        message_id: int | None,
        runtime_message: object | None,
    ) -> None:
        if channel_id is None or message_id is None:
            return
        message = runtime_message
        try:
            if message is None:
                channel = guild.get_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    message = await channel.fetch_message(message_id)
            if message is not None:
                await message.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Could not retire an old scrim board message")

    async def apply_scrim_change(
        interaction: discord.Interaction,
        panel: "SetupPanel",
        scrim_id: str,
        *,
        changes: dict[str, object],
        response_mode: str = "deferred",
        notice: str | None = None,
        ephemeral_notice: str | None = None,
    ) -> None:
        if response_mode == "modal":
            await interaction.response.defer(ephemeral=True)
        scrim = repository.get(scrim_id)
        if scrim is None or scrim.guild_id != panel.guild_id:
            text = "That scrim no longer exists."
            if response_mode == "modal":
                await interaction.edit_original_response(content=text, view=None)
            else:
                await interaction.edit_original_response(content=text, view=None)
            await panel.refresh_message()
            return

        old = {
            "name": scrim.name,
            "public_channel_id": scrim.public_channel_id,
            "staff_channel_id": scrim.staff_channel_id,
            "staff_role_id": scrim.staff_role_id,
            "pending_role_id": scrim.pending_role_id,
            "confirmed_role_id": scrim.confirmed_role_id,
            "cap_channel_id": scrim.cap_channel_id,
            "logs_channel_id": scrim.logs_channel_id,
            "history_channel_id": scrim.history_channel_id,
            "slot_start": scrim.slot_start,
            "slot_end": scrim.slot_end,
            "max_matches": scrim.max_matches,
            "maps": list(scrim.maps),
            "match_maps": list(getattr(scrim, "match_maps", scrim.maps)),
            "public_message_id": scrim.public_message_id,
            "staff_message_id": scrim.staff_message_id,
            "runtime_message": scrim.runtime_message,
            "runtime_staff_message": scrim.runtime_staff_message,
        }
        values = {
            "name": scrim.name,
            "public_channel_id": scrim.public_channel_id,
            "staff_channel_id": scrim.staff_channel_id,
            "staff_role_id": scrim.staff_role_id,
            "pending_role_id": scrim.pending_role_id,
            "confirmed_role_id": scrim.confirmed_role_id,
            "cap_channel_id": scrim.cap_channel_id,
            "logs_channel_id": scrim.logs_channel_id,
            "history_channel_id": scrim.history_channel_id,
            "slot_start": scrim.slot_start,
            "slot_end": scrim.slot_end,
            "max_matches": scrim.max_matches,
            "maps": list(scrim.maps),
            "match_maps": list(getattr(scrim, "match_maps", scrim.maps)),
        }
        values.update(changes)
        try:
            updated = repository.update_scrim(
                scrim_id,
                panel.guild_id,
                **values,
            )
        except (SlotStorageError, ValueError) as error:
            text = (
                "The change could not be saved. "
                f"{error}"
            )
            if response_mode == "modal":
                await interaction.edit_original_response(
                    content=text,
                    view=ScrimEditView(panel, panel.owner_id, panel.guild_id, scrim_id),
                )
            else:
                await interaction.edit_original_response(
                    content=text,
                    view=ScrimEditView(panel, panel.owner_id, panel.guild_id, scrim_id),
                )
            return

        public_changed = old["public_channel_id"] != updated.public_channel_id
        staff_changed = old["staff_channel_id"] != updated.staff_channel_id
        if public_changed or staff_changed:
            with repository.transaction():
                if public_changed:
                    updated.public_message_id = None
                    updated.runtime_message = None
                if staff_changed:
                    updated.staff_message_id = None
                    updated.runtime_staff_message = None

        if public_changed:
            await retire_message(
                interaction.guild,
                old["public_channel_id"],
                old["public_message_id"],
                old["runtime_message"],
            )
        if staff_changed:
            await retire_message(
                interaction.guild,
                old["staff_channel_id"],
                old["staff_message_id"],
                old["runtime_staff_message"],
            )

        board_refreshed = True
        if (
            old["name"] != updated.name
            or public_changed
            or staff_changed
            or old["slot_start"] != updated.slot_start
            or old["slot_end"] != updated.slot_end
        ):
            try:
                board_refreshed = await publish_scrim(updated)
            except Exception as error:
                logger.exception("Could not refresh the edited scrim", exc_info=error)
                board_refreshed = False

        await panel.refresh_message()
        def format_value(field: str, value: object) -> str:
            if field.endswith("channel_id"):
                return f"<#{value}>"
            if field.endswith("role_id"):
                return f"<@&{value}>"
            return _safe_name(str(value))

        changed_fields = ", ".join(changes)
        details = "Changed: " + ", ".join(
            f"{field}: {format_value(field, old[field])} → "
            f"{format_value(field, changes[field])}"
            for field in changes
        ) + "."
        if not board_refreshed:
            details += " The slot boards could not be refreshed."
        if log_action is not None:
            try:
                await log_action(updated, "SCRIM CONFIGURATION UPDATED", details)
            except Exception:
                logger.exception("Could not write scrim configuration log")

        text = notice or (
            f"✅ **{_safe_name(updated.name)}** was updated.\n"
            f"Changed: {changed_fields}."
        )
        if not board_refreshed:
            text += " The saved configuration is active, but the slot boards need a refresh."
        edit_view = ScrimEditView(
            panel, panel.owner_id, panel.guild_id, scrim_id
        )
        if response_mode == "modal":
            await interaction.edit_original_response(
                content=text,
                view=edit_view,
            )
        else:
            await interaction.edit_original_response(
                content=text,
                view=edit_view,
            )
        if ephemeral_notice:
            await interaction.followup.send(ephemeral_notice, ephemeral=True)

    class BoundView(discord.ui.View):
        def __init__(self, owner_id: int, guild_id: int, *, timeout: float = 900):
            super().__init__(timeout=timeout)
            self.owner_id = owner_id
            self.guild_id = guild_id

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not setup_authorized(
                interaction, self.owner_id, self.guild_id
            ):
                await _deny(interaction)
                return False
            return True

        async def on_error(
            self,
            interaction: discord.Interaction,
            error: Exception,
            item: discord.ui.Item[Any],
        ) -> None:
            await _send_error(interaction, error)

    class SetupPanel(BoundView):
        def __init__(self, owner_id: int, guild_id: int):
            super().__init__(owner_id, guild_id)
            self.selected_id: str | None = None
            self.message: discord.Message | None = None
            self.scrims: list[Any] = []
            self.rebuild()

        def load_scrims(self) -> list[Any]:
            self.scrims = list(repository.list(self.guild_id))
            return self.scrims

        def content(self) -> str:
            if not self.scrims:
                return (
                    "**Global Scrim Dashboard**\n"
                    "No scrims are configured for this server. Select "
                    "**Add Scrim** to configure one using existing text channels."
                )
            return (
                "**Global Scrim Dashboard**\n"
                "Use **Manage Scrims** to select a scrim and update its "
                "configuration or operational settings."
            )

        def embed(self) -> discord.Embed:
            embed = discord.Embed(
                title="Global Scrim Dashboard",
                description=(
                    "\n".join(
                        f"• **{_short_name(scrim.name)}** — "
                        f"{'🟢 OPEN' if scrim.is_open else '🔴 CLOSED'}"
                        for scrim in self.scrims
                    )
                    if self.scrims
                    else (
                        "No scrims are configured yet. Select **Add Scrim** "
                        "to begin."
                    )
                ),
                color=discord.Color.blurple(),
            )
            return embed

        def rebuild(self) -> None:
            self.clear_items()
            self.load_scrims()
            add_button = discord.ui.Button(
                label="Add Scrim",
                emoji="🟩",
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def add_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                await interaction.response.send_modal(
                    ScrimNameModal(self, self.owner_id, self.guild_id)
                )

            add_button.callback = add_callback
            self.add_item(add_button)

            manage_button = discord.ui.Button(
                label="Manage Scrims",
                emoji="🟦",
                style=discord.ButtonStyle.primary,
                disabled=not self.scrims,
                row=0,
            )

            async def manage_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                selector = ScrimSelectorView(
                    self, self.owner_id, self.guild_id
                )
                if not selector.scrims:
                    self.rebuild()
                    await interaction.response.edit_message(
                        content=self.content(), embed=self.embed(), view=self
                    )
                    return
                await interaction.response.edit_message(
                    content=selector.content(),
                    embed=selector.embed(),
                    view=selector,
                )

            manage_button.callback = manage_callback
            self.add_item(manage_button)

        async def refresh_message(self) -> bool:
            try:
                self.rebuild()
            except SlotStorageError:
                logger.exception("Could not reload scrims for the setup panel")
                return False
            if self.message is None:
                return False
            try:
                await self.message.edit(
                    content=self.content(),
                    embed=self.embed(),
                    view=self,
                )
                return True
            except discord.HTTPException:
                logger.exception("Could not refresh setup panel")
                return False

    class ScrimSelectorView(BoundView):
        """The second setup state: choose which scrim to manage."""

        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.scrims: list[Any] = []
            self.rebuild()

        def rebuild(self) -> None:
            self.clear_items()
            self.scrims = list(repository.list(self.guild_id))
            if self.scrims:
                selector = discord.ui.Select(
                    placeholder="Choose a configured scrim",
                    min_values=1,
                    max_values=1,
                    options=[
                        discord.SelectOption(
                            label=scrim.name[:100],
                            value=scrim.id,
                            description=(
                                f"{'OPEN' if scrim.is_open else 'CLOSED'} · "
                                f"Slots {scrim.slot_start:02d}–{scrim.slot_end:02d}"
                            )[:100],
                        )
                        for scrim in self.scrims[:25]
                    ],
                    row=0,
                )

                async def select_callback(interaction: discord.Interaction) -> None:
                    if not await self.interaction_check(interaction):
                        return
                    # Acknowledge immediately. Rebuilding the view before the
                    # acknowledgement can make Discord reject the interaction.
                    await interaction.response.defer()
                    try:
                        selected = repository.get(selector.values[0])
                        if selected is None or selected.guild_id != self.guild_id:
                            self.rebuild()
                            await interaction.edit_original_response(
                                content=(
                                    "That scrim no longer exists.\n\n"
                                    + self.content()
                                ),
                                embed=self.embed(),
                                view=self,
                            )
                            return
                        self.panel.selected_id = selected.id
                        management = ScrimManagementView(
                            self.panel,
                            self.owner_id,
                            self.guild_id,
                            selected.id,
                        )
                        await interaction.edit_original_response(
                            content=management.content(),
                            embed=management.embed(),
                            view=management,
                        )
                    except Exception as error:
                        await _send_error(interaction, error)

                selector.callback = select_callback
                self.add_item(selector)

            back_button = discord.ui.Button(
                label="Back to Dashboard",
                emoji="↩️",
                style=discord.ButtonStyle.secondary,
                row=1,
            )

            async def back_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                self.panel.selected_id = None
                self.panel.rebuild()
                await interaction.response.edit_message(
                    content=self.panel.content(),
                    embed=self.panel.embed(),
                    view=self.panel,
                )

            back_button.callback = back_callback
            self.add_item(back_button)

        def content(self) -> str:
            return (
                "**Manage Scrims**\n"
                "Select a scrim from the dropdown to open its management menu."
            )

        def embed(self) -> discord.Embed:
            return discord.Embed(
                title="Select a Scrim to Manage",
                description=(
                    "Choose a configured scrim from the dropdown below."
                    if self.scrims
                    else "No scrims are configured. Return to the dashboard to add one."
                ),
                color=discord.Color.blurple(),
            )

    class ScrimManagementView(BoundView):
        """The selected-scrim state with only operational management actions."""

        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            scrim_id: str,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.scrim_id = scrim_id
            self.rebuild()

        def scrim(self) -> Any | None:
            selected = repository.get(self.scrim_id)
            if selected is None or selected.guild_id != self.guild_id:
                return None
            return selected

        def rebuild(self) -> None:
            self.clear_items()

            edit_button = discord.ui.Button(
                label="Edit Configuration",
                emoji="✏️",
                style=discord.ButtonStyle.primary,
                row=0,
            )

            async def edit_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                selected = self.scrim()
                if selected is None:
                    await interaction.response.edit_message(
                        content="That scrim no longer exists.",
                        embed=None,
                        view=None,
                    )
                    return
                edit_view = ScrimEditView(
                    self.panel, self.owner_id, self.guild_id, self.scrim_id
                )
                await interaction.response.edit_message(
                    content=edit_view.content(),
                    view=edit_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            edit_button.callback = edit_callback
            self.add_item(edit_button)

            delete_button = discord.ui.Button(
                label="Delete Scrim",
                emoji="🟥",
                style=discord.ButtonStyle.danger,
                row=0,
            )

            async def delete_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                selected = self.scrim()
                if selected is None:
                    await interaction.response.edit_message(
                        content="That scrim no longer exists.",
                        embed=None,
                        view=None,
                    )
                    return
                await interaction.response.edit_message(
                    content=(
                        f"Delete **{_safe_name(selected.name)}**? This permanently "
                        "removes its saved slot data, but does not delete either "
                        "Discord channel."
                    ),
                    embed=None,
                    view=DeleteConfirmation(
                        self.panel, self.owner_id, self.guild_id, self.scrim_id
                    ),
                )

            delete_button.callback = delete_callback
            self.add_item(delete_button)

            back_button = discord.ui.Button(
                label="Back to Selector",
                emoji="↩️",
                style=discord.ButtonStyle.secondary,
                row=1,
            )

            async def back_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                selector = ScrimSelectorView(
                    self.panel, self.owner_id, self.guild_id
                )
                await interaction.response.edit_message(
                    content=selector.content(),
                    embed=selector.embed(),
                    view=selector,
                )

            back_button.callback = back_callback
            self.add_item(back_button)

        def content(self) -> str:
            selected = self.scrim()
            if selected is None:
                return "That scrim no longer exists."
            return (
                f"**Scrim Management — {_safe_name(selected.name)}**\n"
                "Choose an action below."
            )

        def embed(self) -> discord.Embed:
            selected = self.scrim()
            if selected is None:
                return discord.Embed(
                    title="Scrim no longer exists",
                    color=discord.Color.red(),
                )
            state = "🟢 OPEN" if selected.is_open else "🔴 CLOSED"
            password_mode = getattr(selected, "pw_type", "fixed").upper()
            max_matches = getattr(selected, "max_matches", len(selected.maps))
            current_match = getattr(selected, "current_match_counter", 1)
            return discord.Embed(
                title=f"Manage Scrim — {_safe_name(selected.name)}",
                description=(
                    f"Status: **{state}**\n"
                    f"Slots: **{selected.slot_start:02d}–{selected.slot_end:02d}** "
                    f"({len(selected.slots)} total)\n"
                    f"Current match: **{current_match} / {max_matches}**\n"
                    f"Password mode: **{password_mode}**"
                ),
                color=discord.Color.blurple(),
            )

    class GridSettingModal(discord.ui.Modal):
        """One-input modal used by every setting in the configuration grid."""

        value = discord.ui.TextInput(
            label="Setting value",
            placeholder="Enter a value",
            required=True,
            max_length=1000,
        )

        def __init__(
            self,
            grid: "ConfigurationGridView",
            setting: str,
            *,
            label: str,
            placeholder: str,
            default: str = "",
        ):
            super().__init__(title=label[:45], timeout=300)
            self.grid = grid
            self.setting = setting
            self.value.label = label[:45]
            self.value.placeholder = placeholder[:100]
            self.value.default = default[:1000]
            self.value.required = setting != "maps"

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await self.grid.apply_setting(
                interaction, self.setting, str(self.value.value).strip()
            )

        async def on_error(
            self, interaction: discord.Interaction, error: Exception
        ) -> None:
            await _send_error(interaction, error)

    class GridDropdownView(BoundView):
        def __init__(
            self,
            grid: "ConfigurationGridView",
            setting: str,
            *,
            title: str,
            options: list[discord.SelectOption],
        ):
            super().__init__(grid.owner_id, grid.guild_id, timeout=300)
            self.grid = grid
            self.setting = setting
            select = discord.ui.Select(
                placeholder=title[:150],
                min_values=1,
                max_values=1,
                options=options[:25],
            )

            async def select_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    if (
                        self.setting == "pw_mode"
                        and select.values[0] == "fixed"
                        and not self.grid.fixed_pw
                    ):
                        await interaction.response.send_modal(
                            self.grid._modal(
                                "pw_mode",
                                "Fixed password",
                                "FIXED:your-password",
                                "FIXED:",
                            )
                        )
                        return
                    await self.grid.apply_setting(
                        interaction, self.setting, select.values[0]
                    )

            select.callback = select_callback
            self.add_item(select)
            back_button = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.secondary,
                row=1,
            )

            async def back_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.edit_message(
                        content=self.grid.content(),
                        embed=self.grid.embed(),
                        view=self.grid,
                    )

            back_button.callback = back_callback
            self.add_item(back_button)

    class GridChannelDropdownView(BoundView):
        def __init__(
            self,
            grid: "ConfigurationGridView",
            setting: str,
        ):
            super().__init__(grid.owner_id, grid.guild_id, timeout=300)
            self.grid = grid
            self.setting = setting
            if setting == "logs_history":
                selectors = (
                    ("logs", "Select the Logs channel"),
                    ("history", "Select the History channel"),
                )
            elif setting == "public_staff":
                selectors = (
                    ("public", "Select the Public channel"),
                    ("staff", "Select the Staff channel"),
                )
            else:
                selectors = ((setting, f"Select the {setting} channel"),)
            self.selected: dict[str, int | None] = {
                field: getattr(
                    grid,
                    f"{field}_channel_id",
                    None,
                )
                for field, _ in selectors
            }
            for field, placeholder in selectors:
                picker = discord.ui.ChannelSelect(
                    channel_types=[discord.ChannelType.text],
                    placeholder=placeholder,
                    min_values=1,
                    max_values=1,
                    row=0 if field in {"logs", "public"} else 1,
                )

                async def picker_callback(
                    interaction: discord.Interaction,
                    picker=picker,
                    field=field,
                ) -> None:
                    if not await self.interaction_check(interaction):
                        return
                    self.selected[field] = picker.values[0].id
                    if setting in {"logs_history", "public_staff"}:
                        first_field, second_field = (
                            ("logs", "history")
                            if setting == "logs_history"
                            else ("public", "staff")
                        )
                        first_id = self.selected[first_field]
                        second_id = self.selected[second_field]
                        if first_id and second_id:
                            if first_id == second_id:
                                await interaction.response.send_message(
                                    (
                                        "Public and staff channels must be different."
                                        if setting == "public_staff"
                                        else "Logs and history channels must be different."
                                    ),
                                    ephemeral=True,
                                )
                                return
                            await self.grid.apply_setting(
                                interaction,
                                setting,
                                f"{first_id}, {second_id}",
                            )
                        else:
                            await interaction.response.edit_message(
                                content=(
                                    "Select both the Public and Staff channels."
                                    if setting == "public_staff"
                                    else "Select both the Logs and History channels."
                                ),
                                view=self,
                            )
                    else:
                        await self.grid.apply_setting(
                            interaction, setting, str(picker.values[0].id)
                        )

                picker.callback = picker_callback
                self.add_item(picker)
            back_button = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.secondary,
                row=2,
            )

            async def back_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.edit_message(
                        content=self.grid.content(),
                        embed=self.grid.embed(),
                        view=self.grid,
                    )

            back_button.callback = back_callback
            self.add_item(back_button)

    class GridRoleDropdownView(BoundView):
        def __init__(self, grid: "ConfigurationGridView", setting: str):
            super().__init__(grid.owner_id, grid.guild_id, timeout=300)
            self.grid = grid
            self.setting = setting
            if setting == "captain_roles":
                selectors = (
                    ("pending_role", "Select the Pending Captain Role"),
                    ("confirmed_role", "Select the Confirmed Captain Role"),
                )
            else:
                selectors = ((setting, f"Select the {setting.replace('_', ' ')}"),)
            self.selected: dict[str, int | None] = {
                field: getattr(
                    grid,
                    f"{field}_id",
                    None,
                )
                for field, _ in selectors
            }

            for field, placeholder in selectors:
                picker = discord.ui.RoleSelect(
                    placeholder=placeholder,
                    min_values=1,
                    max_values=1,
                    row=0 if field == "pending_role" else 1,
                )

                async def picker_callback(
                    interaction: discord.Interaction,
                    picker=picker,
                    field=field,
                ) -> None:
                    if not await self.interaction_check(interaction):
                        return
                    role = picker.values[0]
                    if role.is_default() or role.managed:
                        await interaction.response.send_message(
                            "Select a regular server role, not @everyone or a managed role.",
                            ephemeral=True,
                        )
                        return
                    self.selected[field] = role.id
                    if setting == "captain_roles":
                        pending_id = self.selected["pending_role"]
                        confirmed_id = self.selected["confirmed_role"]
                        if pending_id and confirmed_id:
                            await self.grid.apply_setting(
                                interaction,
                                setting,
                                f"{pending_id}, {confirmed_id}",
                            )
                        else:
                            await interaction.response.edit_message(
                                content=(
                                    "Select both the Pending and Confirmed Captain roles."
                                ),
                                view=self,
                            )
                    else:
                        await self.grid.apply_setting(
                            interaction, setting, str(role.id)
                        )

                picker.callback = picker_callback
                self.add_item(picker)
            back_button = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.secondary,
                row=2 if setting == "captain_roles" else 1,
            )

            async def back_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.edit_message(
                        content=self.grid.content(),
                        embed=self.grid.embed(),
                        view=self.grid,
                    )

            back_button.callback = back_callback
            self.add_item(back_button)

    class MatchCountDropdownView(BoundView):
        def __init__(self, grid: "ConfigurationGridView"):
            super().__init__(grid.owner_id, grid.guild_id, timeout=300)
            self.grid = grid
            self.page = 1 if grid.max_matches <= 16 else 2
            self.rebuild()

        def rebuild(self) -> None:
            self.clear_items()
            first = 1 if self.page == 1 else 17
            last = 16 if self.page == 1 else MAX_MATCHES
            options = [
                discord.SelectOption(
                    label=f"{value} match{'es' if value != 1 else ''}",
                    value=str(value),
                    default=value == self.grid.max_matches,
                )
                for value in range(first, last + 1)
            ]
            select = discord.ui.Select(
                placeholder="Select the number of matches",
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )

            async def select_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                self.grid.max_matches = int(select.values[0])
                self.grid._resize_match_maps()
                await interaction.response.edit_message(
                    content="Select one map for each match.",
                    embed=None,
                    view=MatchAssignmentDropdownView(self.grid),
                )

            select.callback = select_callback
            self.add_item(select)
            previous = discord.ui.Button(
                label="Previous",
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=self.page == 1,
            )
            next_button = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=self.page == 2,
            )
            maps_button = discord.ui.Button(
                label="Choose Maps",
                style=discord.ButtonStyle.primary,
                row=1,
            )
            back_button = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.secondary,
                row=1,
            )

            async def previous_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    self.page = 1
                    self.rebuild()
                    await interaction.response.edit_message(view=self)

            async def next_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    self.page = 2
                    self.rebuild()
                    await interaction.response.edit_message(view=self)

            async def maps_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    self.grid._resize_match_maps()
                    await interaction.response.edit_message(
                        content="Select one map for each match.",
                        embed=None,
                        view=MatchAssignmentDropdownView(self.grid),
                    )

            async def back_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.edit_message(
                        content=self.grid.content(),
                        embed=self.grid.embed(),
                        view=self.grid,
                    )

            previous.callback = previous_callback
            next_button.callback = next_callback
            maps_button.callback = maps_callback
            back_button.callback = back_callback
            self.add_item(previous)
            self.add_item(next_button)
            self.add_item(maps_button)
            self.add_item(back_button)

    class MatchAssignmentDropdownView(BoundView):
        PAGE_SIZE = 4

        def __init__(self, grid: "ConfigurationGridView", page: int = 0):
            super().__init__(grid.owner_id, grid.guild_id, timeout=300)
            self.grid = grid
            self.page = page
            self.rebuild()

        def rebuild(self) -> None:
            self.clear_items()
            start = self.page * self.PAGE_SIZE
            end = min(start + self.PAGE_SIZE, self.grid.max_matches)
            map_options = [
                discord.SelectOption(label=game_map, value=game_map)
                for game_map in self.grid.maps
            ]
            if not map_options:
                map_options = [
                    discord.SelectOption(
                        label="No custom maps configured",
                        value="__none__",
                    )
                ]
            for match_number in range(start, end):
                select = discord.ui.Select(
                    placeholder=f"Match {match_number + 1}: select a map",
                    min_values=1,
                    max_values=1,
                    options=[
                        discord.SelectOption(
                            label=option.label,
                            value=option.value,
                            default=(
                                self.grid.match_maps[match_number] == option.value
                            ),
                        )
                        for option in map_options
                    ],
                    row=match_number - start,
                )

                async def select_callback(
                    interaction: discord.Interaction,
                    select=select,
                    match_number=match_number,
                ) -> None:
                    if not await self.interaction_check(interaction):
                        return
                    value = select.values[0]
                    if value == "__none__":
                        await interaction.response.send_message(
                            "Configure Custom Maps first.", ephemeral=True
                        )
                        return
                    self.grid.match_maps[match_number] = value
                    self.rebuild()
                    await interaction.response.edit_message(view=self)

                select.callback = select_callback
                self.add_item(select)

            save_button = discord.ui.Button(
                label="Save Matches",
                style=discord.ButtonStyle.success,
                row=4,
                disabled=bool(self.grid.maps)
                and (
                    len(self.grid.match_maps) != self.grid.max_matches
                    or any(not value for value in self.grid.match_maps)
                ),
            )
            previous = discord.ui.Button(
                label="Previous",
                style=discord.ButtonStyle.secondary,
                row=4,
                disabled=self.page == 0,
            )
            next_button = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                row=4,
                disabled=end >= self.grid.max_matches,
            )
            back_button = discord.ui.Button(
                label="Back",
                style=discord.ButtonStyle.secondary,
                row=4,
            )

            async def save_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                if self.grid.is_edit:
                    await self.grid._finish_edit(
                        interaction,
                        changes={
                            "max_matches": self.grid.max_matches,
                            "maps": list(self.grid.maps),
                            "match_maps": list(self.grid.match_maps),
                        },
                    )
                else:
                    self.grid.rebuild()
                    await interaction.response.edit_message(
                        content=self.grid.content(),
                        embed=self.grid.embed(),
                        view=self.grid,
                    )

            async def previous_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    self.page -= 1
                    self.rebuild()
                    await interaction.response.edit_message(view=self)

            async def next_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    self.page += 1
                    self.rebuild()
                    await interaction.response.edit_message(view=self)

            async def back_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.edit_message(
                        content=self.grid.content(),
                        embed=self.grid.embed(),
                        view=self.grid,
                    )

            save_button.callback = save_callback
            previous.callback = previous_callback
            next_button.callback = next_callback
            back_button.callback = back_callback
            self.add_item(previous)
            self.add_item(next_button)
            self.add_item(save_button)
            self.add_item(back_button)

    class ConfigurationGridView(BoundView):
        """Shared categorized grid for adding and editing a scrim."""

        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            *,
            name: str | None = None,
            scrim_id: str | None = None,
        ):
            super().__init__(owner_id, guild_id, timeout=900)
            self.panel = panel
            self.scrim_id = scrim_id
            self.is_edit = scrim_id is not None
            self.name = name or ""
            self.public_channel_id: int | None = None
            self.staff_channel_id: int | None = None
            self.target_channel_id: int | None = None
            self.logs_channel_id: int | None = None
            self.history_channel_id: int | None = None
            self.pending_role_id: int | None = None
            self.confirmed_role_id: int | None = None
            self.slot_start = DEFAULT_SLOT_START
            self.slot_end = DEFAULT_SLOT_END
            self.max_matches = len(DEFAULT_MATCH_MAPS)
            self.maps: list[str] = []
            self.match_maps: list[str] = []
            self.pw_type: str | None = None
            self.fixed_pw = ""
            self.timezone_name: str | None = None
            if self.is_edit:
                self.load_scrim()
            self.rebuild()

        def load_scrim(self) -> Any | None:
            scrim = repository.get(self.scrim_id) if self.scrim_id else None
            if scrim is None or scrim.guild_id != self.guild_id:
                return None
            config = repository.get_idpw_config(scrim.id)
            self.name = scrim.name
            self.public_channel_id = scrim.public_channel_id
            self.staff_channel_id = scrim.staff_channel_id
            self.target_channel_id = config.target_channel_id if config else None
            self.logs_channel_id = scrim.logs_channel_id
            self.history_channel_id = scrim.history_channel_id
            self.pending_role_id = scrim.pending_role_id
            self.confirmed_role_id = scrim.confirmed_role_id
            self.slot_start = scrim.slot_start
            self.slot_end = scrim.slot_end
            self.max_matches = scrim.max_matches
            self.maps = list(scrim.maps)
            self.match_maps = list(getattr(scrim, "match_maps", scrim.maps))
            self._resize_match_maps()
            self.pw_type = getattr(scrim, "pw_type", None)
            if self.pw_type not in PASSWORD_TYPES:
                self.pw_type = None
            self.fixed_pw = (
                getattr(scrim, "fixed_pw", "",)
                or (config.fixed_password if config else "")
            )
            self.timezone_name = getattr(scrim, "timezone", None)
            if config is not None and config.timezone_name:
                self.timezone_name = config.timezone_name
            return scrim

        @staticmethod
        def _mention_channel(value: int | None) -> str:
            return f"<#{value}>" if value else "Missing"

        @staticmethod
        def _mention_role(value: int | None) -> str:
            return f"<@&{value}>" if value else "Missing"

        def _status(self, value: object, display: str | None = None) -> str:
            return f"🟢 {display or value}" if value else "🔴 Missing"

        def _resize_match_maps(self) -> None:
            """Keep assignments aligned with the match count and fill new slots."""
            existing = list(self.match_maps)
            self.match_maps = [
                (
                    existing[index]
                    if index < len(existing) and existing[index] in self.maps
                    else self.maps[index] if index < len(self.maps) else ""
                )
                for index in range(self.max_matches)
            ]

        def content(self) -> str:
            if self.is_edit:
                return (
                    f"**Edit Configuration — {_safe_name(self.name)}**\n"
                    "Choose a setting below. Changes are saved immediately."
                )
            return (
                f"**Setup Wizard — {_safe_name(self.name)}**\n"
                "Configure the required settings, then activate the scrim."
            )

        def embed(self) -> discord.Embed:
            maps_value = (
                f"{len(self.maps)} custom maps"
                if self.maps
                else "Optional — no custom rotation"
            )
            assigned_count = sum(
                bool(game_map)
                for game_map in self.match_maps[: self.max_matches]
            )
            match_maps_value = (
                f"{assigned_count}/{self.max_matches} assigned"
                if self.maps
                else "Not configured"
            )
            if self.is_edit:
                channel_value = (
                    f"Public + Staff:\n"
                    f"  Public: {self._mention_channel(self.public_channel_id)}\n"
                    f"  Staff: {self._mention_channel(self.staff_channel_id)}\n"
                    f"ID/PW: {self._mention_channel(self.target_channel_id)}\n"
                    f"Logs + History:\n"
                    f"  Logs: {self._mention_channel(self.logs_channel_id)}\n"
                    f"  History: {self._mention_channel(self.history_channel_id)}"
                )
                role_value = (
                    f"Pending + Confirmed Captain Roles:\n"
                    f"  Pending: {self._mention_role(self.pending_role_id)}\n"
                    f"  Confirmed: {self._mention_role(self.confirmed_role_id)}"
                )
                settings_value = (
                    f"Matches: **{self.max_matches}** "
                    f"({self.slot_start:02d}–{self.slot_end:02d} slots)\n"
                    f"Custom Maps: **{maps_value}**\n"
                    f"Match Maps: **{match_maps_value}**\n"
                    f"PW Mode: **{(self.pw_type or 'not configured').upper()}**\n"
                    f"Timezone: **{self.timezone_name or 'not configured'}**"
                )
                title = f"Edit Configuration — {_safe_name(self.name)}"
                description = "Current database values"
            else:
                channel_value = (
                    f"Public + Staff Ch.: "
                    f"{self._status(self.public_channel_id and self.staff_channel_id, 'Configured')}\n"
                    f"ID/PW Target: {self._status(self.target_channel_id, self._mention_channel(self.target_channel_id))}\n"
                    f"Logs & History: {self._status(self.logs_channel_id and self.history_channel_id, 'Configured')}"
                )
                role_value = (
                    f"Pending + Confirmed Roles: "
                    f"{self._status(self.pending_role_id and self.confirmed_role_id, 'Configured')}"
                )
                settings_value = (
                    f"Matches: {self._status(self.max_matches, str(self.max_matches))}\n"
                    f"Custom Maps: 🟢 {maps_value}\n"
                    f"Match Maps: {self._status(not self.maps or assigned_count == self.max_matches, match_maps_value)}\n"
                    f"PW Mode: {self._status(self.pw_type, (self.pw_type or '').upper())}\n"
                    f"Timezone: {self._status(self.timezone_name, self.timezone_name or '')}"
                )
                title = f"Setup Wizard — {_safe_name(self.name)}"
                description = "🔴 Missing · 🟢 Configured"
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Channels", value=channel_value, inline=False)
            embed.add_field(name="Roles", value=role_value, inline=False)
            embed.add_field(name="Settings", value=settings_value, inline=False)
            return embed

        def ready_to_activate(self) -> bool:
            return all(
                (
                    self.name.strip(),
                    self.public_channel_id,
                    self.staff_channel_id,
                    self.target_channel_id,
                    self.logs_channel_id,
                    self.history_channel_id,
                    self.pending_role_id,
                    self.confirmed_role_id,
                    self.max_matches,
                    self.pw_type,
                    self.timezone_name,
                    self.fixed_pw if self.pw_type == "fixed" else True,
                    not self.maps
                    or (
                        len(self.match_maps) == self.max_matches
                        and all(self.match_maps)
                    ),
                )
            )

        def _modal(
            self,
            setting: str,
            label: str,
            placeholder: str,
            default: object = "",
        ) -> GridSettingModal:
            return GridSettingModal(
                self,
                setting,
                label=label,
                placeholder=placeholder,
                default=str(default or ""),
            )

        def rebuild(self) -> None:
            self.clear_items()

            specs = (
                (
                    "public_staff",
                    "Public + Staff Ch.",
                    "Channel IDs",
                    "public_id, staff_id",
                    (
                        f"{self.public_channel_id}, {self.staff_channel_id}"
                        if self.public_channel_id and self.staff_channel_id
                        else ""
                    ),
                ),
                ("target", "ID/PW Target", "Channel ID", "123456789", self.target_channel_id),
                (
                    "logs_history",
                    "Logs & History",
                    "Logs and history channel IDs",
                    "123456789, 987654321",
                    (
                        f"{self.logs_channel_id}, {self.history_channel_id}"
                        if self.logs_channel_id and self.history_channel_id
                        else ""
                    ),
                ),
            )
            for setting, label, modal_label, placeholder, default in specs:
                button = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary,
                    row=0,
                )

                async def callback(
                    interaction: discord.Interaction,
                    setting=setting,
                    modal_label=modal_label,
                    placeholder=placeholder,
                    default=default,
                ) -> None:
                    if await self.interaction_check(interaction):
                        if setting in {"public_staff", "target", "logs_history"}:
                            await interaction.response.edit_message(
                                content="Select an existing server channel.",
                                embed=None,
                                view=GridChannelDropdownView(self, setting),
                            )
                        else:
                            await interaction.response.send_modal(
                                self._modal(setting, modal_label, placeholder, default)
                            )

                button.callback = callback
                self.add_item(button)

            for setting, label in (
                ("captain_roles", "Pending + Confirmed Roles"),
            ):
                button = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary,
                    row=1,
                )

                async def callback(
                    interaction: discord.Interaction,
                    setting=setting,
                    label=label,
                ) -> None:
                    if await self.interaction_check(interaction):
                        await interaction.response.edit_message(
                            content="Select an existing server role.",
                            embed=None,
                            view=GridRoleDropdownView(self, setting),
                        )

                button.callback = callback
                self.add_item(button)

            settings = (
                ("matches", "Matches", "Match count", f"1-{MAX_MATCHES}", self.max_matches),
                (
                    "maps",
                    "Custom Maps",
                    "Map rotation (optional)",
                    "Erangel, Miramar, Sanhok",
                    ", ".join(self.maps),
                ),
                (
                    "pw_mode",
                    "PW Mode",
                    "Password mode",
                    "DYNAMIC or FIXED:your-password",
                    (
                        f"{self.pw_type.upper()}:{self.fixed_pw}"
                        if self.pw_type == "fixed" and self.fixed_pw
                        else (self.pw_type or "")
                    ),
                ),
                (
                    "timezone",
                    "Timezone",
                    "UTC offset",
                    "UTC+01:00",
                    self.timezone_name or "",
                ),
            )
            for setting, label, modal_label, placeholder, default in settings:
                button = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary,
                    row=2,
                )

                async def callback(
                    interaction: discord.Interaction,
                    setting=setting,
                    modal_label=modal_label,
                    placeholder=placeholder,
                    default=default,
                ) -> None:
                    if await self.interaction_check(interaction):
                        if setting == "matches":
                            await interaction.response.edit_message(
                                content="Select the number of matches.",
                                embed=None,
                                view=MatchCountDropdownView(self),
                            )
                        elif setting == "timezone":
                            await interaction.response.edit_message(
                                content="Select the ID/PW timezone.",
                                embed=None,
                                view=GridDropdownView(
                                    self,
                                    setting,
                                    title="Select a timezone",
                                    options=[
                                        discord.SelectOption(
                                            label=label,
                                            value=value,
                                            default=value == self.timezone_name,
                                        )
                                        for value, label in IDPW_TIMEZONE_CHOICES
                                    ],
                                ),
                            )
                        elif setting == "pw_mode":
                            await interaction.response.edit_message(
                                content="Select the password mode.",
                                embed=None,
                                view=GridDropdownView(
                                    self,
                                    setting,
                                    title="Select DYNAMIC or FIXED",
                                    options=[
                                        discord.SelectOption(
                                            label="Dynamic password",
                                            value="dynamic",
                                            default=self.pw_type == "dynamic",
                                        ),
                                        discord.SelectOption(
                                            label="Fixed password",
                                            value="fixed",
                                            default=self.pw_type == "fixed",
                                        ),
                                    ],
                                ),
                            )
                        else:
                            await interaction.response.send_modal(
                                self._modal(setting, modal_label, placeholder, default)
                            )

                button.callback = callback
                self.add_item(button)

            if self.is_edit:
                reset_button = discord.ui.Button(
                    label="Reset Match Counter",
                    emoji="🔄",
                    style=discord.ButtonStyle.secondary,
                    row=3,
                )

                async def reset_callback(interaction: discord.Interaction) -> None:
                    if not await self.interaction_check(interaction):
                        return
                    scrim = repository.get(self.scrim_id)
                    if scrim is None or scrim.guild_id != self.guild_id:
                        await interaction.response.send_message(
                            "That scrim no longer exists.", ephemeral=True
                        )
                        return
                    await interaction.response.defer()
                    try:
                        with repository.transaction():
                            scrim.current_match_counter = 1
                        await self.panel.refresh_message()
                        self.load_scrim()
                        await interaction.edit_original_response(
                            content="✅ Match counter reset to 1.",
                            embed=self.embed(),
                            view=self,
                        )
                    except (SlotStorageError, ValueError) as error:
                        await _send_error(interaction, error)

                reset_button.callback = reset_callback
                self.add_item(reset_button)

                back_button = discord.ui.Button(
                    label="Back to Selector",
                    emoji="↩️",
                    style=discord.ButtonStyle.secondary,
                    row=3,
                )

                async def back_callback(interaction: discord.Interaction) -> None:
                    if not await self.interaction_check(interaction):
                        return
                    selector = ScrimSelectorView(
                        self.panel, self.owner_id, self.guild_id
                    )
                    await interaction.response.edit_message(
                        content=selector.content(),
                        embed=selector.embed(),
                        view=selector,
                    )

                back_button.callback = back_callback
                self.add_item(back_button)
            else:
                cancel_button = discord.ui.Button(
                    label="Cancel Draft",
                    emoji="🟥",
                    style=discord.ButtonStyle.danger,
                    row=3,
                )

                async def cancel_callback(interaction: discord.Interaction) -> None:
                    if await self.interaction_check(interaction):
                        self.stop()
                        await interaction.response.edit_message(
                            content="Draft cancelled. Nothing was created.",
                            embed=None,
                            view=None,
                        )

                cancel_button.callback = cancel_callback
                self.add_item(cancel_button)

                confirm_button = discord.ui.Button(
                    label="Confirm & Activate",
                    emoji="✅",
                    style=discord.ButtonStyle.success,
                    disabled=not self.ready_to_activate(),
                    row=3,
                )

                async def confirm_callback(interaction: discord.Interaction) -> None:
                    await self.confirm_and_activate(interaction)

                confirm_button.callback = confirm_callback
                self.add_item(confirm_button)

        async def _finish_edit(
            self,
            interaction: discord.Interaction,
            *,
            changes: dict[str, object] | None = None,
            notice: str | None = None,
            ephemeral_notice: str | None = None,
        ) -> None:
            await interaction.response.defer()
            if changes:
                await apply_scrim_change(
                    interaction,
                    self.panel,
                    self.scrim_id,
                    changes=changes,
                    response_mode="deferred",
                    notice=notice,
                    ephemeral_notice=ephemeral_notice,
                )
            else:
                await self.panel.refresh_message()
                self.load_scrim()
                await interaction.edit_original_response(
                    content=self.content(),
                    embed=self.embed(),
                    view=self,
                )

        async def _save_idpw(self, interaction: discord.Interaction) -> None:
            scrim = repository.get(self.scrim_id) if self.scrim_id else None
            if scrim is None or scrim.guild_id != self.guild_id:
                await interaction.response.send_message(
                    "That scrim no longer exists.", ephemeral=True
                )
                return
            if self.target_channel_id is None:
                await interaction.response.send_message(
                    "Set the ID/PW Target before changing password mode or timezone.",
                    ephemeral=True,
                )
                return
            if self.pw_type == "fixed" and not self.fixed_pw.strip():
                await interaction.response.send_message(
                    "FIXED mode requires a password. Use `FIXED:your-password` "
                    "in the PW Mode field.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            try:
                repository.save_idpw_config(
                    self.scrim_id,
                    target_channel_id=self.target_channel_id,
                    fixed_password=self.fixed_pw if self.pw_type == "fixed" else "",
                    timezone_name=self.timezone_name or DEFAULT_IDPW_TIMEZONE,
                    password_type=self.pw_type or "dynamic",
                )
                await self.panel.refresh_message()
                self.load_scrim()
                await interaction.edit_original_response(
                    content=self.content(),
                    embed=self.embed(),
                    view=self,
                )
            except (SlotStorageError, ValueError) as error:
                await _send_error(interaction, error)

        async def apply_setting(
            self,
            interaction: discord.Interaction,
            setting: str,
            raw_value: str,
        ) -> None:
            if not await self.interaction_check(interaction):
                return
            guild = interaction.guild
            try:
                if setting == "public_staff":
                    values = [int(part.strip()) for part in raw_value.split(",")]
                    if len(values) != 2 or any(value <= 0 for value in values):
                        raise ValueError(
                            "Enter exactly two channel IDs: public, staff."
                        )
                    if values[0] == values[1]:
                        raise ValueError(
                            "Public and staff channels must be different."
                        )
                    channels = [guild.get_channel(value) for value in values]
                    if not all(
                        isinstance(channel, discord.TextChannel)
                        for channel in channels
                    ):
                        raise ValueError(
                            "Both IDs must be existing text channels in this server."
                        )
                    permission_error = _channel_permissions_error(
                        guild, channels[0], channels[1]
                    )
                    if permission_error:
                        raise ValueError(permission_error)
                    self.public_channel_id, self.staff_channel_id = values
                elif setting in {"public", "staff", "target"}:
                    value = int(raw_value)
                    if value <= 0:
                        raise ValueError("Enter a positive Discord channel ID.")
                    channel = guild.get_channel(value)
                    if not isinstance(channel, discord.TextChannel):
                        raise ValueError("That ID is not an existing text channel in this server.")
                    member = guild.me
                    permissions = channel.permissions_for(member) if member else None
                    required = ("view_channel", "send_messages", "read_message_history")
                    missing = [
                        permission.replace("_", " ").title()
                        for permission in required
                        if permissions is None
                        or not getattr(permissions, permission, False)
                    ]
                    if setting == "public" and (
                        permissions is None or not permissions.manage_messages
                    ):
                        missing.append("Manage Messages")
                    if missing:
                        raise ValueError(
                            f"I am missing {', '.join(missing)} in {channel.mention}."
                        )
                    if setting == "public":
                        self.public_channel_id = value
                    elif setting == "staff":
                        self.staff_channel_id = value
                    else:
                        self.target_channel_id = value
                elif setting == "logs_history":
                    values = [int(part.strip()) for part in raw_value.split(",")]
                    if len(values) != 2 or any(value <= 0 for value in values):
                        raise ValueError(
                            "Enter exactly two channel IDs: logs, history."
                        )
                    channels = [guild.get_channel(value) for value in values]
                    if not all(
                        isinstance(channel, discord.TextChannel) for channel in channels
                    ):
                        raise ValueError(
                            "Both IDs must be existing text channels in this server."
                        )
                    if values[0] == values[1]:
                        raise ValueError("Logs and history channels must be different.")
                    self.logs_channel_id, self.history_channel_id = values
                elif setting == "captain_roles":
                    values = [int(part.strip()) for part in raw_value.split(",")]
                    if len(values) != 2 or any(value <= 0 for value in values):
                        raise ValueError(
                            "Enter exactly two role IDs: pending, confirmed."
                        )
                    if values[0] == values[1]:
                        raise ValueError(
                            "Pending and Confirmed Captain roles must be different."
                        )
                    roles = [guild.get_role(value) for value in values]
                    if any(
                        role is None or role.is_default() or role.managed
                        for role in roles
                    ):
                        raise ValueError(
                            "Both IDs must be existing regular server roles."
                        )
                    config = repository.get_server_config(self.guild_id)
                    if config and config.staff_role_id in values:
                        raise ValueError(
                            "Captain roles must differ from the global Staff role."
                        )
                    self.pending_role_id, self.confirmed_role_id = values
                elif setting in {"pending_role", "confirmed_role"}:
                    value = int(raw_value)
                    role = guild.get_role(value)
                    if (
                        role is None
                        or role.is_default()
                        or role.managed
                    ):
                        raise ValueError(
                            "Enter an existing regular server role ID."
                        )
                    config = repository.get_server_config(self.guild_id)
                    if config and value == config.staff_role_id:
                        raise ValueError(
                            "Captain roles must differ from the global Staff role."
                        )
                    if setting == "pending_role":
                        self.pending_role_id = value
                    else:
                        self.confirmed_role_id = value
                elif setting == "matches":
                    value = int(raw_value)
                    if not 1 <= value <= MAX_MATCHES:
                        raise ValueError(
                            f"Matches must be between 1 and {MAX_MATCHES}."
                        )
                    self.max_matches = value
                    self._resize_match_maps()
                elif setting == "maps":
                    if not raw_value.strip():
                        self.maps = []
                        self.match_maps = []
                    else:
                        _, self.maps = _match_configuration_from_values(
                            self.max_matches, raw_value
                        )
                        self._resize_match_maps()
                elif setting == "pw_mode":
                    value = raw_value.strip()
                    lowered = value.casefold()
                    if lowered.startswith("fixed:"):
                        password = value.split(":", 1)[1].strip()
                        if not password:
                            raise ValueError("FIXED mode requires a password.")
                        self.pw_type, self.fixed_pw = "fixed", password
                    elif lowered == "fixed":
                        if not self.fixed_pw:
                            raise ValueError(
                                "Use FIXED:your-password to set the fixed password."
                            )
                        self.pw_type = "fixed"
                    elif lowered == "dynamic":
                        self.pw_type, self.fixed_pw = "dynamic", ""
                    else:
                        raise ValueError(
                            "Enter DYNAMIC or FIXED:your-password."
                        )
                elif setting == "timezone":
                    if raw_value not in {value for value, _ in IDPW_TIMEZONE_CHOICES}:
                        raise ValueError(
                            "Use one of the supported values such as UTC, UTC+01:00, or UTC-05:00."
                        )
                    self.timezone_name = raw_value
                else:
                    raise ValueError("Unknown configuration setting.")
            except (TypeError, ValueError) as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return

            if self.is_edit:
                if setting == "target":
                    await self._save_idpw(interaction)
                elif setting in {"pw_mode", "timezone"}:
                    await self._save_idpw(interaction)
                elif setting == "public_staff":
                    await self._finish_edit(
                        interaction,
                        changes={
                            "public_channel_id": self.public_channel_id,
                            "staff_channel_id": self.staff_channel_id,
                        },
                    )
                elif setting == "logs_history":
                    await self._finish_edit(
                        interaction,
                        changes={
                            "logs_channel_id": self.logs_channel_id,
                            "history_channel_id": self.history_channel_id,
                        },
                    )
                elif setting == "captain_roles":
                    await self._finish_edit(
                        interaction,
                        changes={
                            "pending_role_id": self.pending_role_id,
                            "confirmed_role_id": self.confirmed_role_id,
                        },
                    )
                elif setting == "matches":
                    await self._finish_edit(
                        interaction,
                        changes={
                            "max_matches": self.max_matches,
                            "maps": list(self.maps),
                            "match_maps": list(self.match_maps),
                        },
                    )
                elif setting == "maps":
                    await self._finish_edit(
                        interaction,
                        changes={
                            "maps": list(self.maps),
                            "match_maps": list(self.match_maps),
                        },
                    )
                else:
                    field = {
                        "public": "public_channel_id",
                        "staff": "staff_channel_id",
                        "pending_role": "pending_role_id",
                        "confirmed_role": "confirmed_role_id",
                    }[setting]
                    await self._finish_edit(
                        interaction, changes={field: getattr(self, field)}
                    )
            else:
                self.rebuild()
                await interaction.response.edit_message(
                    content=self.content(),
                    embed=self.embed(),
                    view=self,
                )

        async def confirm_and_activate(
            self, interaction: discord.Interaction
        ) -> None:
            if not await self.interaction_check(interaction):
                return
            if not self.ready_to_activate():
                await interaction.response.send_message(
                    "Complete every required setting marked 🔴 before activating.",
                    ephemeral=True,
                )
                return
            scrims = repository.list(self.guild_id)
            if any(
                scrim.name.casefold() == self.name.casefold() for scrim in scrims
            ):
                await interaction.response.send_message(
                    "A scrim with that name already exists in this server.",
                    ephemeral=True,
                )
                return
            guild = interaction.guild
            channels = [
                guild.get_channel(value)
                for value in (
                    self.public_channel_id,
                    self.staff_channel_id,
                    self.logs_channel_id,
                    self.history_channel_id,
                    self.target_channel_id,
                )
            ]
            if not all(isinstance(channel, discord.TextChannel) for channel in channels):
                await interaction.response.send_message(
                    "One or more configured channels no longer exists.",
                    ephemeral=True,
                )
                return
            if len(
                {
                    self.public_channel_id,
                    self.staff_channel_id,
                    self.logs_channel_id,
                    self.history_channel_id,
                }
            ) != 4:
                await interaction.response.send_message(
                    "Public, staff, logs, and history channels must all be different.",
                    ephemeral=True,
                )
                return
            permission_error = _channel_permissions_error(
                guild,
                channels[0],
                channels[1],
                (channels[2], "logs"),
                (channels[3], "history"),
                (channels[4], "ID/PW target"),
            )
            if permission_error:
                await interaction.response.send_message(
                    permission_error, ephemeral=True
                )
                return
            config = repository.get_server_config(self.guild_id)
            if config is None or config.staff_role_id is None:
                await interaction.response.send_message(
                    "Run `!set <@Role>` before activating a scrim.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            scrim = None
            try:
                scrim = repository.create(
                    self.guild_id,
                    self.name,
                    self.public_channel_id,
                    self.staff_channel_id,
                    staff_role_id=config.staff_role_id,
                    pending_role_id=self.pending_role_id,
                    confirmed_role_id=self.confirmed_role_id,
                    logs_channel_id=self.logs_channel_id,
                    history_channel_id=self.history_channel_id,
                    is_open=False,
                    slot_start=self.slot_start,
                    slot_end=self.slot_end,
                    max_matches=self.max_matches,
                    maps=self.maps,
                    match_maps=self.match_maps,
                )
                repository.save_idpw_config(
                    scrim.id,
                    target_channel_id=self.target_channel_id,
                    fixed_password=self.fixed_pw if self.pw_type == "fixed" else "",
                    timezone_name=self.timezone_name,
                    password_type=self.pw_type,
                )
            except (SlotStorageError, ValueError) as error:
                if scrim is not None:
                    try:
                        repository.delete(scrim.id, self.guild_id)
                    except Exception:
                        logger.exception("Could not clean up failed scrim draft")
                await interaction.edit_original_response(
                    content=f"The scrim was not activated: {error}",
                    embed=None,
                    view=None,
                )
                return
            try:
                published = await publish_scrim(scrim)
            except Exception as error:
                logger.exception("Scrim activated but initial board publish failed", exc_info=error)
                published = False
            await self.panel.refresh_message()
            result = (
                f"✅ Scrim **{_safe_name(scrim.name)}** was activated."
                if published
                else (
                    f"✅ Scrim **{_safe_name(scrim.name)}** was activated, but its "
                    "slot board could not be published."
                )
            )
            if log_action is not None:
                try:
                    await log_action(
                        scrim,
                        "SCRIM CREATED",
                        _scrim_configuration_details(scrim, repository),
                    )
                except Exception:
                    logger.exception("Could not write scrim creation log")
            await interaction.edit_original_response(
                content=result,
                embed=None,
                view=None,
            )

    class ScrimEditView(ConfigurationGridView):
        """Compatibility name used by existing edit callbacks."""

        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            scrim_id: str,
        ):
            super().__init__(
                panel,
                owner_id,
                guild_id,
                scrim_id=scrim_id,
            )

    class ScrimSetupWizardView(ConfigurationGridView):
        """Named wizard entry point for the Add Scrim flow."""

        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            name: str,
        ):
            super().__init__(
                panel,
                owner_id,
                guild_id,
                name=name,
            )

    def custom_emoji_content(scrim) -> str:
        return (
            f"**Custom legend emojis — {_safe_name(scrim.name)}**\n"
            f"Available: {scrim.emoji_available}\n"
            f"Reserved: {scrim.emoji_reserved}\n"
            f"Pending: {scrim.emoji_pending}\n"
            f"Confirmed: {scrim.emoji_confirmed}\n\n"
            "Choose a status to edit."
        )

    class CustomEmojiMenuView(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            scrim_id: str,
        ):
            super().__init__(owner_id, guild_id, timeout=600)
            self.panel = panel
            self.scrim_id = scrim_id

            async def edit_emoji(
                interaction: discord.Interaction, field_name: str
            ) -> None:
                if not await self.interaction_check(interaction):
                    return
                scrim = repository.get(self.scrim_id)
                if scrim is None or scrim.guild_id != self.guild_id:
                    await _send_error(
                        interaction,
                        ValueError("The selected scrim no longer exists."),
                    )
                    return
                await interaction.response.send_message(
                    "Please mention me and send the new emoji "
                    "(e.g., `@ARC_Bot <:my_emoji:123>`). "
                    "To cancel, type `@ARC_Bot cancel`.",
                    ephemeral=True,
                )

                def check(message: discord.Message) -> bool:
                    bot_user = bot.user
                    return bool(
                        message.author.id == interaction.user.id
                        and message.channel.id == interaction.channel_id
                        and bot_user is not None
                        and bot_user.mentioned_in(message)
                    )

                try:
                    message = await bot.wait_for(
                        'message', check=check, timeout=60.0
                    )
                except asyncio.TimeoutError:
                    result = "Emoji edit timed out."
                    message = None
                else:
                    if "cancel" in message.content.casefold():
                        result = "Emoji edit cancelled."
                    else:
                        emoji = extract_raw_emoji(message.content)
                        if emoji is None:
                            result = (
                                "No custom or Unicode emoji was found. "
                                "The emoji was not changed."
                            )
                        else:
                            try:
                                updated_scrim = repository.save_scrim_emoji(
                                    self.scrim_id, self.guild_id, field_name, emoji
                                )
                            except (SlotStorageError, ValueError):
                                logger.exception("Could not save custom legend emoji")
                                result = "The emoji could not be saved."
                            else:
                                if updated_scrim.public_message_id is not None:
                                    try:
                                        await publish_scrim(updated_scrim)
                                    except Exception:
                                        logger.exception(
                                            "Could not refresh the scrim board after "
                                            "updating its legend emoji"
                                        )
                                result = None

                try:
                    await interaction.delete_original_response()
                except discord.HTTPException:
                    logger.exception("Could not delete the emoji edit prompt")
                if message is not None:
                    try:
                        await message.delete()
                    except (
                        discord.NotFound,
                        discord.Forbidden,
                        discord.HTTPException,
                    ):
                        pass
                if result:
                    try:
                        await interaction.followup.send(result, ephemeral=True)
                    except discord.HTTPException:
                        logger.exception("Could not report emoji edit result")
                scrim = repository.get(self.scrim_id)
                if scrim is not None and interaction.message is not None:
                    try:
                        await interaction.message.edit(
                            content=custom_emoji_content(scrim),
                            view=CustomEmojiMenuView(
                                self.panel,
                                self.owner_id,
                                self.guild_id,
                                self.scrim_id,
                            ),
                        )
                    except discord.HTTPException:
                        logger.exception("Could not refresh custom emoji menu")

            buttons = (
                ("Edit Available", "emoji_available"),
                ("Edit Reserved", "emoji_reserved"),
                ("Edit Pending", "emoji_pending"),
                ("Edit Confirmed", "emoji_confirmed"),
            )
            for label, field_name in buttons:
                button = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    row=0,
                )
                button.callback = lambda interaction, field_name=field_name: (
                    edit_emoji(interaction, field_name)
                )
                self.add_item(button)

            reset = discord.ui.Button(
                label="Reset to Defaults",
                emoji="🔄",
                style=discord.ButtonStyle.danger,
                row=1,
            )

            async def reset_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                scrim = repository.get(self.scrim_id)
                if scrim is None or scrim.guild_id != self.guild_id:
                    await _send_error(
                        interaction,
                        ValueError("The selected scrim no longer exists."),
                    )
                    return
                try:
                    scrim = repository.reset_scrim_emojis(
                        self.scrim_id, self.guild_id
                    )
                except (SlotStorageError, ValueError):
                    logger.exception("Could not reset custom legend emojis")
                    await _send_error(
                        interaction,
                        ValueError("The emojis could not be reset."),
                    )
                    return
                await interaction.response.edit_message(
                    content=custom_emoji_content(scrim),
                    view=CustomEmojiMenuView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.scrim_id,
                    ),
                )
                try:
                    confirmation = await interaction.followup.send(
                        "✅ Emojis have been reset to default.",
                        ephemeral=True,
                        wait=True,
                    )
                    if confirmation is not None:
                        await confirmation.delete(delay=5)
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    logger.exception("Could not report emoji reset")

            reset.callback = reset_callback
            self.add_item(reset)

            back = discord.ui.Button(
                label="Back to Configuration",
                emoji="↩️",
                style=discord.ButtonStyle.primary,
                row=2,
            )

            async def back_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                scrim = repository.get(self.scrim_id)
                if scrim is None or scrim.guild_id != self.guild_id:
                    await interaction.response.edit_message(
                        content="That scrim no longer exists.", view=None
                    )
                    return
                edit_view = ScrimEditView(
                    self.panel, self.owner_id, self.guild_id, self.scrim_id
                )
                await interaction.response.edit_message(
                    content=edit_view.content(),
                    view=edit_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            back.callback = back_callback
            self.add_item(back)

    class IdPwPasswordModal(discord.ui.Modal, title="Set match password"):
        password = discord.ui.TextInput(
            label="Fixed match password",
            placeholder="Enter the password used for the match lobby",
            min_length=1,
            max_length=100,
            required=True,
        )

        def __init__(self, config_view) -> None:
            super().__init__()
            self.config_view = config_view

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not await self.config_view.interaction_check(interaction):
                return
            password = str(self.password.value).strip()
            if not password:
                await interaction.response.send_message(
                    "The match password cannot be empty.", ephemeral=True
                )
                return
            try:
                repository.save_idpw_config(
                    self.config_view.scrim.id,
                    target_channel_id=self.config_view.channel_id,
                    fixed_password=password,
                    timezone_name=self.config_view.timezone_name,
                    password_type="fixed",
                )
            except (SlotStorageError, ValueError):
                logger.exception("Could not save ID/password configuration")
                await interaction.response.send_message(
                    "The ID/password configuration could not be saved. "
                    "Nothing was changed.",
                    ephemeral=True,
                )
                return
            self.config_view.stop()
            await self.config_view.panel.refresh_message()
            await interaction.response.send_message(
                "ID/password alerts are configured. Use `!idpw <room_id> / <minutes>` "
                "when you want to publish match access.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    class IdPwConfigView(BoundView):
        def __init__(
            self, panel: SetupPanel, owner_id: int, guild_id: int, scrim: Any
        ) -> None:
            super().__init__(owner_id, guild_id, timeout=600)
            self.panel = panel
            self.scrim = scrim
            current = repository.get_idpw_config(scrim.id)
            self.channel_id = current.target_channel_id if current else None
            self.timezone_name = (
                current.timezone_name if current else DEFAULT_IDPW_TIMEZONE
            )
            self.password_type = getattr(scrim, "pw_type", "fixed")
            if self.password_type not in PASSWORD_TYPES:
                self.password_type = "fixed"

            channel_picker = discord.ui.ChannelSelect(
                placeholder="Select the target text channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
                row=0,
            )

            async def channel_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                channel = interaction.guild.get_channel(channel_picker.values[0].id)
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message(
                        "Select an existing text channel in this server.",
                        ephemeral=True,
                    )
                    return
                self.channel_id = channel.id
                save_button.disabled = False
                await interaction.response.edit_message(
                    content=self.content(), view=self
                )

            channel_picker.callback = channel_callback
            self.add_item(channel_picker)

            timezone_picker = discord.ui.Select(
                placeholder="Select the timezone for match times",
                options=[
                    discord.SelectOption(
                        label=label,
                        value=value,
                        default=value == self.timezone_name,
                    )
                    for value, label in IDPW_TIMEZONE_CHOICES
                ],
                min_values=1,
                max_values=1,
                row=1,
            )

            async def timezone_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                self.timezone_name = timezone_picker.values[0]
                await interaction.response.edit_message(
                    content=self.content(), view=self
                )

            timezone_picker.callback = timezone_callback
            self.add_item(timezone_picker)

            mode_picker = discord.ui.Select(
                placeholder="Select the password mode",
                options=[
                    discord.SelectOption(
                        label="FIXED",
                        value="fixed",
                        description="Use the saved password for !idpw and !idpwg.",
                        default=self.password_type == "fixed",
                    ),
                    discord.SelectOption(
                        label="DYNAMIC",
                        value="dynamic",
                        description="Provide the password in each !idpwg command.",
                        default=self.password_type == "dynamic",
                    ),
                ],
                min_values=1,
                max_values=1,
                row=2,
            )

            async def mode_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                self.password_type = mode_picker.values[0]
                save_button.label = "Confirm"
                await interaction.response.edit_message(
                    content=self.content(), view=self
                )

            mode_picker.callback = mode_callback
            self.add_item(mode_picker)

            save_button = discord.ui.Button(
                label="Confirm",
                style=discord.ButtonStyle.success,
                disabled=self.channel_id is None,
                row=3,
            )

            async def save_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                if self.channel_id is None:
                    await interaction.response.send_message(
                        "Select a target channel first.",
                        ephemeral=True,
                    )
                    return
                if self.password_type == "dynamic":
                    try:
                        repository.save_idpw_config(
                            self.scrim.id,
                            target_channel_id=self.channel_id,
                            fixed_password="",
                            timezone_name=self.timezone_name,
                            password_type="dynamic",
                        )
                    except (SlotStorageError, ValueError):
                        logger.exception("Could not save dynamic ID/password configuration")
                        await interaction.response.send_message(
                            "The dynamic ID/password configuration could not be saved. "
                            "Nothing was changed.",
                            ephemeral=True,
                        )
                        return
                    self.stop()
                    await self.panel.refresh_message()
                    await interaction.response.edit_message(
                        content=(
                            "Dynamic ID/password mode is configured. Use "
                            "`!idpwg1` through `!idpwg25` with "
                            "`<room_id> / <password> / <minutes>`."
                        ),
                        view=None,
                    )
                    return
                await interaction.response.send_modal(IdPwPasswordModal(self))

            save_button.callback = save_callback
            self.add_item(save_button)

            cancel_button = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.secondary, row=3
            )

            async def cancel_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                self.stop()
                await interaction.response.edit_message(
                    content="ID/password setup cancelled.", view=None
                )

            cancel_button.callback = cancel_callback
            self.add_item(cancel_button)

        def content(self) -> str:
            channel = f"<#{self.channel_id}>" if self.channel_id else "not selected"
            return (
                f"**ID/PW · {_safe_name(self.scrim.name)}**\n"
                f"Channel: {channel}  ·  Timezone: `{self.timezone_name}`\n"
                f"Mode: **{self.password_type.upper()}**\n"
                + (
                    "Select a channel, timezone, and **FIXED**, then tap **Confirm** "
                    "to enter the password."
                    if self.password_type == "fixed"
                    else (
                        "Select a channel, timezone, and **DYNAMIC**, then tap "
                        "**Confirm**. Use `!idpwgX <room_id> / <password> / <minutes>`."
                    )
                )
            )

    class ManagerRoleView(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            name: str,
            slot_start: int = DEFAULT_SLOT_START,
            slot_end: int = DEFAULT_SLOT_END,
            max_matches: int = len(DEFAULT_MATCH_MAPS),
            maps: list[str] | None = None,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.name = name
            self.slot_start = slot_start
            self.slot_end = slot_end
            self.max_matches = max_matches
            self.maps = list(maps or DEFAULT_MATCH_MAPS)
            picker = discord.ui.RoleSelect(
                placeholder="Select the Pending Captain Role",
                min_values=1,
                max_values=1,
            )

            async def picker_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                role = picker.values[0]
                if role.is_default() or role.managed:
                    await interaction.response.send_message(
                        "Select a regular server role, not @everyone or a managed role.",
                        ephemeral=True,
                    )
                    return
                config = repository.get_server_config(self.guild_id)
                if (
                    config is not None
                    and config.staff_role_id is not None
                    and role.id == config.staff_role_id
                ):
                    await interaction.response.send_message(
                        "The Pending Captain Role must differ from the server administration role configured with `!set`.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.edit_message(
                    content=(
                        f"Pending Captain Role selected: {role.mention}\n"
                        "Now select the Confirmed Captain Role."
                    ),
                    view=ConfirmedRoleView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.name,
                        role.id,
                        self.slot_start,
                        self.slot_end,
                        self.max_matches,
                        self.maps,
                    ),
                )

            picker.callback = picker_callback
            self.add_item(picker)

    class ConfirmedRoleView(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            name: str,
            pending_role_id: int,
            slot_start: int = DEFAULT_SLOT_START,
            slot_end: int = DEFAULT_SLOT_END,
            max_matches: int = len(DEFAULT_MATCH_MAPS),
            maps: list[str] | None = None,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.name = name
            self.pending_role_id = pending_role_id
            self.slot_start = slot_start
            self.slot_end = slot_end
            self.max_matches = max_matches
            self.maps = list(maps or DEFAULT_MATCH_MAPS)
            picker = discord.ui.RoleSelect(
                placeholder="Select the Confirmed Captain Role",
                min_values=1,
                max_values=1,
            )

            async def picker_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                role = picker.values[0]
                config = repository.get_server_config(self.guild_id)
                if (
                    role.is_default()
                    or role.managed
                    or (
                        config is not None
                        and config.staff_role_id is not None
                        and role.id == config.staff_role_id
                    )
                    or role.id == self.pending_role_id
                ):
                    await interaction.response.send_message(
                        "Select a regular role different from the Staff and Pending Captain roles.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.edit_message(
                    content=(
                        f"Confirmed Captain Role selected: {role.mention}\n"
                        "Now select the public text channel for slots."
                    ),
                    view=PublicChannelView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.name,
                        self.pending_role_id,
                        role.id,
                        self.slot_start,
                        self.slot_end,
                        self.max_matches,
                        self.maps,
                    ),
                )

            picker.callback = picker_callback
            self.add_item(picker)

    class PublicChannelView(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            name: str,
            pending_role_id: int | None = None,
            confirmed_role_id: int | None = None,
            slot_start: int = DEFAULT_SLOT_START,
            slot_end: int = DEFAULT_SLOT_END,
            max_matches: int = len(DEFAULT_MATCH_MAPS),
            maps: list[str] | None = None,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.name = name
            self.pending_role_id = pending_role_id
            self.confirmed_role_id = confirmed_role_id
            self.slot_start = slot_start
            self.slot_end = slot_end
            self.max_matches = max_matches
            self.maps = list(maps or DEFAULT_MATCH_MAPS)
            picker = discord.ui.ChannelSelect(
                placeholder="Select the public text channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )

            async def picker_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                channel = picker.values[0]
                resolved = interaction.guild.get_channel(channel.id)
                if not isinstance(resolved, discord.TextChannel):
                    await interaction.response.send_message(
                        "Select an existing text channel in this server.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.edit_message(
                    content=(
                        f"Public channel selected: {resolved.mention}\n"
                        "Now select a distinct staff text channel."
                    ),
                    view=StaffChannelView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.name,
                        resolved.id,
                        self.pending_role_id,
                        self.confirmed_role_id,
                        self.slot_start,
                        self.slot_end,
                        self.max_matches,
                        self.maps,
                    ),
                )

            picker.callback = picker_callback
            self.add_item(picker)

    class StaffChannelView(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            name: str,
            public_channel_id: int,
            pending_role_id: int | None = None,
            confirmed_role_id: int | None = None,
            slot_start: int = DEFAULT_SLOT_START,
            slot_end: int = DEFAULT_SLOT_END,
            max_matches: int = len(DEFAULT_MATCH_MAPS),
            maps: list[str] | None = None,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.name = name
            self.public_channel_id = public_channel_id
            self.pending_role_id = pending_role_id
            self.confirmed_role_id = confirmed_role_id
            self.slot_start = slot_start
            self.slot_end = slot_end
            self.max_matches = max_matches
            self.maps = list(maps or DEFAULT_MATCH_MAPS)
            picker = discord.ui.ChannelSelect(
                placeholder="Select the staff text channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )

            async def picker_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                guild = interaction.guild
                selected = picker.values[0]
                public_channel = guild.get_channel(self.public_channel_id)
                staff_channel = guild.get_channel(selected.id)
                if not isinstance(public_channel, discord.TextChannel):
                    await interaction.response.edit_message(
                        content=(
                            "The selected public channel no longer exists. "
                            "Start again with **Add scrim**."
                        ),
                        view=None,
                    )
                    return
                if not isinstance(staff_channel, discord.TextChannel):
                    await interaction.response.send_message(
                        "Select an existing text channel in this server.",
                        ephemeral=True,
                    )
                    return
                if staff_channel.id == public_channel.id:
                    await interaction.response.send_message(
                        "The public and staff channels must be different.",
                        ephemeral=True,
                    )
                    return
                permission_error = _channel_permissions_error(
                    guild, public_channel, staff_channel
                )
                if permission_error:
                    await interaction.response.send_message(
                        permission_error, ephemeral=True
                    )
                    return

                await interaction.response.edit_message(
                    content=(
                        f"Public channel selected: {public_channel.mention}\n"
                        f"Staff channel selected: {staff_channel.mention}\n"
                        "Now select the logs channel."
                    ),
                    view=LogsChannelView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.name,
                        self.pending_role_id,
                        self.confirmed_role_id,
                        public_channel.id,
                        staff_channel.id,
                        self.slot_start,
                        self.slot_end,
                        self.max_matches,
                        self.maps,
                    ),
                )

            picker.callback = picker_callback
            self.add_item(picker)

    class LogsChannelView(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            name: str,
            pending_role_id: int,
            confirmed_role_id: int,
            public_channel_id: int,
            staff_channel_id: int,
            slot_start: int = DEFAULT_SLOT_START,
            slot_end: int = DEFAULT_SLOT_END,
            max_matches: int = len(DEFAULT_MATCH_MAPS),
            maps: list[str] | None = None,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.name = name
            self.pending_role_id = pending_role_id
            self.confirmed_role_id = confirmed_role_id
            self.public_channel_id = public_channel_id
            self.staff_channel_id = staff_channel_id
            self.slot_start = slot_start
            self.slot_end = slot_end
            self.max_matches = max_matches
            self.maps = list(maps or DEFAULT_MATCH_MAPS)
            picker = discord.ui.ChannelSelect(
                placeholder="Select the logs text channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )

            async def picker_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                guild = interaction.guild
                logs_channel = guild.get_channel(picker.values[0].id)
                public_channel = guild.get_channel(self.public_channel_id)
                staff_channel = guild.get_channel(self.staff_channel_id)
                if not all(
                    isinstance(channel, discord.TextChannel)
                    for channel in (public_channel, staff_channel, logs_channel)
                ):
                    await interaction.response.send_message(
                        "Select existing text channels in this server.", ephemeral=True
                    )
                    return
                if len(
                    {public_channel.id, staff_channel.id, logs_channel.id}
                ) != 3:
                    await interaction.response.send_message(
                        "Public, staff, and logs channels must be different.",
                        ephemeral=True,
                    )
                    return
                permission_error = _channel_permissions_error(
                    guild,
                    public_channel,
                    staff_channel,
                    (logs_channel, "logs"),
                )
                if permission_error:
                    await interaction.response.send_message(
                        permission_error, ephemeral=True
                    )
                    return
                await interaction.response.edit_message(
                    content=(
                        f"Logs channel selected: {logs_channel.mention}\n"
                        "Now select the Scrims History archive channel."
                    ),
                    view=HistoryChannelView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.name,
                        self.pending_role_id,
                        self.confirmed_role_id,
                        self.public_channel_id,
                        self.staff_channel_id,
                        logs_channel.id,
                        self.slot_start,
                        self.slot_end,
                        self.max_matches,
                        self.maps,
                    ),
                )

            picker.callback = picker_callback
            self.add_item(picker)

    class HistoryChannelView(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            name: str,
            pending_role_id: int,
            confirmed_role_id: int,
            public_channel_id: int,
            staff_channel_id: int,
            logs_channel_id: int,
            slot_start: int = DEFAULT_SLOT_START,
            slot_end: int = DEFAULT_SLOT_END,
            max_matches: int = len(DEFAULT_MATCH_MAPS),
            maps: list[str] | None = None,
        ):
            super().__init__(owner_id, guild_id)
            self.panel = panel
            self.name = name
            self.pending_role_id = pending_role_id
            self.confirmed_role_id = confirmed_role_id
            self.public_channel_id = public_channel_id
            self.staff_channel_id = staff_channel_id
            self.logs_channel_id = logs_channel_id
            self.slot_start = slot_start
            self.slot_end = slot_end
            self.max_matches = max_matches
            self.maps = list(maps or DEFAULT_MATCH_MAPS)
            self.history_channel_id: int | None = None
            picker = discord.ui.ChannelSelect(
                placeholder="Select the Scrims History text channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )
            cap_picker = discord.ui.ChannelSelect(
                placeholder="Select the designated !cap text channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )

            async def cap_picker_callback(
                interaction: discord.Interaction,
            ) -> None:
                if not await self.interaction_check(interaction):
                    return
                guild = interaction.guild
                cap_channel = guild.get_channel(cap_picker.values[0].id)
                if not isinstance(cap_channel, discord.TextChannel):
                    await interaction.response.send_message(
                        "Select an existing text channel in this server.",
                        ephemeral=True,
                    )
                    return
                if cap_channel.id in {
                    self.public_channel_id,
                    self.staff_channel_id,
                    self.logs_channel_id,
                    self.history_channel_id,
                }:
                    await interaction.response.send_message(
                        "The !cap channel must be different from the public, staff, logs, and history channels.",
                        ephemeral=True,
                    )
                    return
                member = guild.me
                permissions = cap_channel.permissions_for(member) if member else None
                required = (
                    "view_channel",
                    "send_messages",
                    "read_message_history",
                    "manage_messages",
                )
                missing = [
                    permission.replace("_", " ").title()
                    for permission in required
                    if permissions is None
                    or not getattr(permissions, permission, False)
                ]
                if missing:
                    await interaction.response.send_message(
                        f"I am missing {', '.join(missing)} in {cap_channel.mention}.",
                        ephemeral=True,
                    )
                    return
                self.cap_channel_id = cap_channel.id
                await create_callback(interaction)

            async def create_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                if (
                    self.history_channel_id is None
                    or self.cap_channel_id is None
                ):
                    await interaction.response.send_message(
                        "Select the History and designated !cap channels before completing scrim setup.",
                        ephemeral=True,
                    )
                    return
                guild = interaction.guild
                history_channel = guild.get_channel(self.history_channel_id)
                cap_channel = guild.get_channel(self.cap_channel_id)
                channels = [
                    guild.get_channel(channel_id)
                    for channel_id in (
                        self.public_channel_id,
                        self.staff_channel_id,
                        self.logs_channel_id,
                    )
                ]
                if (
                    not isinstance(history_channel, discord.TextChannel)
                    or not isinstance(cap_channel, discord.TextChannel)
                    or not all(
                        isinstance(channel, discord.TextChannel)
                        for channel in channels
                    )
                ):
                    await interaction.response.send_message(
                        "Select existing text channels in this server.", ephemeral=True
                    )
                    return
                all_channels = [*channels, history_channel, cap_channel]
                if len({channel.id for channel in all_channels}) != 5:
                    await interaction.response.send_message(
                        "Public, staff, logs, history, and !cap channels must be different.",
                        ephemeral=True,
                    )
                    return
                permission_error = _channel_permissions_error(
                    guild,
                    channels[0],
                    channels[1],
                    (channels[2], "logs"),
                    (history_channel, "history"),
                    (cap_channel, "!cap"),
                )
                if permission_error:
                    await interaction.response.send_message(
                        permission_error, ephemeral=True
                    )
                    return

                legacy = getattr(repository, "legacy", None)
                legacy_board = (
                    legacy.get("public_board") if isinstance(legacy, dict) else None
                )
                adopted_legacy = (
                    isinstance(legacy_board, dict)
                    and legacy_board.get("channel_id") == self.public_channel_id
                )
                config = repository.get_server_config(self.guild_id)
                if config is None or config.staff_role_id is None:
                    await interaction.response.send_message(
                        "The global Staff role is unavailable. Run `!set <@Role>` first.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.defer()
                try:
                    scrim = repository.create(
                        self.guild_id,
                        self.name,
                        self.public_channel_id,
                        self.staff_channel_id,
                        staff_role_id=config.staff_role_id,
                        pending_role_id=self.pending_role_id,
                        confirmed_role_id=self.confirmed_role_id,
                        cap_channel_id=self.cap_channel_id,
                        logs_channel_id=self.logs_channel_id,
                        history_channel_id=history_channel.id,
                        is_open=False,
                        slot_start=self.slot_start,
                        slot_end=self.slot_end,
                        max_matches=self.max_matches,
                        maps=self.maps,
                    )
                except ValueError:
                    await interaction.edit_original_response(
                        content=(
                            "The scrim was not created. Its name or channels conflict "
                            "with another scrim, or this server has reached its limit."
                        ),
                        view=None,
                    )
                    return
                except SlotStorageError:
                    await interaction.edit_original_response(
                        content=(
                            "The scrim could not be saved. Nothing was created. "
                            "Please try again."
                        ),
                        view=None,
                    )
                    return

                try:
                    published = await publish_scrim(scrim)
                except Exception as error:
                    logger.exception(
                        "Scrim saved but initial board publish failed", exc_info=error
                    )
                    published = False
                panel_refreshed = await self.panel.refresh_message()
                result = (
                    f"Scrim **{_safe_name(scrim.name)}** was saved in a closed state "
                    f"with its slot board in <#{scrim.public_channel_id}>."
                    if published
                    else (
                        f"Scrim **{_safe_name(scrim.name)}** was saved, but its slot "
                        "board could not be published. Check my permissions and use "
                        "the setup panel again."
                    )
                )
                if adopted_legacy:
                    result += " Existing legacy slots were preserved."
                if not panel_refreshed:
                    result += " Reopen `!setup` to refresh the panel."
                if log_action is not None:
                    try:
                        await log_action(
                            scrim,
                            "SCRIM CREATED",
                            _scrim_configuration_details(scrim, repository),
                        )
                    except Exception:
                        logger.exception("Could not write scrim creation log")
                await interaction.edit_original_response(content=result, view=None)

            async def picker_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                guild = interaction.guild
                history_channel = guild.get_channel(picker.values[0].id)
                channels = [
                    guild.get_channel(channel_id)
                    for channel_id in (
                        self.public_channel_id,
                        self.staff_channel_id,
                        self.logs_channel_id,
                    )
                ]
                if (
                    not isinstance(history_channel, discord.TextChannel)
                    or not all(
                        isinstance(channel, discord.TextChannel)
                        for channel in channels
                    )
                ):
                    await interaction.response.send_message(
                        "Select existing text channels in this server.", ephemeral=True
                    )
                    return
                all_channels = [*channels, history_channel]
                if len({channel.id for channel in all_channels}) != 4:
                    await interaction.response.send_message(
                        "Public, staff, logs, and history channels must be different.",
                        ephemeral=True,
                    )
                    return
                permission_error = _channel_permissions_error(
                    guild,
                    channels[0],
                    channels[1],
                    (channels[2], "logs"),
                    (history_channel, "history"),
                )
                if permission_error:
                    await interaction.response.send_message(
                        permission_error, ephemeral=True
                    )
                    return

                self.history_channel_id = history_channel.id
                self.clear_items()
                self.add_item(cap_picker)
                await interaction.response.edit_message(
                    content=(
                        f"History channel selected: {history_channel.mention}\n"
                        "Now select the designated !cap channel."
                    ),
                    view=self,
                )

            picker.callback = picker_callback
            cap_picker.callback = cap_picker_callback
            self.add_item(picker)

    class LegacyScrimEditView(BoundView):
        def __init__(
            self,
            panel: "SetupPanel",
            owner_id: int,
            guild_id: int,
            scrim_id: str,
        ):
            super().__init__(owner_id, guild_id, timeout=600)
            self.panel = panel
            self.scrim_id = scrim_id

            async def edit_name(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    scrim = repository.get(self.scrim_id)
                    if scrim is None:
                        await interaction.response.send_message(
                            "That scrim no longer exists.", ephemeral=True
                        )
                    else:
                        await interaction.response.send_modal(
                            ScrimNameEditModal(self)
                        )

            async def edit_captain_role(
                interaction: discord.Interaction, field: str, label: str
            ) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.send_message(
                        f"Select the {label} for this scrim.",
                        view=ScrimRolePicker(self, field, label),
                        ephemeral=True,
                    )

            async def edit_range(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    scrim = repository.get(self.scrim_id)
                    if scrim is None:
                        await interaction.response.send_message(
                            "That scrim no longer exists.", ephemeral=True
                        )
                    else:
                        await interaction.response.send_modal(
                            SlotRangeModal(self)
                        )

            async def edit_matches(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    scrim = repository.get(self.scrim_id)
                    if scrim is None:
                        await interaction.response.send_message(
                            "That scrim no longer exists.", ephemeral=True
                        )
                    else:
                        await interaction.response.send_modal(
                            MatchConfigModal(self)
                        )

            async def edit_channel(
                interaction: discord.Interaction, field: str, label: str
            ) -> None:
                if await self.interaction_check(interaction):
                    await interaction.response.send_message(
                        f"Select the {label} channel.",
                        view=ScrimChannelPicker(self, field),
                        ephemeral=True,
                    )

            async def show_slots(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                scrim = repository.get(self.scrim_id)
                if scrim is None or scrim.guild_id != self.guild_id:
                    await interaction.response.send_message(
                        "That scrim no longer exists.", ephemeral=True
                    )
                    return
                await interaction.response.defer(ephemeral=True)
                try:
                    published = await publish_scrim(scrim)
                except Exception as error:
                    logger.exception("Could not publish scrim board", exc_info=error)
                    published = False
                if published:
                    text = (
                        f"The slot board for **{_safe_name(scrim.name)}** was "
                        f"published or refreshed in <#{scrim.public_channel_id}>."
                    )
                else:
                    text = (
                        f"The slot board for **{_safe_name(scrim.name)}** could not "
                        f"be published in <#{scrim.public_channel_id}>. Check the "
                        "channel and my permissions, then try again."
                    )
                await interaction.followup.send(text, ephemeral=True)

            async def configure_idpw(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                scrim = repository.get(self.scrim_id)
                if scrim is None or scrim.guild_id != self.guild_id:
                    await interaction.response.send_message(
                        "That scrim no longer exists.", ephemeral=True
                    )
                    return
                idpw_view = IdPwConfigView(
                    self.panel, self.owner_id, self.guild_id, scrim
                )
                await interaction.response.send_message(
                    idpw_view.content(),
                    view=idpw_view,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            async def configure_emojis(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                scrim = repository.get(self.scrim_id)
                if scrim is None or scrim.guild_id != self.guild_id:
                    await interaction.response.send_message(
                        "That scrim no longer exists.", ephemeral=True
                    )
                    return
                await interaction.response.edit_message(
                    content=custom_emoji_content(scrim),
                    view=CustomEmojiMenuView(
                        self.panel, self.owner_id, self.guild_id, self.scrim_id
                    ),
                )

            name_button = discord.ui.Button(
                label="Scrim name", emoji="✏️", style=discord.ButtonStyle.primary, row=0
            )
            pending_role_button = discord.ui.Button(
                label="Pending Captain Role", emoji="🎭",
                style=discord.ButtonStyle.secondary, row=0
            )
            confirmed_role_button = discord.ui.Button(
                label="Confirmed Captain Role", emoji="🎭",
                style=discord.ButtonStyle.secondary, row=0
            )
            cap_channel_button = discord.ui.Button(
                label="Set !cap Channel",
                emoji="⚙️",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            public_button = discord.ui.Button(
                label="Public channel", emoji="📣",
                style=discord.ButtonStyle.secondary, row=1
            )
            staff_button = discord.ui.Button(
                label="Staff channel", emoji="🔒",
                style=discord.ButtonStyle.secondary, row=1
            )
            logs_button = discord.ui.Button(
                label="Logs channel", emoji="📝",
                style=discord.ButtonStyle.secondary, row=2
            )
            history_button = discord.ui.Button(
                label="History channel", emoji="🗄️",
                style=discord.ButtonStyle.secondary, row=2
            )
            range_button = discord.ui.Button(
                label="Slot range", emoji="🔢",
                style=discord.ButtonStyle.secondary, row=3
            )
            matches_button = discord.ui.Button(
                label="Games & maps", emoji="🎮",
                style=discord.ButtonStyle.secondary, row=3
            )
            show_button = discord.ui.Button(
                label="Show/refresh slots",
                emoji="🔄",
                style=discord.ButtonStyle.primary,
                row=4,
            )
            idpw_button = discord.ui.Button(
                label="ID/PW",
                emoji="🔐",
                style=discord.ButtonStyle.secondary,
                row=4,
            )
            emojis_button = discord.ui.Button(
                label="Custom Emojis",
                emoji="🎨",
                style=discord.ButtonStyle.secondary,
                row=4,
            )
            back_button = discord.ui.Button(
                label="Back to Management",
                emoji="↩️",
                style=discord.ButtonStyle.secondary,
                row=4,
            )

            name_button.callback = edit_name
            pending_role_button.callback = lambda interaction: edit_captain_role(
                interaction, "pending_role_id", "Pending Captain Role"
            )
            confirmed_role_button.callback = lambda interaction: edit_captain_role(
                interaction, "confirmed_role_id", "Confirmed Captain Role"
            )
            public_button.callback = lambda interaction: edit_channel(
                interaction, "public_channel_id", "public"
            )
            staff_button.callback = lambda interaction: edit_channel(
                interaction, "staff_channel_id", "staff"
            )
            logs_button.callback = lambda interaction: edit_channel(
                interaction, "logs_channel_id", "logs"
            )
            history_button.callback = lambda interaction: edit_channel(
                interaction, "history_channel_id", "history"
            )
            cap_channel_button.callback = lambda interaction: edit_channel(
                interaction, "cap_channel_id", "!cap"
            )
            range_button.callback = edit_range
            matches_button.callback = edit_matches
            show_button.callback = show_slots
            idpw_button.callback = configure_idpw
            emojis_button.callback = configure_emojis

            async def back_callback(interaction: discord.Interaction) -> None:
                if await self.interaction_check(interaction):
                    management = ScrimManagementView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.scrim_id,
                    )
                    await interaction.response.edit_message(
                        content=management.content(),
                        embed=management.embed(),
                        view=management,
                    )

            back_button.callback = back_callback
            for item in (
                name_button,
                pending_role_button,
                confirmed_role_button,
                cap_channel_button,
                public_button,
                staff_button,
                logs_button,
                history_button,
                range_button,
                matches_button,
                show_button,
                idpw_button,
                emojis_button,
                back_button,
            ):
                self.add_item(item)

        @staticmethod
        def content(scrim: Any) -> str:
            return (
                f"**Edit scrim — {_safe_name(scrim.name)}**\n"
                f"**Pending Captain Role:** "
                f"{f'<@&{scrim.pending_role_id}>' if scrim.pending_role_id else 'not configured'}\n"
                f"**Confirmed Captain Role:** "
                f"{f'<@&{scrim.confirmed_role_id}>' if scrim.confirmed_role_id else 'not configured'}\n"
                f"!cap channel: {_channel_ref(scrim.cap_channel_id)}\n"
                f"Public channel: {_channel_ref(scrim.public_channel_id)}\n"
                f"Staff channel: {_channel_ref(scrim.staff_channel_id)}\n"
                f"Logs channel: {_channel_ref(scrim.logs_channel_id)}\n"
                f"History channel: {_channel_ref(scrim.history_channel_id)}\n\n"
                f"Slots: {scrim.slot_start:02d}–{scrim.slot_end:02d} "
                f"({len(scrim.slots)} total)\n"
                f"Games: {scrim.max_matches}\n"
                f"Maps: {', '.join(scrim.maps)}\n\n"
                "Choose one setting to change. Each change is saved immediately."
            )

    class ScrimRolePicker(BoundView):
        def __init__(self, edit_view: ScrimEditView, field: str, label: str):
            super().__init__(
                edit_view.owner_id, edit_view.guild_id, timeout=300
            )
            self.edit_view = edit_view
            self.field = field
            picker = discord.ui.RoleSelect(
                placeholder=f"Select the {label}",
                min_values=1,
                max_values=1,
            )

            async def callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                role = picker.values[0]
                if role.is_default() or role.managed:
                    await _send_error(
                        interaction,
                        ValueError(
                            "Select a regular server role, not @everyone or a managed role."
                        ),
                    )
                    return
                scrim = repository.get(self.edit_view.scrim_id)
                config = repository.get_server_config(self.edit_view.guild_id)
                if (
                    scrim is not None
                    and config is not None
                    and config.staff_role_id is not None
                    and role.id == config.staff_role_id
                ):
                    await _send_error(
                        interaction,
                        ValueError(
                            "The Captain roles must differ from the server administration role configured with `!set`."
                        ),
                    )
                    return
                if (
                    scrim is not None
                    and self.field == "pending_role_id"
                    and scrim.confirmed_role_id == role.id
                ) or (
                    scrim is not None
                    and self.field == "confirmed_role_id"
                    and scrim.pending_role_id == role.id
                ):
                    await _send_error(
                        interaction,
                        ValueError("Pending and Confirmed Captain roles must be different."),
                    )
                    return
                await interaction.response.defer()
                await apply_scrim_change(
                    interaction,
                    self.edit_view.panel,
                    self.edit_view.scrim_id,
                    changes={self.field: role.id},
                )

            picker.callback = callback
            self.add_item(picker)

    class ScrimChannelPicker(BoundView):
        def __init__(self, edit_view: ScrimEditView, field: str):
            super().__init__(
                edit_view.owner_id, edit_view.guild_id, timeout=300
            )
            self.edit_view = edit_view
            self.field = field
            picker = discord.ui.ChannelSelect(
                placeholder=f"Select the {field.replace('_channel_id', '')} channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            )

            async def callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                channel = interaction.guild.get_channel(picker.values[0].id)
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message(
                        "Select an existing text channel in this server.",
                        ephemeral=True,
                    )
                    return
                member = interaction.guild.me
                permissions = channel.permissions_for(member) if member else None
                required = ("view_channel", "send_messages", "read_message_history")
                missing = [
                    permission.replace("_", " ").title()
                    for permission in required
                    if permissions is None or not getattr(permissions, permission, False)
                ]
                if self.field in {"public_channel_id", "cap_channel_id"} and (
                    permissions is None or not permissions.manage_messages
                ):
                    missing.append("Manage Messages")
                if missing:
                    await interaction.response.send_message(
                        f"I am missing {', '.join(missing)} in {channel.mention}.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.defer()
                await apply_scrim_change(
                    interaction,
                    self.edit_view.panel,
                    self.edit_view.scrim_id,
                    changes={self.field: channel.id},
                )

            picker.callback = callback
            self.add_item(picker)

    class ScrimNameEditModal(discord.ui.Modal, title="Edit scrim name"):
        name = discord.ui.TextInput(
            label="Scrim name",
            min_length=1,
            max_length=80,
        )

        def __init__(self, edit_view: ScrimEditView):
            super().__init__(timeout=300)
            self.edit_view = edit_view
            scrim = repository.get(edit_view.scrim_id)
            if scrim is not None:
                self.name.default = scrim.name

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not setup_authorized(
                interaction, self.edit_view.owner_id, self.edit_view.guild_id
            ):
                await _deny(interaction)
                return
            await apply_scrim_change(
                interaction,
                self.edit_view.panel,
                self.edit_view.scrim_id,
                changes={"name": str(self.name).strip()},
                response_mode="modal",
            )

        async def on_error(
            self, interaction: discord.Interaction, error: Exception
        ) -> None:
            await _send_error(interaction, error)

    class SlotRangeModal(discord.ui.Modal, title="Edit slot range"):
        slot_start = discord.ui.TextInput(
            label="First slot number",
            placeholder="Example: 01, 03, or 05",
            min_length=1,
            max_length=2,
        )
        slot_count = discord.ui.TextInput(
            label="Number of slots",
            placeholder="Example: 16, 23, or 25",
            min_length=1,
            max_length=2,
        )

        def __init__(self, edit_view: ScrimEditView):
            super().__init__(timeout=300)
            self.edit_view = edit_view
            scrim = repository.get(edit_view.scrim_id)
            if scrim is not None:
                self.slot_start.default = f"{scrim.slot_start:02d}"
                self.slot_count.default = str(scrim.slot_end - scrim.slot_start + 1)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not setup_authorized(
                interaction, self.edit_view.owner_id, self.edit_view.guild_id
            ):
                await _deny(interaction)
                return
            try:
                start, end = _slot_range_from_count(
                    self.slot_start, self.slot_count
                )
            except (TypeError, ValueError) as error:
                await interaction.response.send_message(
                    f"Invalid slot range. {error}",
                    ephemeral=True,
                )
                return
            await apply_scrim_change(
                interaction,
                self.edit_view.panel,
                self.edit_view.scrim_id,
                changes={"slot_start": start, "slot_end": end},
                response_mode="modal",
            )

        async def on_error(
            self, interaction: discord.Interaction, error: Exception
        ) -> None:
            await _send_error(interaction, error)

    class MatchConfigModal(discord.ui.Modal, title="Edit games and maps"):
        match_count = discord.ui.TextInput(
            label="Games amount",
            placeholder=f"1-{MAX_MATCHES}",
            min_length=1,
            max_length=2,
        )
        maps = discord.ui.TextInput(
            label="Map rotation",
            placeholder="One map per game, separated by commas",
            max_length=1000,
        )

        def __init__(self, edit_view: ScrimEditView):
            super().__init__(timeout=300)
            self.edit_view = edit_view
            scrim = repository.get(edit_view.scrim_id)
            if scrim is not None:
                self.match_count.default = str(scrim.max_matches)
                self.maps.default = ", ".join(scrim.maps)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not setup_authorized(
                interaction, self.edit_view.owner_id, self.edit_view.guild_id
            ):
                await _deny(interaction)
                return
            try:
                max_matches, maps = _match_configuration_from_values(
                    self.match_count, self.maps
                )
            except ValueError as error:
                await interaction.response.send_message(
                    f"Invalid games setup. {error}",
                    ephemeral=True,
                )
                return
            await apply_scrim_change(
                interaction,
                self.edit_view.panel,
                self.edit_view.scrim_id,
                changes={"max_matches": max_matches, "maps": maps},
                response_mode="modal",
            )

        async def on_error(
            self, interaction: discord.Interaction, error: Exception
        ) -> None:
            await _send_error(interaction, error)

    class ScrimNameModal(discord.ui.Modal, title="Add a scrim"):
        name = discord.ui.TextInput(
            label="Scrim name",
            placeholder="Example: Friday Night Scrim",
            min_length=1,
            max_length=80,
        )

        def __init__(self, panel: SetupPanel, owner_id: int, guild_id: int):
            super().__init__(timeout=300)
            self.panel = panel
            self.owner_id = owner_id
            self.guild_id = guild_id

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not setup_authorized(
                interaction, self.owner_id, self.guild_id
            ):
                await _deny(interaction)
                return
            name = str(self.name).strip()
            if not name:
                await interaction.response.send_message(
                    "The scrim name cannot be empty.", ephemeral=True
                )
                return
            try:
                scrims = repository.list(self.guild_id)
            except SlotStorageError as error:
                await _send_error(interaction, error)
                return
            if any(scrim.name.casefold() == name.casefold() for scrim in scrims):
                await interaction.response.send_message(
                    "A scrim with that name already exists in this server.",
                    ephemeral=True,
                )
                return
            if len(scrims) >= 25:
                await interaction.response.send_message(
                    "This server already has the maximum of 25 configured scrims.",
                    ephemeral=True,
                )
                return
            grid = ScrimSetupWizardView(
                self.panel,
                self.owner_id,
                self.guild_id,
                name=name,
            )
            await interaction.response.send_message(
                f"Configure **{_safe_name(name)}** in the setup wizard.",
                embed=grid.embed(),
                view=grid,
                ephemeral=True,
            )

        async def on_error(
            self, interaction: discord.Interaction, error: Exception
        ) -> None:
            await _send_error(interaction, error)

    class DeleteConfirmation(BoundView):
        def __init__(
            self,
            panel: SetupPanel,
            owner_id: int,
            guild_id: int,
            scrim_id: str,
        ):
            super().__init__(owner_id, guild_id, timeout=120)
            self.panel = panel
            self.scrim_id = scrim_id

            confirm = discord.ui.Button(
                label="Delete permanently", style=discord.ButtonStyle.danger
            )
            cancel = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.secondary
            )

            def controls_are_on_setup_message(
                interaction: discord.Interaction,
            ) -> bool:
                return bool(
                    self.panel.message is not None
                    and interaction.message is not None
                    and self.panel.message.id == interaction.message.id
                )

            async def confirm_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                selected_scrim = repository.get(self.scrim_id)
                if (
                    selected_scrim is None
                    or selected_scrim.guild_id != self.guild_id
                ):
                    await interaction.response.edit_message(
                        content="That scrim no longer exists.", view=None
                    )
                    await self.panel.refresh_message()
                    return

                await interaction.response.defer()
                deleted = None
                cleanup_error: str | None = None
                try:
                    # Match every board/state mutation's lock order. Keeping the
                    # board lock through cleanup prevents an in-flight publisher
                    # or reset from touching a replacement board on this channel.
                    async with selected_scrim.board_lock:
                        async with selected_scrim.state_lock:
                            if not setup_authorized(
                                interaction, self.owner_id, self.guild_id
                            ):
                                await _deny(interaction)
                                return
                            current = repository.get(self.scrim_id)
                            if (
                                current is not selected_scrim
                                or current.guild_id != self.guild_id
                            ):
                                raise ValueError("Scrim identity vanished.")
                            # Durable deletion precedes best-effort Discord cleanup.
                            deleted = repository.delete(
                                self.scrim_id, self.guild_id
                            )

                        old_message = getattr(deleted, "runtime_message", None)
                        try:
                            if (
                                old_message is None
                                and deleted.public_message_id is not None
                            ):
                                channel = interaction.guild.get_channel(
                                    deleted.public_channel_id
                                )
                                if not isinstance(channel, discord.TextChannel):
                                    cleanup_error = (
                                        "the old public channel is unavailable"
                                    )
                                else:
                                    old_message = await channel.fetch_message(
                                        deleted.public_message_id
                                    )
                            if old_message is not None:
                                await old_message.edit(view=None)
                        except (
                            discord.NotFound,
                            discord.Forbidden,
                            discord.HTTPException,
                        ):
                            logger.exception(
                                "Could not remove deleted scrim board controls"
                            )
                            cleanup_error = (
                                "Discord would not let me remove the old controls"
                            )
                except ValueError:
                    await interaction.edit_original_response(
                        content="That scrim no longer exists.", view=None
                    )
                    await self.panel.refresh_message()
                    return
                except SlotStorageError:
                    await interaction.edit_original_response(
                        content=(
                            "The scrim could not be deleted from storage. "
                            "Nothing was changed."
                        ),
                        view=None,
                    )
                    return

                if self.panel.selected_id == self.scrim_id:
                    self.panel.selected_id = None
                panel_refreshed = await self.panel.refresh_message()
                result = (
                    f"Scrim **{_safe_name(deleted.name)}** and its saved slot data "
                    "were deleted. Its Discord channels were not deleted."
                )
                if cleanup_error:
                    result += (
                        f" The scrim is deleted, but {cleanup_error}; any old board "
                        "controls are no longer valid."
                    )
                if not panel_refreshed:
                    result += " Reopen `!setup` to refresh the management panel."
                if log_action is not None:
                    try:
                        await log_action(
                            deleted,
                            "SCRIM DELETED",
                            "The scrim configuration and saved slot data were deleted by an administrator.",
                        )
                    except Exception:
                        logger.exception("Could not write scrim deletion log")
                if controls_are_on_setup_message(interaction):
                    # The panel was already refreshed above. Do not overwrite it
                    # with the deletion result; report that result privately.
                    await interaction.followup.send(result, ephemeral=True)
                else:
                    await interaction.edit_original_response(content=result, view=None)

            async def cancel_callback(interaction: discord.Interaction) -> None:
                if not await self.interaction_check(interaction):
                    return
                if controls_are_on_setup_message(interaction):
                    selected = repository.get(self.scrim_id)
                    if selected is None or selected.guild_id != self.guild_id:
                        await interaction.response.edit_message(
                            content="That scrim no longer exists.",
                            embed=None,
                            view=None,
                        )
                        await self.panel.refresh_message()
                        return
                    management = ScrimManagementView(
                        self.panel,
                        self.owner_id,
                        self.guild_id,
                        self.scrim_id,
                    )
                    await interaction.response.edit_message(
                        content=management.content(),
                        embed=management.embed(),
                        view=management,
                    )
                    return
                await interaction.response.edit_message(
                    content="Deletion cancelled. Nothing was changed.", view=None
                )

            confirm.callback = confirm_callback
            cancel.callback = cancel_callback
            self.add_item(confirm)
            self.add_item(cancel)

    async def start_scrim_wizard(interaction: discord.Interaction) -> None:
        """Launch the scrim wizard from another setup flow."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "This setup wizard can only be used in a server.", ephemeral=True
            )
            return
        panel = SetupPanel(interaction.user.id, interaction.guild.id)
        await interaction.response.send_modal(
            ScrimNameModal(panel, interaction.user.id, interaction.guild.id)
        )

    bot.start_scrim_wizard = start_scrim_wizard

    @bot.command(name="setup")
    @commands.guild_only()
    async def setup_command(ctx: commands.Context[commands.Bot]) -> None:
        """Open the management panel bound to the invoking staff member."""
        try:
            authorized = user_is_staff(ctx.author, ctx.guild.id)
        except SlotStorageError:
            logger.exception("Could not read scrim configuration")
            await _delete_command_message(ctx)
            await ctx.send(
                "The scrim configuration could not be read. "
                "Nothing was changed. Please try again.",
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not authorized:
            await _delete_command_message(ctx)
            await ctx.send(
                "You do not have the configured Staff role to use `!setup`.",
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            panel = SetupPanel(ctx.author.id, ctx.guild.id)
        except SlotStorageError:
            logger.exception("Could not read scrim configuration")
            await _delete_command_message(ctx)
            await ctx.send(
                "The scrim configuration could not be read. "
                "Nothing was changed. Please try again.",
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        panel.message = await ctx.send(
            panel.content(),
            embed=panel.embed(),
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )