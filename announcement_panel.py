"""Owner-only, confirmation-gated announcements to configured scrim log channels."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

logger = logging.getLogger("pung-scrim-bot.announcements")

ANNOUNCEMENT_AUDIENCES = {
    "Standard": frozenset({"Standard"}),
    "Gold": frozenset({"Standard", "Gold"}),
    "Diamond": frozenset({"Standard", "Gold", "Diamond"}),
}
ANNOUNCEMENT_COLORS = {
    "Standard": discord.Color.green(),
    "Gold": discord.Color.gold(),
    "Diamond": discord.Color.purple(),
}
MAX_ANNOUNCEMENT_LENGTH = 4000
V19_UPDATE_LOG_TEXT = """# ARC BOT V1.9 Update Log

• Screenshot scanning: Added staff-only OCR testing with checks for mismatched screenshots and conflicting results. Scanned scores stay in review and are never saved automatically.

• Team registration: Duplicate team names are declined. If different teams share a tag, the new request goes to staff review—even when auto-accept is on.

• Slot management: Improved the commands for confirming, removing, moving, and switching teams, with safeguards against outdated actions and occupied slots.

• Gold customization: Gold servers can customize emojis. Standard servers use the defaults, while saved custom settings are kept if Gold is enabled again.

• Matches & Maps: Fixed custom-map saving when there are fewer maps than matches. New scrims start without a configured map rotation.

• ID/PW: Regular !idpw now shows Next Match, ID, PW, Start Time, and the role mention—without a map or match-counter change. Numbered !idpwgN remains map-specific.

The finalized changes were uploaded to both the Beta and Public GitHub branches."""


def eligible_license_types(announcement_tier: str) -> frozenset[str]:
    """Return the license tiers included by an announcement tier."""
    try:
        return ANNOUNCEMENT_AUDIENCES[announcement_tier]
    except KeyError as error:
        raise ValueError("Choose Standard, Gold, or Diamond.") from error


def parse_server_selection(value: str, eligible_ids: set[int]) -> list[int]:
    """Parse 'all' or an explicit comma/newline/space-separated guild ID list."""
    tokens = [part for part in re.split(r"[\s,;]+", value.strip()) if part]
    if not tokens:
        raise ValueError("Enter `all` or at least one eligible server ID.")
    if len(tokens) == 1 and tokens[0].casefold() == "all":
        return sorted(eligible_ids)
    if any(token.casefold() == "all" for token in tokens):
        raise ValueError("Use `all` by itself, or enter individual server IDs.")
    if any(not token.isdigit() for token in tokens):
        raise ValueError("Server IDs must contain digits only.")

    selected = [int(token) for token in tokens]
    if len(selected) != len(set(selected)):
        raise ValueError("Remove duplicate server IDs from the selection.")
    ineligible = sorted(set(selected) - eligible_ids)
    if ineligible:
        raise ValueError(
            "These servers are not eligible for this announcement or are not "
            "authorized in this bot: " + ", ".join(str(guild_id) for guild_id in ineligible)
        )
    return sorted(selected)


def announcement_marker(tier: str, guild_id: int, body: str) -> str:
    """Stable marker used to recognize repeat announcements in recent history."""
    digest = hashlib.sha256(
        f"{tier}\0{guild_id}\0{body.strip()}".encode("utf-8")
    ).hexdigest()[:20]
    return f"ARC announcement ref {digest}"


def recipient_is_currently_eligible(repository, guild_id: int, tier: str) -> bool:
    """Check each recipient against the bot's locally persisted authorization."""
    return (
        repository.is_guild_authorized(guild_id)
        and repository.get_server_license_type(guild_id)
        in eligible_license_types(tier)
    )


@dataclass(frozen=True)
class AnnouncementTarget:
    guild_id: int
    guild_name: str
    license_type: str
    channel: Any | None
    channel_name: str | None
    error: str | None = None


