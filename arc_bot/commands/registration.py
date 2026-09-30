"""Public registration and review feature binding.

The registration implementation is owned by the entry-point module because it
uses a substantial set of instance-local storage and Discord helpers.  This
adapter gives each bot tree an independent feature installation and command
registration, while keeping those collaborators resolved on the owning
module (which is also important for test doubles and separate bot state).
"""
from __future__ import annotations

import re
import discord
from typing import Any

from discord.ext import commands
from arc_bot.views.base import DurableView
from arc_bot.commands.bans import BanStorageError, RegistrationBlockedByBan

_owner: Any = None


def _d(name: str) -> Any:
    return getattr(_owner, name)


def registration_queue_content(scrim) -> str:
    requests = sorted(
        getattr(scrim, "pending_registrations", {}).values(),
        key=lambda request: (request.slot_number, request.request_id),
    )
    lines = [f"**Registration review · {len(requests)} pending**"]
    if not requests:
        lines.append("No teams waiting for staff review.")
    flagged = [request for request in requests if request.ban_match_reason]
    ordinary = [request for request in requests if not request.ban_match_reason]
    visible = 0
    for title, group in (("⚠️ FLAGGED · possible ban matches", flagged), ("🆗 Standard review", ordinary)):
        if not group:
            continue
        lines.append(f"\n**{title}**")
        for request in group:
            safe = lambda value: _d("discord").utils.escape_markdown(
                _d("discord").utils.escape_mentions(value)
            )
            name, tag = safe(request.team_name), safe(request.tag)
            if request.ban_match_reason:
                reason = safe(request.ban_match_reason)
                line = (
                    f"⚠️ `{request.slot_number:02d}` **{name}** · {tag} · "
                    f"<@{request.manager_id}>\n↳ {reason}"
                )
            else:
                line = (
                    f"{_d('configured_emoji')(scrim, 'registration_ok_emoji', '🆗')} "
                    f"`{request.slot_number:02d}` **{name}** · {tag} · <@{request.manager_id}>"
                )
            if len("\n".join(lines)) + len(line) > 1750:
                if request.ban_match_reason:
                    line = (
                        f"⚠️ `{request.slot_number:02d}` **{name}** · {tag} · "
                        f"<@{request.manager_id}> · select to review all match reasons"
                    )
                if len("\n".join(lines)) + len(line) > 1750:
                    break
            lines.append(line)
            visible += 1
    if len(requests) > visible:
        lines.append(f"\n…and {len(requests) - visible} more. Use the selection pages below.")
    lines.append("\nAccept or Decline opens a private selection and confirmation.")
    return "\n".join(lines)


async def notify_staff_for_registration(scrim, request, *, tag_match=None) -> bool:
    if request is None or not await refresh_registration_queue(scrim):
        return False
    if tag_match is None:
        return True
    channel = await _d("staff_channel")(scrim)
    if channel is None or not _d("is_active")(scrim):
        return False
    config = _d("repository").get_server_config(scrim.guild_id)
    staff_role_id = config.staff_role_id if config else None
    existing_name, existing_number, existing_status = tag_match
    safe = lambda value: _d("discord").utils.escape_markdown(
        _d("discord").utils.escape_mentions(value)
    )
    content = (
        f"{f'<@&{staff_role_id}> ' if staff_role_id else ''}"
        f"⚠️ **Matching team tag: {safe(request.tag)}**\n"
        f"New: **{safe(request.team_name)}** · Slot {request.slot_number:02d} · <@{request.manager_id}>\n"
        f"Already used by **{safe(existing_name)}** · Slot {existing_number:02d} · **{existing_status}**\n"
        "Held in the registration review queue for staff."
    )
    try:
        await channel.send(content, allowed_mentions=(
            _d("discord").AllowedMentions(
                everyone=False, users=False,
                roles=[_d("discord").Object(id=staff_role_id)],
                replied_user=False,
            ) if staff_role_id is not None else _d("discord").AllowedMentions.none()
        ))
        return True
    except _d("discord").HTTPException:
        _d("logger").exception("Could not send the registration review (%s).", scrim.id)
        return False


