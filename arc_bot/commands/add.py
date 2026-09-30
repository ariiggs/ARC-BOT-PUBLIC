"""Staff bulk-registration command.

This module is deliberately owner-bound.  The two bot entry points have
slightly different runtime globals (and tests patch those globals), so command
helpers must resolve collaborators through the owning main module rather than
importing an entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import discord
from discord.ext import commands
from arc_bot.views.base import DurableView

_owner: Any = None


def bind_owner(owner: Any) -> None:
    global _owner
    _owner = owner


def _get(name: str) -> Any:
    return getattr(_owner, name)


@dataclass
class AddDraftEntry:
    original_line: str
    team_name: str
    slot_number: int
    member: discord.Member
    tag: str = ""


def add_preview_embed(scrim, valid_entries, error_entries, *, title="🔍 Registration Preview",
                      color=discord.Color.blurple()):
    lines = []
    if valid_entries:
        lines.append("**Valid registrations**")
        lines.extend(
            f"✅ Slot {entry.slot_number:02d}: "
            f"{discord.utils.escape_mentions(entry.team_name)} "
            f"(Cap: <@{entry.member.id}>)" for entry in valid_entries
        )
    if error_entries:
        if lines:
            lines.append("")
        lines.append("**Entries requiring attention**")
        lines.extend(f"⚠️ Failed: {discord.utils.escape_mentions(error)}"
                     for error in error_entries)
    if not lines:
        lines.append("No registrations were provided.")
    embed = discord.Embed(title=title, description="\n".join(lines)[:4096], color=color)
    embed.set_footer(text=f"Scrim: {scrim.name}")
    return embed


async def resolve_add_member(ctx, member_text):
    match = re.fullmatch(r"<@!?(\d+)>", member_text.strip())
    if match is not None and ctx.guild is not None:
        member = ctx.guild.get_member(int(match.group(1)))
        if member is None:
            member = await ctx.guild.fetch_member(int(match.group(1)))
        return member
    return await commands.MemberConverter().convert(ctx, member_text)


async def build_add_draft(ctx, scrim, arguments):
    raw = arguments.strip()
    lines = [line.strip() for line in (raw.splitlines() if raw else []) if line.strip()]
    valid_entries, error_entries, requested_slots = [], [], set()
    if not lines:
        return [], ["empty input - provide Team / TAG / @Captain"]
    for line in lines:
        parts = [part.strip() for part in line.split("/")]
        if len(parts) != 3:
            if len(lines) != 1:
                error_entries.append(f"{line} - invalid format; use Team / TAG / @Captain")
                continue
            try:
                team_name, tag, member_text = _get("parse_add_arguments")(line)
            except ValueError:
                error_entries.append(f"{line} - invalid format")
                continue
        else:
            team_name, tag, member_text = parts
            if not team_name or not tag or not member_text:
                error_entries.append(f"{line} - invalid format")
                continue
        if not team_name or not member_text:
            error_entries.append(f"{line} - team name and captain are required")
            continue
        try:
            member = await _get("resolve_add_member")(ctx, member_text)
        except (commands.MemberNotFound, discord.NotFound, discord.Forbidden, discord.HTTPException):
            error_entries.append(f"{line} - captain not found")
            continue
        async with scrim.state_lock:
            slot_number = next(
                (
                    slot.number
                    for slot in scrim.slots.values()
                    if _get("slot_is_assignable")(slot)
                    and slot.number not in requested_slots
                ),
                None,
            )
        if slot_number is None:
            error_entries.append(f"{line} - no available slot")
            continue
        requested_slots.add(slot_number)
        valid_entries.append(AddDraftEntry(line, team_name, slot_number, member, tag))
    return valid_entries, error_entries


async def apply_add_entries(scrim, entries):
    unavailable, snapshots = [], []
    async with scrim.state_lock:
        if not _get("is_active")(scrim):
            unavailable.append("The scrim is no longer active.")
        elif _get("pause_scrim_if_unconfigured")(scrim):
            unavailable.append("The scrim is paused because required settings are missing.")
        else:
            for entry in entries:
                slot = scrim.slots.get(entry.slot_number)
                if slot is None or not _get("slot_is_assignable")(slot):
                    unavailable.append(f"Slot {entry.slot_number:02d} became occupied before confirmation.")
            if not unavailable:
                with _get("repository").transaction():
                    for entry in entries:
                        slot = scrim.slots[entry.slot_number]
                        slot.assignment_id += 1
                        slot.status = _get("STATUS_RESERVED")
                        slot.team_name, slot.tag = entry.team_name, entry.tag
                        slot.manager_id, slot.captain_1_id, slot.captain_2_id = entry.member.id, entry.member.id, None
                        snapshots.append(slot.snapshot())
    if unavailable:
        return snapshots, unavailable, [], False
    access_failures = []
    for entry in entries:
        if not await _get("grant_manager_access")(scrim, entry.member):
            access_failures.append(f"Slot {entry.slot_number:02d}")
        await _get("send_scrim_log")(scrim, "TEAM ADDED",
            f"Slot {entry.slot_number:02d} · team **{entry.team_name}** · captain <@{entry.member.id}>")
    board_refreshed = await _get("refresh_public_slots")(scrim)
    return snapshots, unavailable, access_failures, board_refreshed


class AddRegistrationView(DurableView):
    def __init__(self, ctx, scrim, valid_entries, error_entries):
        super().__init__(timeout=120)
        self.ctx, self.scrim, self.owner_id = ctx, scrim, ctx.author.id
        self.valid_entries, self.error_entries = valid_entries, error_entries
        self.message, self.completed, self.processing = None, False, False
        if not valid_entries:
            self.confirm_button.disabled = True

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This registration preview belongs to another staff member.", ephemeral=True)
            return False
        return True

    def final_embed(self, title, *, color, extra_lines=None):
        lines = list(extra_lines or [])
        if not lines:
            lines = [f"✅ Slot {e.slot_number:02d}: {discord.utils.escape_mentions(e.team_name)} "
                     f"(Cap: <@{e.member.id}>)" for e in self.valid_entries]
        embed = discord.Embed(title=title, description="\n".join(lines)[:4096], color=color)
        embed.set_footer(text=f"Scrim: {self.scrim.name}")
        return embed

    async def on_timeout(self):
        if self.completed or self.processing:
            return
        self.completed = True
        _get("disable_view_items")(self)
        self.stop()
        await _get("delete_message_or_clear")(self.message, log_context="expired registration preview",
            fallback_content="This registration preview expired. No changes were made.")

    async def _finish_cancel(self, interaction):
        if self.completed or self.processing:
            await interaction.response.send_message("This registration preview is already being processed.", ephemeral=True)
            return
        self.completed = True
        _get("disable_view_items")(self)
        self.stop()
        await interaction.response.send_message("Registration cancelled. No changes were made.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await _get("delete_message_or_clear")(self.message or getattr(interaction, "message", None),
            log_context="cancelled registration preview", fallback_content="This registration preview was cancelled.")

    async def _finish_confirm(self, interaction):
        if self.completed or self.processing:
            await interaction.response.send_message("This registration preview has already been processed.", ephemeral=True)
            return
        self.processing = True
        try:
            snapshots, unavailable, access_failures, board_refreshed = await _get("apply_add_entries")(self.scrim, self.valid_entries)
        except Exception:
            self.processing = False
            _get("logger").exception("Could not finish the registration preview for scrim %s.", self.scrim.id)
            await interaction.response.send_message("Registration could not be completed. Check the current slot board and try again.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        if unavailable:
            self.completed = True; _get("disable_view_items")(self); self.stop()
            await interaction.response.send_message(embed=self.final_embed("⚠️ Registration Not Completed",
                color=discord.Color.orange(), extra_lines=["No changes were made.", *unavailable]),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await _get("delete_message_or_clear")(self.message or getattr(interaction, "message", None),
                log_context="unavailable registration preview", fallback_content="This registration preview is no longer available.")
            return
        self.completed = True; _get("disable_view_items")(self); self.stop()
        result_lines = [f"✅ Slot {s.number:02d}: {discord.utils.escape_mentions(s.team_name)} (Cap: <@{s.captain_1_id}>)" for s in snapshots]
        if access_failures:
            result_lines.append("⚠️ Pending Captain access could not be completed for: " + ", ".join(access_failures) + ".")
        if not board_refreshed:
            result_lines.append("⚠️ The slot board could not be refreshed.")
        result_lines.extend(f"⚠️ Skipped: {e}" for e in self.error_entries)
        await interaction.response.send_message(embed=self.final_embed("✅ Registration Completed Successfully",
            color=discord.Color.green(), extra_lines=result_lines), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        await _get("delete_message_or_clear")(self.message or getattr(interaction, "message", None),
            log_context="completed registration preview", fallback_content="This registration preview has been completed.")

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction, button):
        await self._finish_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_button(self, interaction, button):
        await self._finish_cancel(interaction)


async def add_team(ctx, *, arguments):
    scrim = await _get("require_staff_scrim")(ctx, allow_public=True)
    if scrim is None:
        return
    valid_entries, error_entries = await _get("build_add_draft")(ctx, scrim, arguments)
    if len(valid_entries) < 2:
        if not valid_entries:
            details = "\n".join(f"⚠️ {e}" for e in error_entries)
            await _get("send_private_command_feedback")(ctx, details or "No valid registration was provided.", silent=False)
            return
        snapshots, unavailable, access_failures, board_refreshed = await _get("apply_add_entries")(scrim, valid_entries)
        if unavailable:
            await _get("send_private_command_feedback")(ctx, "No changes were made.\n" + "\n".join(f"⚠️ {e}" for e in unavailable), silent=False)
            return
        messages = [f"✅ Slot {s.number:02d}: {s.team_name} was added." for s in snapshots]
        messages.extend(f"⚠️ Skipped: {e}" for e in error_entries)
        if access_failures:
            messages.append("⚠️ Pending Captain access could not be completed for: " + ", ".join(access_failures) + ".")
        if not board_refreshed:
            messages.append("⚠️ The slot board could not be refreshed.")
        await _get("send_private_command_feedback")(ctx, "\n".join(messages), silent=False, delete_after=30)
        return
    view = _get("AddRegistrationView")(ctx, scrim, valid_entries, error_entries)
    try:
        view.message = await ctx.send(embed=_get("add_preview_embed")(scrim, valid_entries, error_entries),
            view=view, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        _get("logger").exception("Could not send the registration preview.")
        return
    await _get("delete_command_message")(ctx)


def install_add_command(bot, owner):
    bind_owner(owner)
    bot.command(name="add", aliases=["a"])(commands.guild_only()(add_team))