@dataclass(frozen=True)
class AnnouncementConfirmation:
    """Immutable copy of the exact announcement and destinations reviewed."""

    revision: int
    tier: str
    body: str
    guild_ids: tuple[int, ...]
    destinations: tuple[tuple[int, int | None], ...]


def _selected_logs_channel(repository, guild_id: int):
    """Prefer the server-wide logs override, then the first configured scrim log."""
    scrims = repository.list(guild_id)
    if not scrims:
        return None, None, "No active scrim has a configured logs channel."

    config = repository.get_server_config(guild_id)
    configured_id = getattr(config, "logs_channel_id", None)
    if configured_id is not None:
        return scrims[0], configured_id, None

    for scrim in scrims:
        channel_id = getattr(scrim, "logs_channel_id", None)
        if channel_id is not None:
            return scrim, channel_id, None
    return None, None, "No active scrim has a configured logs channel."


class AnnouncementWizard(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        bot,
        repository,
        configured_text_channel,
    ) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.bot = bot
        self.repository = repository
        self.configured_text_channel = configured_text_channel
        self.message: discord.Message | None = None
        self.tier: str | None = None
        self.guild_ids: list[int] = []
        self.body: str | None = None
        self.preview_targets: list[AnnouncementTarget] = []
        self.send_in_progress = False
        self.completed_guild_ids: set[int] = set()
        self.retry_guild_ids: set[int] = set()
        self.confirm_button: discord.ui.Button | None = None
        self.revision = 0
        self.reviewed_snapshot: AnnouncementConfirmation | None = None
        self.confirmed_snapshot: AnnouncementConfirmation | None = None
        self.confirmation_started = False
        self.add_tier_selector()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This announcement panel belongs to another user.",
                ephemeral=True,
            )
            return False
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "Only the bot owner can send announcements.",
                ephemeral=True,
            )
            return False
        if self.confirmation_started:
            custom_id = (
                (getattr(interaction, "data", None) or {}).get("custom_id")
            )
            if self.send_in_progress or custom_id not in {
                "arc_announcement_confirm",
                "arc_announcement_retry",
                "arc_announcement_close",
            }:
                await interaction.response.send_message(
                    "This announcement has already been confirmed. Start "
                    "`!staffannounce` again to change its text or recipients.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return False
        return True

    async def authorize_modal_submission(
        self,
        interaction: discord.Interaction,
        modal_revision: int,
    ) -> bool:
        if (
            interaction.user.id == self.owner_id
            and await self.bot.is_owner(interaction.user)
            and not self.confirmation_started
            and not self.send_in_progress
            and modal_revision == self.revision
        ):
            self.revision += 1
            self.reviewed_snapshot = None
            return True
        await interaction.response.send_message(
            (
                "This modal is stale or confirmation has already started. "
                "No changes were applied; start `!staffannounce` again if "
                "you need to change the announcement."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def add_tier_selector(self) -> None:
        selector = discord.ui.Select(
            placeholder="Choose announcement tier...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=tier,
                    value=tier,
                    description=(
                        "Standard servers"
                        if tier == "Standard"
                        else "Standard and Gold servers"
                        if tier == "Gold"
                        else "Standard, Gold, and Diamond servers"
                    ),
                )
                for tier in ANNOUNCEMENT_AUDIENCES
            ],
        )

        async def choose_tier(interaction: discord.Interaction) -> None:
            if self.confirmation_started:
                await interaction.response.send_message(
                    "This announcement has already been confirmed.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self.tier = selector.values[0]
            self.revision += 1
            self.reviewed_snapshot = None
            await self.show_eligible_servers(
                interaction,
                self.tier,
                self.revision,
            )

        selector.callback = choose_tier
        self.add_item(selector)

        async def cancel(interaction: discord.Interaction) -> None:
            self.stop()
            await interaction.response.edit_message(
                content="Announcement cancelled.",
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        button.callback = cancel
        self.add_item(button)

    async def eligible_guilds(self, tier: str | None = None) -> dict[int, tuple[Any, str]]:
        selected_tier = tier or self.tier
        if selected_tier is None:
            return {}
        allowed = eligible_license_types(selected_tier)
        result = {}
        for guild_id, _, _ in self.repository.list_authorizations():
            license_type = self.repository.get_server_license_type(guild_id)
            if license_type not in allowed:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                result[guild_id] = (guild, license_type)
        return result

    async def show_eligible_servers(
        self,
        interaction: discord.Interaction,
        tier: str,
        expected_revision: int,
    ) -> None:
        await interaction.response.defer()
        if self.revision != expected_revision or self.confirmation_started:
            await interaction.followup.send(
                "This tier selection is stale. No server selection was opened.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        eligible = await self.eligible_guilds(tier)
        lines = [
            f"**Eligible {tier} announcement servers**",
            "Use `all` to choose every listed server, or enter specific IDs in "
            "the next step.",
        ]
        for guild_id, (guild, license_type) in eligible.items():
            lines.append(
                f"• {discord.utils.escape_markdown(guild.name)} "
                f"(`{guild_id}`) — {license_type}"
            )
        if not eligible:
            lines.append(
                "No active eligible servers for this bot are currently connected."
            )
        pages = self._chunk("\n".join(lines))
        for page in pages:
            await interaction.channel.send(
                page,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if self.revision != expected_revision or self.confirmation_started:
                await interaction.followup.send(
                    "The tier selection changed while the server list was "
                    "being prepared. No selection step was opened.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        self.clear_items()

        async def select_servers(button_interaction: discord.Interaction) -> None:
            if self.confirmation_started:
                await button_interaction.response.send_message(
                    "This announcement has already been confirmed.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await button_interaction.response.send_modal(
                SelectServersModal(
                    self,
                    modal_revision=self.revision,
                )
            )

        choose_button = discord.ui.Button(
            label="Choose target servers",
            style=discord.ButtonStyle.primary,
        )
        choose_button.callback = select_servers
        self.add_item(choose_button)
        self.add_cancel_button()
        await self._edit_anchor(
            f"**{tier} announcement** — {len(eligible)} eligible server(s). "
            "Review the server ID list sent above, then choose the exact "
            "destinations. Nothing has been sent.",
            embed=None,
        )

    async def set_selected_servers(
        self,
        interaction: discord.Interaction,
        raw_selection: str,
    ) -> None:
        expected_revision = self.revision
        await interaction.response.defer()
        if expected_revision != self.revision or self.confirmation_started:
            await interaction.followup.send(
                "This server-selection modal is stale; no destinations were "
                "changed. Reopen the selection step.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        eligible = await self.eligible_guilds()
        try:
            self.guild_ids = parse_server_selection(
                raw_selection,
                set(eligible),
            )
        except ValueError as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not self.guild_ids:
            await interaction.followup.send(
                "There are no active, eligible servers currently connected to "
                "this bot.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        names = [
            f"• {discord.utils.escape_markdown(eligible[guild_id][0].name)} "
            f"(`{guild_id}`) — {eligible[guild_id][1]}"
            for guild_id in self.guild_ids
        ]
        message_lines = [
            f"**{len(names)} target server(s) selected.**",
            "Next, add the announcement text. Nothing has been sent.",
            *names[:20],
        ]
        if len(names) > 20:
            message_lines.append(
                f"… and {len(names) - 20} more selected server(s)."
            )
        self.clear_items()

        async def enter_v19_message(
            button_interaction: discord.Interaction,
        ) -> None:
            if self.confirmation_started:
                await button_interaction.response.send_message(
                    "This announcement has already been confirmed.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await button_interaction.response.send_modal(
                AnnouncementTextModal(
                    self,
                    modal_revision=self.revision,
                    include_v19_title=True,
                )
            )

        v19_button = discord.ui.Button(
            label="Write V1.9 update log",
            style=discord.ButtonStyle.primary,
        )
        v19_button.callback = enter_v19_message
        self.add_item(v19_button)

        async def enter_message(button_interaction: discord.Interaction) -> None:
            if self.confirmation_started:
                await button_interaction.response.send_message(
                    "This announcement has already been confirmed.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await button_interaction.response.send_modal(
                AnnouncementTextModal(
                    self,
                    modal_revision=self.revision,
                )
            )

        button = discord.ui.Button(
            label="Write custom announcement",
            style=discord.ButtonStyle.secondary,
        )
        button.callback = enter_message
        self.add_item(button)
        self.add_cancel_button()
        await self._edit_anchor("\n".join(message_lines), embed=None)

    def add_cancel_button(self) -> None:
        async def cancel(interaction: discord.Interaction) -> None:
            self.stop()
            await interaction.response.edit_message(
                content="Announcement cancelled.",
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        button.callback = cancel
        self.add_item(button)

    async def preview(self, interaction: discord.Interaction, body: str) -> None:
        expected_revision = self.revision
        cleaned = body.strip()
        if not cleaned:
            await self._show_error(interaction, "The announcement text cannot be empty.")
            return
        if len(cleaned) > MAX_ANNOUNCEMENT_LENGTH:
            await self._show_error(
                interaction,
                f"Announcement text must be {MAX_ANNOUNCEMENT_LENGTH} characters "
                "or fewer.",
            )
            return
        if self.tier is None or not self.guild_ids:
            await self._show_error(
                interaction,
                "Select the announcement tier and target servers again.",
            )
            return
        self.body = cleaned
        self.reviewed_snapshot = None
        await interaction.response.defer()
        preview_tier = self.tier
        preview_guild_ids = tuple(self.guild_ids)
        targets, errors = await self.resolve_targets(
            preview_guild_ids,
            preview_tier,
        )
        if self.revision != expected_revision or self.confirmation_started:
            await interaction.followup.send(
                "The announcement changed while the destination preview was "
                "being prepared. No confirmation was created; review it again.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.preview_targets = targets
        target_by_guild = {target.guild_id: target for target in targets}
        destinations = tuple(
            (
                guild_id,
                (
                    target_by_guild[guild_id].channel.id
                    if guild_id in target_by_guild
                    and target_by_guild[guild_id].channel is not None
                    else None
                ),
            )
            for guild_id in preview_guild_ids
        )
        snapshot = AnnouncementConfirmation(
            revision=expected_revision,
            tier=preview_tier,
            body=cleaned,
            guild_ids=preview_guild_ids,
            destinations=destinations,
        )
        self.reviewed_snapshot = snapshot
        pages = self._recipient_pages(self.preview_targets, errors)
        for page in pages:
            await interaction.channel.send(
                page,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if self.revision != expected_revision or self.confirmation_started:
                await interaction.followup.send(
                    "The announcement changed while the preview was being "
                    "displayed. No send confirmation was created.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        embed = discord.Embed(
            title=f"{snapshot.tier} announcement preview",
            description=snapshot.body,
            color=ANNOUNCEMENT_COLORS[snapshot.tier],
        )
        embed.add_field(
            name="Recipients",
            value=(
                f"{sum(target.channel is not None for target in self.preview_targets)} "
                "server(s) ready; "
                f"{len(errors)} will be skipped."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Nothing has been sent. Confirm below to send to the ready "
                "servers; skipped destinations will be reported."
            )
        )
        self.clear_items()
        async def confirm(button_interaction: discord.Interaction) -> None:
            await self.send_announcement(button_interaction)

        send_button = discord.ui.Button(
            label="Confirm and send",
            style=discord.ButtonStyle.success,
            custom_id="arc_announcement_confirm",
        )
        send_button.callback = confirm
        self.confirm_button = send_button
        self.add_item(send_button)
        self.add_cancel_button()
        if self.revision != expected_revision or self.confirmation_started:
            self.reviewed_snapshot = None
            await interaction.followup.send(
                "The announcement changed before confirmation was ready. "
                "Nothing was sent; review it again.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._edit_anchor("", embed=embed)

    async def resolve_targets(
        self,
        guild_ids: tuple[int, ...] | list[int],
        tier: str,
    ) -> tuple[list[AnnouncementTarget], list[str]]:
        eligible = await self.eligible_guilds(tier)
        targets: list[AnnouncementTarget] = []
        errors: list[str] = []
        for guild_id in guild_ids:
            item = eligible.get(guild_id)
            if item is None:
                errors.append(f"`{guild_id}` — no longer authorized or eligible.")
                continue
            guild, license_type = item
            scrim, channel_id, error = _selected_logs_channel(
                self.repository,
                guild_id,
            )
            if error or scrim is None or channel_id is None:
                targets.append(
                    AnnouncementTarget(
                        guild_id, guild.name, license_type, None, None, error
                    )
                )
                errors.append(
                    f"{discord.utils.escape_markdown(guild.name)} (`{guild_id}`) — "
                    f"{error or 'No logs channel is configured.'}"
                )
                continue
            try:
                channel = await self.configured_text_channel(scrim, channel_id)
            except discord.HTTPException:
                logger.exception(
                    "Could not resolve announcement logs channel %s.",
                    channel_id,
                )
                channel = None
            if channel is None:
                targets.append(
                    AnnouncementTarget(
                        guild_id,
                        guild.name,
                        license_type,
                        None,
                        None,
                        "Configured logs channel could not be resolved.",
                    )
                )
                errors.append(
                    f"{discord.utils.escape_markdown(guild.name)} (`{guild_id}`) — "
                    f"configured logs channel could not be resolved."
                )
                continue
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
                targets.append(
                    AnnouncementTarget(
                        guild_id,
                        guild.name,
                        license_type,
                        None,
                        None,
                        "Configured logs destination is not a text channel in this server.",
                    )
                )
                errors.append(
                    f"{discord.utils.escape_markdown(guild.name)} (`{guild_id}`) — "
                    "configured logs destination is not a text channel in that server."
                )
                continue
            member = guild.me
            permissions = channel.permissions_for(member) if member else None
            missing = []
            if permissions is None or not permissions.view_channel:
                missing.append("View Channel")
            if permissions is None or not permissions.send_messages:
                missing.append("Send Messages")
            if permissions is None or not permissions.read_message_history:
                missing.append("Read Message History (duplicate check)")
            if missing:
                reason = "missing " + ", ".join(missing)
                targets.append(
                    AnnouncementTarget(
                        guild_id,
                        guild.name,
                        license_type,
                        None,
                        channel.name,
                        reason,
                    )
                )
                errors.append(
                    f"{discord.utils.escape_markdown(guild.name)} (`{guild_id}`) "
                    f"· #{discord.utils.escape_markdown(channel.name)} — {reason}."
                )
                continue
            targets.append(
                AnnouncementTarget(
                    guild_id,
                    guild.name,
                    license_type,
                    channel,
                    channel.name,
                )
            )
        return targets, errors

    @staticmethod
    def _recipient_pages(
        targets: list[AnnouncementTarget],
        errors: list[str],
    ) -> list[str]:
        lines = [
            "**Announcement destination preview**",
            "The bot will use only configured scrim logs channels (or the "
            "server logs override), never public/staff boards.",
        ]
        for target in targets:
            if target.channel is None:
                lines.append(
                    f"• {discord.utils.escape_markdown(target.guild_name)} "
                    f"(`{target.guild_id}`) — SKIPPED: "
                    f"{target.error or 'destination unavailable'}"
                )
            else:
                lines.append(
                    f"• {discord.utils.escape_markdown(target.guild_name)} "
                    f"(`{target.guild_id}`, {target.license_type}) — "
                    f"#{discord.utils.escape_markdown(target.channel_name or '')} "
                    f"(`{target.channel.id}`)"
                )
        lines.extend(f"• REVIEW REQUIRED: {error}" for error in errors)
        pages = []
        current = ""
        for line in lines:
            if current and len(current) + len(line) + 1 > 1900:
                pages.append(current)
                current = ""
            current = f"{current}\n{line}".strip()
        if current:
            pages.append(current)
        return pages or ["No announcement destinations were found."]

    async def send_announcement(
        self,
        interaction: discord.Interaction,
        snapshot: AnnouncementConfirmation | None = None,
    ) -> None:
        if self.send_in_progress:
            await interaction.response.send_message(
                "This announcement is already being processed. Wait for its "
                "delivery report; do not confirm again.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if snapshot is None:
            snapshot = self.confirmed_snapshot or self.reviewed_snapshot
        if snapshot is None:
            await interaction.response.send_message(
                "This preview is stale or incomplete. Review the message and "
                "destinations again before confirming.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not self.confirmation_started:
            if (
                snapshot is not self.reviewed_snapshot
                or snapshot.revision != self.revision
                or snapshot.tier != self.tier
                or snapshot.body != self.body
                or snapshot.guild_ids != tuple(self.guild_ids)
            ):
                self.reviewed_snapshot = None
                await interaction.response.send_message(
                    "This preview is stale. Review the message and exact "
                    "destinations again; nothing was sent.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            snapshot = AnnouncementConfirmation(
                revision=snapshot.revision,
                tier=snapshot.tier,
                body=snapshot.body,
                guild_ids=tuple(snapshot.guild_ids),
                destinations=tuple(snapshot.destinations),
            )
            self.confirmed_snapshot = snapshot
            self.confirmation_started = True
        elif snapshot is not self.confirmed_snapshot:
            original = self.confirmed_snapshot
            if (
                original is None
                or snapshot.tier != original.tier
                or snapshot.body != original.body
                or any(
                    destination not in original.destinations
                    for destination in snapshot.destinations
                )
                or not set(snapshot.guild_ids).issubset(original.guild_ids)
            ):
                await interaction.response.send_message(
                    "This retry does not match the confirmed announcement. "
                    "Nothing was sent.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        self.send_in_progress = True
        if self.confirm_button is not None:
            self.confirm_button.disabled = True
            self.confirm_button.label = "Sending…"
        await interaction.response.defer()
        await self._edit_anchor(
            "Sending announcement. Do not confirm again.",
            embed=None,
        )
        try:
            targets, errors = await self.resolve_targets(
                snapshot.guild_ids,
                snapshot.tier,
            )
            reports = list(errors)
            sent_now = 0
            targets_by_guild = {target.guild_id: target for target in targets}
            reviewed_destinations = dict(snapshot.destinations)
            for guild_id in snapshot.guild_ids:
                if not recipient_is_currently_eligible(
                    self.repository,
                    guild_id,
                    snapshot.tier,
                ):
                    reports.append(
                        f"`{guild_id}` — skipped: local authorization was revoked "
                        "or the current license is outside the selected tier."
                    )
                    continue
                if self.bot.get_guild(guild_id) is None:
                    reports.append(
                        f"`{guild_id}` — skipped: this bot is no longer "
                        "connected to that server."
                    )
                    continue
                target = targets_by_guild.get(guild_id)
                if target is None or target.channel is None:
                    continue
                if target.channel.id != reviewed_destinations.get(guild_id):
                    reports.append(
                        f"{discord.utils.escape_markdown(target.guild_name)} "
                        f"(`{guild_id}`) — skipped: its logs destination changed "
                        "after review; create a fresh preview before sending there."
                    )
                    continue
                marker = announcement_marker(
                    snapshot.tier,
                    target.guild_id,
                    snapshot.body,
                )
                channel = target.channel
                try:
                    if await self.was_already_sent(channel, marker):
                        self.completed_guild_ids.add(target.guild_id)
                        reports.append(
                            f"{discord.utils.escape_markdown(target.guild_name)} "
                            f"(`{target.guild_id}`) — not resent: this announcement "
                            "was found in recent channel history."
                        )
                        continue
                    embed = discord.Embed(
                        description=snapshot.body,
                        color=ANNOUNCEMENT_COLORS[snapshot.tier],
                    )
                    embed.set_footer(text=marker)
                    await channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self.completed_guild_ids.add(target.guild_id)
                    sent_now += 1
                except discord.Forbidden:
                    reports.append(
                        f"{discord.utils.escape_markdown(target.guild_name)} "
                        f"(`{target.guild_id}`) — skipped: Discord denied channel "
                        "history or message sending."
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not send announcement to guild %s, channel %s.",
                        target.guild_id,
                        getattr(channel, "id", None),
                    )
                    reports.append(
                        f"{discord.utils.escape_markdown(target.guild_name)} "
                        f"(`{target.guild_id}`) — skipped: Discord send failed."
                    )

            self.retry_guild_ids = (
                set(snapshot.guild_ids) - self.completed_guild_ids
            )
            result_lines = [
                (
                    f"Partial delivery: {sent_now} sent in this attempt; "
                    f"{len(self.retry_guild_ids)} recipient(s) still need attention."
                    if self.retry_guild_ids
                    else f"Announcement complete: {sent_now} sent in this attempt."
                )
            ]
            if self.retry_guild_ids:
                result_lines.append(
                    "Successful or previously detected deliveries will not be "
                    "included in a retry. Fix the listed channel/authorization "
                    "issue, then use **Retry failed targets** to retry only the "
                    "remaining servers."
                )
            if reports:
                result_lines.append("Delivery report:")
                result_lines.extend(f"• {report}" for report in reports)
            result = "\n".join(result_lines)
            for page in self._chunk(result):
                await interaction.followup.send(
                    page,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            self.send_in_progress = False
            if self.retry_guild_ids:
                self._set_retry_view()
                await self._edit_anchor(
                    "Partial delivery. Review the report, fix any destination "
                    "issues, and use **Retry failed targets** only when ready.",
                    embed=self.preview_embed(),
                )
            else:
                self.stop()
                if self.message is not None:
                    try:
                        await self.message.edit(
                            content="Announcement process complete.",
                            embed=None,
                            view=None,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        logger.exception(
                            "Could not close completed announcement panel."
                        )
        except Exception:
            logger.exception("Announcement batch failed during delivery.")
            self.send_in_progress = False
            self.retry_guild_ids = (
                set(snapshot.guild_ids) - self.completed_guild_ids
            )
            await interaction.followup.send(
                "The announcement process stopped unexpectedly. Some servers "
                f"may already have received it; {len(self.completed_guild_ids)} "
                "successful/already-present destinations are recorded in this "
                "wizard. Check their logs channels before retrying. Use **Retry "
                "failed targets** only for recipients still pending.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if self.retry_guild_ids:
                self._set_retry_view()
                await self._edit_anchor(
                    "Delivery stopped with pending recipients. Check logs before "
                    "retrying; only pending recipients are eligible for retry.",
                    embed=self.preview_embed(),
                )

    def preview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{self.tier or 'ARC'} announcement",
            description=self.body or "",
            color=ANNOUNCEMENT_COLORS.get(
                self.tier,
                discord.Color.blurple(),
            ),
        )
        embed.add_field(
            name="Recipients",
            value=(
                f"{len(self.completed_guild_ids)} already sent/detected; "
                f"{len(self.retry_guild_ids)} pending."
            ),
            inline=False,
        )
        return embed

    def _set_retry_view(self) -> None:
        self.clear_items()

        async def retry(interaction: discord.Interaction) -> None:
            original = self.confirmed_snapshot
            if original is None:
                await interaction.response.send_message(
                    "The confirmed announcement snapshot is unavailable. "
                    "Start `!staffannounce` again.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            retry_ids = tuple(sorted(self.retry_guild_ids))
            retry_snapshot = AnnouncementConfirmation(
                revision=original.revision,
                tier=original.tier,
                body=original.body,
                guild_ids=retry_ids,
                destinations=tuple(
                    destination
                    for destination in original.destinations
                    if destination[0] in retry_ids
                ),
            )
            await self.send_announcement(interaction, retry_snapshot)

        retry_button = discord.ui.Button(
            label="Retry failed targets",
            style=discord.ButtonStyle.primary,
            custom_id="arc_announcement_retry",
        )
        retry_button.callback = retry
        self.confirm_button = retry_button
        self.add_item(retry_button)

        async def close(interaction: discord.Interaction) -> None:
            self.stop()
            await interaction.response.edit_message(
                content="Announcement tool closed. Review the delivery report.",
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        close_button = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.secondary,
            custom_id="arc_announcement_close",
        )
        close_button.callback = close
        self.add_item(close_button)

    @staticmethod
    async def was_already_sent(channel, marker: str) -> bool:
        async for message in channel.history(limit=100):
            if any(
                getattr(getattr(embed, "footer", None), "text", None) == marker
                for embed in getattr(message, "embeds", ())
            ):
                return True
        return False

    @staticmethod
    def _chunk(text: str, limit: int = 1900) -> list[str]:
        chunks = []
        current = ""
        for line in text.splitlines():
            if current and len(current) + len(line) + 1 > limit:
                chunks.append(current)
                current = ""
            current = f"{current}\n{line}".strip()
        if current:
            chunks.append(current)
        return chunks or [text[:limit]]

    async def _edit_anchor(
        self,
        content: str,
        *,
        embed: discord.Embed | None,
    ) -> None:
        if self.message is None:
            return
        await self.message.edit(
            content=content,
            embed=embed,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _show_error(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_timeout(self) -> None:
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="⏱️ This announcement panel expired. Run `!staffannounce` again.",
                    embed=None,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not close expired announcement panel.")


class SelectServersModal(discord.ui.Modal, title="Select announcement servers"):
    server_ids = discord.ui.TextInput(
        label="Enter `all` or eligible server IDs",
        placeholder="all   or   123456789, 987654321",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True,
    )

    def __init__(
        self,
        wizard: AnnouncementWizard,
        *,
        modal_revision: int,
    ) -> None:
        super().__init__()
        self.wizard = wizard
        self.modal_revision = modal_revision

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.wizard.authorize_modal_submission(
            interaction,
            self.modal_revision,
        ):
            return
        await self.wizard.set_selected_servers(
            interaction,
            str(self.server_ids.value),
        )


class AnnouncementTextModal(discord.ui.Modal, title="Write announcement"):
    text = discord.ui.TextInput(
        label="Message to send",
        placeholder="Paste the announcement text to show in the logs channel.",
        style=discord.TextStyle.paragraph,
        max_length=MAX_ANNOUNCEMENT_LENGTH,
        required=True,
    )

    def __init__(
        self,
        wizard: AnnouncementWizard,
        *,
        include_v19_title: bool = False,
        modal_revision: int | None = None,
    ) -> None:
        super().__init__()
        self.wizard = wizard
        self.modal_revision = (
            wizard.revision if modal_revision is None else modal_revision
        )
        if include_v19_title:
            self.text.default = V19_UPDATE_LOG_TEXT

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.wizard.authorize_modal_submission(
            interaction,
            self.modal_revision,
        ):
            return
        await self.wizard.preview(interaction, str(self.text.value))


def install_announcement_command(
    bot,
    repository,
    configured_text_channel,
) -> None:
    @bot.command(name="staffannounce", hidden=True)
    async def staffannounce_command(ctx: commands.Context) -> None:
        if not await bot.is_owner(ctx.author):
            await ctx.reply(
                "Only the bot owner can use this command.",
                delete_after=20,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if ctx.guild is not None:
            await ctx.reply(
                "For safety, run `!staffannounce` in a DM with this bot.",
                delete_after=20,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        wizard = AnnouncementWizard(
            owner_id=ctx.author.id,
            bot=bot,
            repository=repository,
            configured_text_channel=configured_text_channel,
        )
        try:
            wizard.message = await ctx.author.send(
                "**ARC announcement tool**\n"
                "Started with `!staffannounce`.\n"
                "Choose the announcement tier, select `all` eligible servers "
                "or exact guild IDs, enter the message, review every destination, "
                "and explicitly confirm before sending.",
                view=wizard,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not open announcement tool for owner %s.",
                ctx.author.id,
            )