def parse_registration_arguments(arguments: str) -> tuple[str, str, str | None]:
    """Parse ``!register Team Name / Tag [/ @Manager]``."""
    raw = arguments.strip()
    if "\n" in raw or "\r" in raw:
        raise ValueError(
            "`!register` accepts one team per command. Use `!add` for bulk "
            "staff registrations."
        )
    parts = [part.strip() for part in raw.split("/")]
    if len(parts) not in {2, 3} or not all(parts):
        raise ValueError(
            "Wrong format. Use `!register Team Name / Tag` or "
            "`!register Team Name / Tag / @Manager`. "
            "The manager mention is optional; without it, you become captain."
        )
    team_name, tag = parts[:2]
    manager_text = parts[2] if len(parts) == 3 else None
    if len(team_name) > 100 or len(tag) > 32:
        raise ValueError("The team name or tag is too long.")
    if any(character in team_name or character in tag for character in ("\r", "\n")):
        raise ValueError("The team name and tag must stay on one line.")
    if manager_text is not None and not re.fullmatch(r"<@!?\d+>", manager_text):
        raise ValueError(
            "The manager must be a member mention, such as `@Manager`."
        )
    return team_name, tag, manager_text


def bind_owner(owner: Any) -> None:
    global _owner
    _owner = owner


async def registration_queue_allowed(interaction, scrim, *, message=True) -> bool:
    allowed = (
        _d("is_active")(scrim)
        and _d("repository").is_guild_authorized(scrim.guild_id)
        and interaction.guild is not None
        and interaction.guild.id == scrim.guild_id
        and interaction.channel_id == scrim.staff_channel_id
        and _d("member_is_staff")(interaction.user, scrim)
        and (
            not message
            or (
                interaction.message is not None
                and interaction.message.id == scrim.registration_review_message_id
            )
        )
    )
    if not allowed:
        await interaction.response.send_message(
            "This registration queue is for staff in this scrim's staff channel.",
            ephemeral=True,
        )
    return allowed


