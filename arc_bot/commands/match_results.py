"""Match score review and correction controls for one bot instance."""
from __future__ import annotations

import re
from typing import Any

import discord
from discord.ext import commands

from arc_bot.storage.slot_storage import SlotStorageError
from arc_bot.utils.discord_views import delete_message_or_clear, disable_view_items
from arc_bot.views.base import ExpiringView
from scrim_state import MatchScore, STATUS_AVAILABLE

_owner: Any = None


def bind_owner(owner: Any) -> None:
    global _owner
    _owner = owner


def _o(name: str) -> Any:
    return getattr(_owner, name)


def _snapshot(scrim, match_number):
    return tuple(
        (score.slot_number, score.kills, score.placement)
        for score in sorted(
            (score for score in getattr(scrim, "match_scores", {}).values()
             if score.match_number == match_number),
            key=lambda item: item.slot_number,
        )
    )


class MatchScoreSubmissionReviewView(ExpiringView):
    def __init__(self, *, owner_id, guild_id, channel_id, scrim,
                 match_number, scores, raw_input):
        super().__init__(timeout=300)
        self.owner_id, self.guild_id, self.channel_id = owner_id, guild_id, channel_id
        self.scrim_id, self.match_number = scrim.id, match_number
        self.scores, self.raw_input = tuple(scores), raw_input.strip()
        self.assignment_generation = _o("assignment_fingerprint")(scrim)
        self.baseline_scores = _snapshot(scrim, match_number)
        self.team_names = {
            score.slot_number: scrim.slots[score.slot_number].team_name
            for score in scores
        }
        self.message = None
        self.completed = self.editing = self.processing = False

    def embed(self):
        lines = [
            f"**{score.placement:02d}.** Slot {score.slot_number:02d} · "
            f"{discord.utils.escape_markdown(discord.utils.escape_mentions(self.team_names.get(score.slot_number, 'Unknown team')))} — **{score.kills}** kills"
            for score in sorted(self.scores, key=lambda item: item.placement)
        ]
        embed = discord.Embed(
            title=f"Review Match {self.match_number} results",
            description=("Check the rank order and kills before saving. "
                         f"Confirm replaces the previous complete result set for Match {self.match_number}; "
                         "teams omitted here will be removed.\n\n" + "\n".join(lines) +
                         "\n\nAny authorized scrim Staff member in this channel may review this submission."),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="No scores have been saved · Confirm saves · Edit changes the command")
        return embed

    async def authorized_scrim(self, interaction):
        if getattr(interaction.guild, "id", None) != self.guild_id or interaction.channel_id != self.channel_id:
            await interaction.response.send_message("This score review is only valid in its original server and channel.", ephemeral=True)
            return None
        if self.completed:
            await interaction.response.send_message("This score review has already been processed.", ephemeral=True)
            return None
        scrim = _o("repository").get(self.scrim_id)
        if (scrim is None or scrim.guild_id != self.guild_id or not _o("is_active")(scrim)
                or not _o("member_is_staff")(interaction.user, scrim)):
            await interaction.response.send_message("You no longer have access to this scrim's score review.", ephemeral=True)
            return None
        return scrim

    async def current_scrim(self, interaction):
        scrim = await self.authorized_scrim(interaction)
        if scrim is None:
            return None
        if (_o("assignment_fingerprint")(scrim) != self.assignment_generation
                or _snapshot(scrim, self.match_number) != self.baseline_scores):
            await interaction.response.send_message(
                "This review is out of date because teams or saved scores changed. No proposed scores were saved. Run the command again to review the current data.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await self.close("This score review is out of date.", message=getattr(interaction, "message", None))
            return None
        return scrim

    async def close(self, content, *, message=None):
        self.completed = True
        self.editing = False
        disable_view_items(self)
        self.stop()
        await delete_message_or_clear(self.message or message, log_context=f"Match {self.match_number} score review", fallback_content=content)

    async def on_timeout(self):
        if not self.completed:
            await self.close(f"Match {self.match_number} score review expired. No proposed scores were saved.")

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction, button):
        scrim = await self.current_scrim(interaction)
        if scrim is None:
            return
        try:
            processed = _o("repository").replace_match_scores(scrim.id, scrim.guild_id, self.match_number, list(self.scores))
        except (ValueError, SlotStorageError) as error:
            await interaction.response.send_message(
                f"❌ {error}" if isinstance(error, ValueError) else "The scores could not be saved. Nothing was changed; please try again.",
                ephemeral=True)
            return
        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.send_message(
            content=f"✅ Scores saved for Match {self.match_number}: Processed {processed} teams (reviewed by <@{interaction.user.id}>). Use `!res` when you are ready to publish the leaderboard image.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await delete_message_or_clear(self.message or getattr(interaction, "message", None), log_context="completed score review", fallback_content="This score review has been completed.")

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def edit_button(self, interaction, button):
        scrim = await self.current_scrim(interaction)
        if scrim is not None:
            self.editing = True
            await interaction.response.send_modal(MatchScoreSubmissionEditModal(self))

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction, button):
        if await self.authorized_scrim(interaction) is None:
            return
        self.completed = True
        disable_view_items(self)
        self.stop()
        await interaction.response.send_message("Score entry cancelled. No proposed scores were saved; previously saved results remain unchanged.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await delete_message_or_clear(self.message or getattr(interaction, "message", None), log_context="cancelled score review", fallback_content="This score review was cancelled.")


class MatchScoreSubmissionEditModal(discord.ui.Modal):
    def __init__(self, source_review):
        super().__init__(title=f"Edit Match {source_review.match_number} score command", timeout=300)
        self.source_review = source_review
        self.command_input = discord.ui.TextInput(label="Paste or edit the full command", style=discord.TextStyle.paragraph, required=True, max_length=4000, default=f"!resg{source_review.match_number}\n{source_review.raw_input}"[:4000])
        self.add_item(self.command_input)

    async def on_submit(self, interaction):
        source = self.source_review
        scrim = await source.authorized_scrim(interaction)
        if scrim is None:
            return
        text = _o("normalize_score_submission_text")(self.command_input.value, source.match_number)
        try:
            scores = _o("parse_match_score_lines")(text, match_number=source.match_number, scrim=scrim)
        except ValueError as error:
            await interaction.response.send_message(f"❌ {error} Nothing was saved. Reopen Edit to try again.", ephemeral=True)
            return
        review = type(source)(owner_id=source.owner_id, guild_id=scrim.guild_id, channel_id=source.channel_id, scrim=scrim, match_number=source.match_number, scores=scores, raw_input=text)
        await interaction.response.send_message("Review the edited scores below. **Nothing has been saved.**", embed=review.embed(), view=review, allowed_mentions=discord.AllowedMentions.none())
        try:
            review.message = await interaction.original_response()
        except discord.HTTPException:
            pass
        await source.close("This score review was replaced by the edited proposal below. It did not save any scores.")


class MatchScoreCorrectionView(ExpiringView):
    def __init__(self, *, owner_id, guild_id, channel_id, scrim, match_number, slots):
        super().__init__(timeout=300)
        self.owner_id, self.guild_id, self.channel_id = owner_id, guild_id, channel_id
        self.scrim_id, self.match_number = scrim.id, match_number
        self.slot_assignment_ids = {slot.number: slot.assignment_id for slot in slots}
        self.message = None
        selector = discord.ui.Select(placeholder="Choose the slot/team to correct...", options=[
            discord.SelectOption(label=f"Slot {slot.number:02d} · {slot.team_name}"[:100], value=str(slot.number))
            for slot in slots])
        async def callback(interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("This score correction belongs to another staff member.", ephemeral=True); return
            current = _o("repository").get(self.scrim_id)
            if current is None:
                await interaction.response.send_message("That scrim no longer exists.", ephemeral=True); return
            slot = current.slots.get(int(selector.values[0]))
            if slot is None or slot.assignment_id != self.slot_assignment_ids.get(slot.number) or slot.status == STATUS_AVAILABLE:
                await interaction.response.send_message("That team changed after this correction menu opened. Run the command again and choose the current team.", ephemeral=True); return
            await interaction.response.send_modal(MatchScoreCorrectionModal(self, slot, default_score=current.match_scores.get((self.match_number, slot.number)), baseline_score=current.match_scores.get((self.match_number, slot.number))))
        selector.callback = callback
        self.add_item(selector)

    async def authorized_scrim(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This score correction belongs to another staff member.", ephemeral=True); return None
        scrim = _o("repository").get(self.scrim_id)
        if scrim is None or getattr(interaction.guild, "id", None) != self.guild_id or interaction.channel_id != self.channel_id:
            await interaction.response.send_message("This score correction is no longer valid in this channel.", ephemeral=True); return None
        return scrim

    async def finish(self, content):
        disable_view_items(self); self.stop()
        await delete_message_or_clear(self.message, log_context="score correction", fallback_content=content)

    async def on_timeout(self):
        await self.finish("This score correction expired.")


class MatchScoreCorrectionModal(discord.ui.Modal):
    def __init__(self, correction_view, slot, *, default_score=None, baseline_score=None, source_review=None):
        super().__init__(title=f"Edit Match {correction_view.match_number} · Slot {slot.number:02d}", timeout=300)
        self.correction_view, self.slot = correction_view, slot
        self.source_review = source_review
        self.slot_number = slot.number
        self.baseline_score = baseline_score
        self.kills_input = discord.ui.TextInput(label="Kills", required=True, max_length=5, default=str(getattr(default_score, "kills", 0)))
        self.placement_input = discord.ui.TextInput(label="Placement", required=True, max_length=3, default=str(getattr(default_score, "placement", 1)))
        self.add_item(self.kills_input); self.add_item(self.placement_input)

    async def on_submit(self, interaction):
        scrim = await self.correction_view.authorized_scrim(interaction)
        if scrim is None: return
        try:
            kills, placement = int(str(self.kills_input.value).strip()), int(str(self.placement_input.value).strip())
            if kills < 0 or placement < 1: raise ValueError
        except ValueError:
            await interaction.response.send_message("Kills and placement must be whole numbers. Kills cannot be negative and placement must be at least 1.", ephemeral=True); return
        score = MatchScore(self.correction_view.match_number, self.slot.number, kills, placement)
        review = MatchScoreCorrectionReviewView(self.correction_view, self.slot, score, self.baseline_score)
        await interaction.response.send_message("Review this score correction before saving.", embed=review.embed(), view=review, ephemeral=True)


class MatchScoreCorrectionReviewView(ExpiringView):
    def __init__(self, correction_view, slot, proposed_score=None, baseline_score=None, previous_score=None):
        super().__init__(timeout=300)
        self.correction_view, self.slot, self.proposed_score = correction_view, slot, proposed_score
        self.baseline_score = baseline_score if baseline_score is not None else previous_score
        self.previous_score = self.baseline_score
        self.completed = False

    def embed(self):
        return discord.Embed(title=f"Review score correction · Match {self.proposed_score.match_number}", description=f"Slot {self.slot.number:02d} · {self.slot.team_name}\nKills: **{self.proposed_score.kills}**\nPlacement: **{self.proposed_score.placement}**", color=discord.Color.orange())

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction, button):
        scrim = await self.correction_view.authorized_scrim(interaction)
        if scrim is None: return
        current = scrim.match_scores.get((self.proposed_score.match_number, self.slot.number))
        if current != self.baseline_score:
            await interaction.response.edit_message(content="This score changed after the recap was opened. Run the command again.", view=None); return
        try:
            _o("repository").upsert_match_scores(scrim.id, scrim.guild_id, self.proposed_score.match_number, [self.proposed_score])
        except (ValueError, SlotStorageError) as error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True); return
        await interaction.response.edit_message(
            content=(
                f"✅ Score correction saved for Slot {self.slot.number:02d}: "
                f"rank {self.proposed_score.placement}, "
                f"{self.proposed_score.kills} kills."
            ),
            view=None,
        )
        self.completed = True
        self.stop()

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction, button):
        self.completed = True
        self.completed = True
        await interaction.response.edit_message(content="Score correction cancelled. No score was changed.", view=None); self.stop()

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def edit_button(self, interaction, button):
        await interaction.response.send_modal(
            MatchScoreCorrectionModal(
                self.correction_view, self.slot,
                default_score=self.proposed_score,
                baseline_score=self.baseline_score,
                source_review=self,
            )
        )

    @discord.ui.button(label="Choose another team", style=discord.ButtonStyle.secondary)
    async def choose_another_team_button(self, interaction, button):
        self.correction_view.message = getattr(interaction, "message", None)
        self.completed = True
        await interaction.response.edit_message(view=self.correction_view)


async def edit_match_score_command(ctx: commands.Context, match_number: str):
    if not str(match_number).strip().isdigit():
        await _o("send_private_command_feedback")(ctx, "Use `!editres <match number>`, for example `!editres 2`.", silent=False)
        return
    return await _o("_start_match_score_correction")(ctx, int(str(match_number).strip()))


def install_match_results_commands(bot, owner):
    bind_owner(owner)
    bot.remove_command("editres")
    command = bot.command(name="editres")(commands.guild_only()(edit_match_score_command))
    return {
        "MatchScoreSubmissionReviewView": MatchScoreSubmissionReviewView,
        "MatchScoreSubmissionEditModal": MatchScoreSubmissionEditModal,
        "MatchScoreCorrectionView": MatchScoreCorrectionView,
        "MatchScoreCorrectionModal": MatchScoreCorrectionModal,
        "MatchScoreCorrectionReviewView": MatchScoreCorrectionReviewView,
        "edit_match_score_command": command,
    }