class SlotReviewView(DurableView):
    def __init__(
        self,
        scrim,
        slot,
        *,
        registration_request=False,
        registration_request_id=None,
    ):
        super().__init__(timeout=None)
        self.scrim = scrim
        self.slot = slot
        self.registration_request = registration_request
        self.registration_request_id = registration_request_id
        action = _d("StaffActionButton")
        self.add_item(
            action(
                scrim.id,
                slot.number,
                slot.assignment_id,
                True,
                registration_request=registration_request,
            )
        )
        self.add_item(
            action(
                scrim.id,
                slot.number,
                slot.assignment_id,
                False,
                registration_request=registration_request,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        allowed = (
            _d("interaction_matches_scrim")(interaction, self.scrim, staff=True)
            and isinstance(member, discord.Member)
            and _d("member_is_staff")(member, self.scrim)
        )
        if not allowed:
            await interaction.response.send_message(
                "These buttons are for staff in the configured staff channel only.",
                ephemeral=True,
            )
        return allowed

    async def finish_review(
        self, interaction: discord.Interaction, confirm: bool
    ) -> None:
        missing = _d("pause_scrim_if_unconfigured")(self.scrim)
        if missing:
            await interaction.response.send_message(
                "This scrim is paused because required settings are missing: "
                + ", ".join(missing)
                + ".",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = None
        registration_member_id = None
        reviewed_registration = None
        manager_to_revoke = None
        async with self.scrim.state_lock:
            if _d("is_active")(self.scrim):
                current = self.scrim.slots[self.slot.number]
                if self.registration_request:
                    requests = getattr(self.scrim, "pending_registrations", {})
                    request = requests.get(self.registration_request_id)
                    if (
                        request is not None
                        and current.assignment_id == request.assignment_id
                        and current.status == _d("STATUS_AVAILABLE")
                    ):
                        reviewed_registration = request
                        result = _d("SlotSnapshot")(
                            number=request.slot_number,
                            status=_d("STATUS_PENDING"),
                            team_name=request.team_name,
                            tag=request.tag,
                            manager_id=request.manager_id,
                            assignment_id=request.assignment_id,
                            captain_1_id=request.manager_id,
                        )
                        with _d("repository").transaction():
                            requests.pop(request.request_id, None)
                            if confirm:
                                current.assignment_id += 1
                                current.status = _d("STATUS_RESERVED")
                                current.team_name = request.team_name
                                current.tag = request.tag
                                current.manager_id = request.manager_id
                                current.captain_1_id = request.manager_id
                                current.captain_2_id = None
                                result = current.snapshot()
                                registration_member_id = request.manager_id
                elif (
                    current.assignment_id == self.slot.assignment_id
                    and current.team_name == self.slot.team_name
                    and current.manager_id == self.slot.manager_id
                    and current.status == _d("STATUS_PENDING")
                ):
                    with _d("repository").transaction():
                        result = current.snapshot() if not confirm else None
                        if confirm:
                            current.assignment_id += 1
                            current.status = _d("STATUS_CONFIRMED")
                            result = current.snapshot()
                        else:
                            result = _d("release_slot")(current)
                    if not confirm and result is not None:
                        manager_to_revoke = result.manager_id
        if manager_to_revoke is not None:
            await _d("revoke_manager_access_if_unused")(
                self.scrim, manager_to_revoke
            )
        if result is None:
            _d("disable_view_items")(self)
            await interaction.followup.send(
                "This request is no longer active or the slot was reassigned.",
                ephemeral=True,
            )
            await _d("delete_staff_review_message")(interaction)
            return

        access_ok = True
        if confirm and self.registration_request:
            guild = getattr(interaction, "guild", None) or _d("bot").get_guild(
                self.scrim.guild_id
            )
            member = None
            if guild is not None:
                get_member = getattr(guild, "get_member", None)
                member = (
                    get_member(registration_member_id)
                    if get_member is not None and registration_member_id is not None
                    else None
                )
                if member is None:
                    try:
                        fetch_member = getattr(guild, "fetch_member", None)
                        if fetch_member is not None and registration_member_id is not None:
                            member = await fetch_member(registration_member_id)
                    except (
                        discord.NotFound,
                        discord.Forbidden,
                        discord.HTTPException,
                    ):
                        member = None
            access_ok = member is not None and await _d("grant_manager_access")(
                self.scrim, member
            )
        text = (
            (
                f"✅ Team **{result.team_name}** has been reserved."
                if self.registration_request
                else f"🟢 Team **{result.team_name}** has been confirmed."
            )
            if confirm
            else (
                f"❌ Registration for **{result.team_name}** was rejected."
                if self.registration_request
                else f"Team **{result.team_name}** has been released."
            )
        )
        if confirm and self.registration_request and not access_ok:
            text += " Pending Captain access could not be completed."
        if self.registration_request and not await _d("update_registration_reaction")(
            self.scrim,
            reviewed_registration,
            approved=confirm,
        ):
            text += (
                " The registration message could not be marked "
                f"{'approved' if confirm else 'rejected'}."
            )
        if (
            confirm
            and not self.registration_request
            and not await _d("confirm_captain_role")(self.scrim, result.manager_id)
        ):
            text += " The confirmed captain role could not be updated."
        if confirm and not await _d("refresh_public_slots")(self.scrim):
            text += " The public board could not be updated."
        await _d("send_scrim_log")(
            self.scrim,
            (
                "STAFF REGISTRATION APPROVAL"
                if self.registration_request and confirm
                else "STAFF REGISTRATION REJECTION"
                if self.registration_request
                else "STAFF VALIDATION"
                if confirm
                else "STAFF RELEASE"
            ),
            f"Slot {result.number:02d} · team **{result.team_name}** · "
            f"result {_d('slot_status_emoji')(self.scrim, result)} "
            f"{_d('slot_status_label')(result)}",
        )
        await interaction.followup.send(text, ephemeral=True)
        if self.registration_request:
            await refresh_registration_queue(self.scrim)
        await _d("delete_staff_review_message")(interaction)


class RegistrationQueueView(discord.ui.View):
    def __init__(self, scrim_id):
        super().__init__(timeout=None)
        self.scrim_id = scrim_id
        for action, style, emoji in (
            ("Accept", discord.ButtonStyle.success, "✅"),
            ("Decline", discord.ButtonStyle.danger, "❌"),
        ):
            button = discord.ui.Button(
                label=action, style=style, emoji=emoji,
                custom_id=f"slots:regqueue:{scrim_id}:{action.lower()}",
            )
            button.callback = self.open_selection
            self.add_item(button)

    async def open_selection(self, interaction):
        scrim = _d("repository").get(self.scrim_id)
        if scrim is None:
            await interaction.response.send_message("This scrim no longer exists.", ephemeral=True)
            return
        if not await registration_queue_allowed(interaction, scrim):
            return
        async with scrim.state_lock:
            requests = sorted(scrim.pending_registrations.values(),
                              key=lambda request: (request.slot_number, request.request_id))
        if not requests:
            await interaction.response.send_message("No registrations are pending.", ephemeral=True)
            return
        action = "Accept" if interaction.data["custom_id"].endswith(":accept") else "Decline"
        view = RegistrationQueueSelectView(scrim.id, interaction.user.id, action, requests)
        await interaction.response.send_message(view.page_text(), view=view, ephemeral=True)


class RegistrationQueueSelectView(discord.ui.View):
    def __init__(self, scrim_id, owner_id, action, requests, page=0):
        super().__init__(timeout=180)
        self.scrim_id, self.owner_id, self.action = scrim_id, owner_id, action
        self.requests, self.page = requests, page
        chunk = requests[page * 25:(page + 1) * 25]
        selector = discord.ui.Select(
            placeholder=f"Select teams to {action.lower()}",
            min_values=1, max_values=len(chunk),
            options=[discord.SelectOption(
                label=f"{'⚠️ ' if r.ban_match_reason else ''}{r.slot_number:02d} · {r.team_name}"[:100],
                description=(
                    f"⚠️ FLAGGED: {r.ban_match_reason}"
                    if r.ban_match_reason
                    else f"{r.tag} · primary captain {r.manager_id}"
                )[:100],
                value=r.request_id,
            ) for r in chunk],
        )
        selector.callback = self.select
        self.add_item(selector)

    def page_text(self):
        return (f"**{self.action} registrations** · page {self.page + 1}/"
                f"{(len(self.requests) + 24) // 25}\n"
                "Select one or more teams. Nothing changes until you confirm.")

    async def owner_allowed(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This private selection belongs to another staff member.", ephemeral=True)
            return False
        scrim = _d("repository").get(self.scrim_id)
        if scrim is None:
            await interaction.response.send_message("This scrim no longer exists.", ephemeral=True)
            return False
        return await registration_queue_allowed(interaction, scrim, message=False)

    async def select(self, interaction):
        if not await self.owner_allowed(interaction):
            return
        ids = set(self.children[0].values)
        chosen = [r for r in self.requests if r.request_id in ids]
        if not chosen:
            await interaction.response.send_message("Select at least one pending team.", ephemeral=True)
            return
        if sum(bool(request.ban_match_reason) for request in chosen) > 1:
            await interaction.response.send_message(
                "Select flagged teams one at a time so every matching ban reason can be reviewed in full.",
                ephemeral=True,
            )
            return
        view = RegistrationQueueConfirmView(self.scrim_id, self.owner_id, self.action, chosen)
        summary = "\n".join(
            f"`{r.slot_number:02d}` "
            f"{discord.utils.escape_markdown(discord.utils.escape_mentions(r.team_name))}"
            for r in chosen
        )
        flagged_reasons = [
            f"**{discord.utils.escape_markdown(discord.utils.escape_mentions(r.team_name))}**\n"
            f"{discord.utils.escape_markdown(discord.utils.escape_mentions(r.ban_match_reason))}"
            for r in chosen
            if r.ban_match_reason
        ]
        details = None
        if flagged_reasons:
            details = discord.Embed(
                title="Flagged registration · possible ban matches",
                description="\n\n".join(flagged_reasons)[:4096],
                color=discord.Color.orange(),
            )
        await interaction.response.edit_message(
            content=(
                f"**Confirm {self.action.lower()} for {len(chosen)} team(s)?**\n"
                f"{summary}"
            )[:2000],
            embed=details,
            view=view,
        )


class RegistrationQueueConfirmView(discord.ui.View):
    def __init__(self, scrim_id, owner_id, action, requests):
        super().__init__(timeout=180)
        self.scrim_id, self.owner_id, self.action, self.requests = scrim_id, owner_id, action, requests
        confirm = discord.ui.Button(
            label=f"Confirm {action}",
            style=discord.ButtonStyle.success if action == "Accept" else discord.ButtonStyle.danger,
        )
        confirm.callback = self.confirm
        self.add_item(confirm)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self.cancel
        self.add_item(cancel)

    async def cancel(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not your review.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Review cancelled. No changes made.", view=None)

    async def confirm(self, interaction):
        scrim = _d("repository").get(self.scrim_id)
        if interaction.user.id != self.owner_id or scrim is None:
            await interaction.response.send_message("This review is no longer available.", ephemeral=True)
            return
        if not await registration_queue_allowed(interaction, scrim, message=False):
            return
        await interaction.response.defer(ephemeral=True)
        approved = self.action == "Accept"
        async with scrim.state_lock:
            valid = bool(self.requests) and _d("is_active")(scrim) and all(
                (current := scrim.pending_registrations.get(r.request_id)) == r
                and (slot := scrim.slots.get(r.slot_number)) is not None
                and slot.status == _d("STATUS_AVAILABLE")
                and slot.assignment_id == r.assignment_id
                for r in self.requests
            )
            if valid:
                with _d("repository").transaction():
                    for request in self.requests:
                        scrim.pending_registrations.pop(request.request_id)
                        if approved:
                            slot = scrim.slots[request.slot_number]
                            slot.assignment_id += 1
                            slot.status = _d("STATUS_RESERVED")
                            slot.team_name, slot.tag = request.team_name, request.tag
                            slot.manager_id = slot.captain_1_id = request.manager_id
                            slot.captain_2_id = None
        if not valid:
            await interaction.followup.send(
                "The queue changed or a slot became unavailable. Reopen the queue and select again.",
                ephemeral=True)
            return
        issues = []
        for request in self.requests:
            if approved:
                guild = interaction.guild or _d("bot").get_guild(scrim.guild_id)
                member = guild.get_member(request.manager_id) if guild else None
                if member is None and guild is not None:
                    try:
                        member = await guild.fetch_member(request.manager_id)
                    except (_d("discord").NotFound, _d("discord").Forbidden, _d("discord").HTTPException):
                        pass
                if member is None or not await _d("grant_manager_access")(scrim, member):
                    issues.append(f"Captain access for slot {request.slot_number:02d}")
            reaction_options = {"approved": approved}
            if not approved and request.ban_match_reason:
                reaction_options["banned"] = True
            if not await _d("update_registration_reaction")(
                scrim, request, **reaction_options
            ):
                issues.append(f"Reaction for slot {request.slot_number:02d}")
            await _d("send_scrim_log")(scrim,
                "STAFF REGISTRATION APPROVAL" if approved else "STAFF REGISTRATION REJECTION",
                f"Slot {request.slot_number:02d} · team **{request.team_name}** · "
                f"{'Reserved' if approved else 'Declined'}")
        if approved and not await _d("refresh_public_slots")(scrim):
            issues.append("Public slots board")
        if not await _d("refresh_registration_queue")(scrim):
            issues.append("Staff registration queue")
        _d("disable_view_items")(self)
        await interaction.edit_original_response(
            content=(f"{'✅ Accepted' if approved else '❌ Declined'} {len(self.requests)} team(s)." +
                     (f" ⚠️ Could not update: {', '.join(issues)}." if issues else "")),
            view=self)


async def apply_registration_entry(
    scrim,
    entry,
    *,
    registration_message_id: int | None = None,
):
    """Create a reserved slot or a durable unassigned registration request."""
    snapshot = None
    auto_accept = getattr(scrim, "registration_auto_accept", False) is True
    tag_match = None
    async with scrim.state_lock:
        if not _d("is_active")(scrim):
            return None, "The scrim is no longer active.", False, False
        if _d("pause_scrim_if_unconfigured")(scrim):
            return None, "This scrim is paused because required settings are missing.", False, False
        ban_match = _d("ban_system").match(
            getattr(scrim, "guild_id", 0),
            entry.team_name,
            entry.tag,
            scrim_id=getattr(scrim, "id", None),
        )
        if ban_match is not None and ban_match.is_exact:
            raise RegistrationBlockedByBan(ban_match)
        ban_match_reason = (
            "\n".join(ban_match.reasons)
            if ban_match is not None and ban_match.kind == "partial"
            else None
        )
        if ban_match_reason is not None and len(ban_match_reason) > 3500:
            raise BanStorageError(
                "This registration has too many matching ban records to review safely."
            )
        if ban_match_reason:
            auto_accept = False
        conflict = _d("registration_team_conflict")(scrim, entry.team_name, entry.member.id)
        if conflict:
            return None, conflict, False, False
        tag_match = _d("registration_tag_conflict")(scrim, entry.team_name, entry.tag)
        if tag_match is not None:
            auto_accept = False
        slot = scrim.slots.get(entry.slot_number)
        if slot is None or not _d("registration_slot_is_available")(scrim, slot):
            return None, "The selected slot is no longer available.", False, False
        if auto_accept:
            with _d("repository").transaction():
                slot.assignment_id += 1
                slot.status = _d("STATUS_RESERVED")
                slot.team_name = entry.team_name
                slot.tag = entry.tag
                slot.manager_id = entry.member.id
                slot.captain_1_id = entry.member.id
                slot.captain_2_id = None
                snapshot = slot.snapshot()
        else:
            request = _d("RegistrationRequest")(
                request_id=_d("secrets").token_hex(8),
                slot_number=entry.slot_number,
                team_name=entry.team_name,
                tag=entry.tag,
                manager_id=entry.member.id,
                assignment_id=slot.assignment_id,
                registration_message_id=registration_message_id,
                ban_match_reason=ban_match_reason,
            )
            with _d("repository").transaction():
                pending = getattr(scrim, "pending_registrations", None)
                if pending is None:
                    pending = {}
                    scrim.pending_registrations = pending
                pending[request.request_id] = request
            snapshot = _d("SlotSnapshot")(
                number=request.slot_number,
                status=_d("STATUS_PENDING"),
                team_name=request.team_name,
                tag=request.tag,
                manager_id=request.manager_id,
                assignment_id=request.assignment_id,
                captain_1_id=request.manager_id,
            )
    if auto_accept:
        access_ok = await _d("grant_manager_access")(scrim, entry.member)
        board_refreshed = await _d("refresh_public_slots")(scrim)
        await _d("send_scrim_log")(
            scrim, "TEAM REGISTERED",
            f"Slot {entry.slot_number:02d} · team **{entry.team_name}** · "
            f"captain <@{entry.member.id}>",
        )
        return snapshot, None, access_ok, board_refreshed
    notified = await notify_staff_for_registration(
        scrim, request, **({"tag_match": tag_match} if tag_match else {})
    )
    await _d("send_scrim_log")(
        scrim, "TEAM REGISTRATION REQUEST",
        f"Slot {entry.slot_number:02d} · team **{entry.team_name}** · "
        f"captain <@{entry.member.id}>",
    )
    return snapshot, None, True, notified


async def register_team(ctx, *, arguments: str) -> None:
    """Register the invoking member's team through the configured public channel."""
    scrim = _d("resolve_registration_scrim")(ctx)
    if scrim is None:
        await _d("send_private_command_feedback")(
            ctx, "This command must be used in a configured team registration channel.",
            silent=False,
        )
        return
    missing = _d("pause_scrim_if_unconfigured")(scrim)
    if missing:
        await _d("send_private_command_feedback")(
            ctx, "This scrim is paused because required settings are missing: "
            + ", ".join(missing) + ". Complete the !setup configuration before registering.",
            silent=False,
        )
        return
    if not getattr(scrim, "registration_open", True):
        await _d("send_private_command_feedback")(
            ctx, "Registrations are currently closed. Please wait for staff to reopen the registration channel.",
            silent=False,
        )
        return
    if not _d("registration_role_allows")(ctx, scrim):
        await _d("send_private_command_feedback")(
            ctx, "You do not have the role allowed to use `!register`.", silent=False
        )
        return
    try:
        team_name, tag, manager_text = parse_registration_arguments(arguments)
    except ValueError as error:
        await _d("send_private_registration_feedback")(ctx, str(error))
        return
    manager = ctx.author
    if manager_text is not None:
        try:
            manager = await _d("resolve_add_member")(ctx, manager_text)
        except (_d("commands").MemberNotFound, _d("discord").NotFound,
                _d("discord").Forbidden, _d("discord").HTTPException):
            await _d("send_private_registration_feedback")(
                ctx, "That manager mention could not be found in this server. Mention a current server member."
            )
            return
    try:
        initial_ban_match = _d("ban_system").match(
            getattr(scrim, "guild_id", getattr(ctx.guild, "id", 0)),
            team_name,
            tag,
            scrim_id=getattr(scrim, "id", None),
        )
    except BanStorageError:
        await _d("send_private_registration_feedback")(
            ctx,
            "Registration could not be accepted because the active ban list could not be checked. Please contact staff.",
            delete_command=True,
        )
        return
    if initial_ban_match is not None and initial_ban_match.is_exact:
        await _d("ban_system").add_banned_reaction(
            getattr(ctx, "message", None), scrim.guild_id
        )
        await _d("send_private_registration_feedback")(
            ctx,
            "This team is not eligible to register.",
            delete_command=True,
        )
        return
    async with scrim.state_lock:
        conflict = _d("registration_team_conflict")(scrim, team_name, manager.id)
        slot_number = None if conflict else next(
            (slot.number for slot in scrim.slots.values()
             if _d("registration_slot_is_available")(scrim, slot)), None
        )
    if conflict:
        await _d("mark_registration_declined")(ctx, scrim)
        await _d("send_private_registration_feedback")(ctx, conflict, delete_command=False)
        return
    if slot_number is None:
        await _d("send_private_command_feedback")(
            ctx, "There are no available slots for this scrim.", silent=False
        )
        return
    entry = _d("AddDraftEntry")(
        original_line=arguments.strip(), team_name=team_name,
        slot_number=slot_number, member=manager, tag=tag,
    )
    try:
        snapshot, error, access_ok, board_refreshed = await _d("apply_registration_entry")(
            scrim, entry, registration_message_id=getattr(getattr(ctx, "message", None), "id", None)
        )
    except RegistrationBlockedByBan:
        await _d("ban_system").add_banned_reaction(
            getattr(ctx, "message", None), scrim.guild_id
        )
        await _d("send_private_registration_feedback")(
            ctx,
            "This team is not eligible to register.",
            delete_command=True,
        )
        return
    except BanStorageError:
        await _d("send_private_registration_feedback")(
            ctx,
            "Registration could not be accepted because the active ban list could not be checked. Please contact staff.",
            delete_command=True,
        )
        return
    if error or snapshot is None:
        async with scrim.state_lock:
            duplicate = _d("registration_team_conflict")(scrim, team_name, manager.id)
        if duplicate == error:
            await _d("mark_registration_declined")(ctx, scrim)
        await _d("send_private_registration_feedback")(
            ctx, error or "The registration could not be completed.",
            delete_command=not (duplicate == error and error is not None),
        )
        return
    if not access_ok or not board_refreshed:
        _d("logger").warning(
            "Registration succeeded with follow-up warnings for scrim %s: access_ok=%s board_refreshed=%s",
            scrim.id, access_ok, board_refreshed,
        )
    if snapshot.status == _d("STATUS_PENDING"):
        async with scrim.state_lock:
            tag_match = _d("registration_tag_conflict")(scrim, team_name, tag)
        if tag_match:
            existing_name, existing_number, existing_status = tag_match
            safe = lambda value: _d("discord").utils.escape_markdown(
                _d("discord").utils.escape_mentions(value)
            )
            notice = (
                f"Your registration is pending staff review. The tag **{safe(tag)}** "
                f"is already used by **{safe(existing_name)}** "
                f"(slot {existing_number:02d} · {existing_status}). "
                + ("Staff have been alerted." if board_refreshed
                   else "The staff alert could not be delivered. Please contact staff.")
            )
            await _d("send_private_registration_feedback")(ctx, notice, delete_command=False)
    add_reaction = getattr(getattr(ctx, "message", None), "add_reaction", None)
    if add_reaction is not None:
        try:
            await add_reaction(
                _d("registration_success_reaction")(
                    scrim, accepted=snapshot.status == _d("STATUS_RESERVED")
                )
            )
        except _d("discord").HTTPException:
            _d("logger").exception("Could not acknowledge successful registration.")


async def retire_legacy_registration_reviews(scrim, channel) -> None:
    history = getattr(channel, "history", None)
    bot_user_id = getattr(getattr(_d("bot"), "user", None), "id", None)
    if history is None or bot_user_id is None:
        return
    async for message in history(limit=200):
        if not _d("is_active")(scrim) or getattr(getattr(message, "author", None), "id", None) != bot_user_id:
            continue
        if not any(
            str(getattr(item, "custom_id", "")).startswith(f"slots:staff:{scrim.id}:")
            and str(getattr(item, "custom_id", "")).endswith(":reg")
            for row in getattr(message, "components", ())
            for item in getattr(row, "children", ())
        ):
            continue
        note = f"\n\nReview moved to the shared queue (message {scrim.registration_review_message_id})."
        await message.edit(content=((getattr(message, "content", "") or "")[:2000 - len(note)] + note),
                            view=None, allowed_mentions=_d("discord").AllowedMentions.none())


async def refresh_registration_queue(scrim) -> bool:
    async with getattr(scrim, "board_lock", __import__("asyncio").Lock()):
        if not _d("is_active")(scrim):
            return False
        channel = await _d("staff_channel")(scrim)
        if channel is None or not _d("is_active")(scrim):
            return False
        message_id = getattr(scrim, "registration_review_message_id", None)
        if message_id is None and not getattr(scrim, "pending_registrations", {}):
            return True
        try:
            message = None
            if message_id is not None:
                try:
                    message = await channel.fetch_message(message_id)
                except _d("discord").NotFound:
                    pass
            async with scrim.state_lock:
                if not _d("is_active")(scrim):
                    return False
                content = registration_queue_content(scrim)
            if message is None:
                message = await channel.send(content, view=RegistrationQueueView(scrim.id),
                                             allowed_mentions=_d("discord").AllowedMentions.none())
                if not _d("is_active")(scrim) or type(getattr(message, "id", None)) is not int:
                    return False
                with _d("repository").transaction():
                    scrim.registration_review_message_id = message.id
                await retire_legacy_registration_reviews(scrim, channel)
            else:
                await message.edit(content=content, view=RegistrationQueueView(scrim.id),
                                   allowed_mentions=_d("discord").AllowedMentions.none())
            return True
        except (_d("discord").HTTPException, AttributeError, _d("SlotStorageError")):
            _d("logger").exception("Could not update registration queue (%s).", scrim.id)
            return False


def install_registration_commands(bot, owner: Any) -> dict[str, Any]:
    """Install registration commands and expose the review UI for one bot."""
    bind_owner(owner)
    register_impl = register_team

    async def register_command(ctx: commands.Context, *, arguments: str) -> None:
        await register_impl(ctx, arguments=arguments)

    register_command = bot.command(
        name="register", aliases=["reg"]
    )(commands.guild_only()(register_command))

    names = (
        "SlotReviewView", "RegistrationQueueView",
        "RegistrationQueueSelectView", "RegistrationQueueConfirmView",
        "registration_queue_content", "refresh_registration_queue", "apply_registration_entry",
        "parse_registration_arguments", "register_team",
    )
    return {name: globals()[name] for name in names}


def __getattr__(name: str) -> Any:
    if _owner is None:
        raise AttributeError(name)
    return getattr(_owner, name)