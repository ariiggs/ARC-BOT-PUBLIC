"""Captain and staff slot commands owned by one bot entry point.

This module intentionally contains the command implementations rather than an
adapter to ``main.py``.  Discord/storage collaborators are resolved through
the bound entry point so Beta and Public retain independent state.
"""
from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord.ext import commands

from scrim_state import (
    STATUS_PENDING,
    STATUS_RESERVED,
    Slot,
    SlotSnapshot,
    Scrim,
)

_owner: Any = None


def bind_owner(owner: Any) -> None:
    global _owner
    _owner = owner


def _o(name: str) -> Any:
    return getattr(_owner, name)


def _sync() -> None:
    """Refresh patchable collaborators used by tests and integrations."""
    for name in (
        "repository", "logger", "require_staff_scrim", "is_active",
        "refresh_public_slots", "send_scrim_log", "send_private_command_feedback",
        "delete_command_message", "confirm_captain_role",
        "revoke_manager_access_if_unused", "grant_manager_access",
        "captain_assignments", "cap_channel_is_allowed", "cap_role_is_allowed",
        "captain_slot_id", "CAP_CHANNEL_RESTRICTION_MESSAGE",
        "CAP_ROLE_RESTRICTION_MESSAGE", "cap_feedback_mentions",
        "slot_is_assignable", "pause_scrim_if_unconfigured",
        "delete_message_or_clear", "disable_view_items",
    ):
        if hasattr(_owner, name):
            globals()[name] = getattr(_owner, name)


def _parse_numbers(value: str) -> list[int]:
    tokens = value.split()
    if not tokens or any(not token.isdigit() for token in tokens):
        raise ValueError("Slot numbers must be separated by spaces.")
    numbers = [int(token) for token in tokens]
    if len(set(numbers)) != len(numbers):
        raise ValueError("Do not repeat the same slot number.")
    return numbers


def _refresh_cap_board(scrim: Scrim) -> bool:
    try:
        return bool(asyncio.get_event_loop().run_until_complete(_o("publish_scrim")(scrim)))
    except RuntimeError:
        return True


async def refresh_cap_board(scrim: Scrim) -> bool:
    try:
        return await _o("publish_scrim")(scrim)
    except Exception as error:
        _o("logger").exception(
            "Could not refresh the board after a captain change.", exc_info=error
        )
        return False


class CaptainSlotSelectView(discord.ui.View):
    def __init__(self, ctx, action: str, target_user, assignments):
        super().__init__(timeout=120)
        _sync()
        self.ctx, self.owner_id = ctx, ctx.author.id
        self.guild_id, self.action, self.target_user = ctx.guild.id, action, target_user
        self.message = None
        self.processing = False
        self.assignments = {
            captain_slot_id(scrim, slot): (scrim, slot, position, slot.assignment_id)
            for scrim, slot, position in assignments
        }
        select = discord.ui.Select(
            placeholder="Select a slot to apply this command...",
            options=[
                discord.SelectOption(
                    label=f"Slot {slot.number} - {slot.team_name}"[:100],
                    value=slot_id,
                )
                for slot_id, (_scrim, slot, _position, _generation)
                in self.assignments.items()
            ],
        )

        async def callback(interaction):
            _sync()
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "This slot selector belongs to another user.", ephemeral=True
                )
                return
            if self.processing:
                await interaction.response.send_message(
                    "This slot command is already being processed.", ephemeral=True
                )
                return
            selected = self.assignments.get(select.values[0])
            if selected is None:
                self.stop()
                await interaction.response.send_message(
                    "This slot selection is no longer available.", ephemeral=True
                )
                return
            scrim, slot, _position, generation = selected
            current = next(
                (
                    assignment
                    for assignment in captain_assignments(
                        self.guild_id, self.owner_id
                    )
                    if captain_slot_id(assignment[0], assignment[1]) == select.values[0]
                ),
                None,
            )
            if (
                current is None
                or current[0] is not scrim
                or current[1] is not slot
                or current[1].assignment_id != generation
            ):
                await interaction.response.send_message(
                    "This slot assignment changed before the command was applied.",
                    ephemeral=True,
                )
                return
            if not cap_channel_is_allowed(
                self.guild_id, interaction.channel_id, scrim
            ) or not cap_role_is_allowed(interaction.user, self.guild_id, scrim):
                await interaction.response.send_message(
                    CAP_CHANNEL_RESTRICTION_MESSAGE, ephemeral=True
                )
                return
            self.processing = True
            self.stop()
            success, message = await execute_cap_action(
                self.action, self.ctx, self.target_user, current
            )
            await interaction.response.send_message(
                (
                    f"✅ Command successfully applied to Slot "
                    f"{slot.number} ({slot.team_name})."
                    if success else message or "The command could not be applied."
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        select.callback = callback
        self.add_item(select)

    async def on_timeout(self):
        _sync()
        disable_view_items(self)
        await delete_message_or_clear(
            self.message,
            log_context="expired captain slot selector",
            fallback_content="This slot selector has expired.",
        )


async def send_captain_slot_selector(ctx, action, target_user, assignments):
    view = CaptainSlotSelectView(ctx, action, target_user, assignments)
    kwargs = {"view": view, "allowed_mentions": discord.AllowedMentions.none()}
    interaction = getattr(ctx, "interaction", None)
    kwargs["ephemeral" if interaction is not None and not interaction.is_expired()
            else "delete_after"] = True if "ephemeral" in kwargs else 120
    try:
        view.message = await ctx.send("Select a slot to apply this command...", **kwargs)
    except discord.HTTPException:
        _o("logger").exception("Could not send the captain slot selector.")
    await delete_command_message(ctx)


async def captain_assignment_or_selection(ctx, action, target_user):
    _sync()
    assignments = captain_assignments(ctx.guild.id, ctx.author.id)
    if not assignments:
        await send_private_command_feedback(
            ctx, "❌ You are not a captain of any registered slot.", silent=False
        )
        return None
    if len(assignments) == 1:
        return assignments[0]
    if len(assignments) > 25:
        await send_private_command_feedback(
            ctx, "You are registered as captain for too many slots to display in one selector.",
            silent=False,
        )
        return None
    await send_captain_slot_selector(ctx, action, target_user, assignments)
    return None


async def perform_cap_add(ctx, member, assignment):
    _sync()
    scrim, slot, _position = assignment
    if slot.captain_2_id is not None:
        return False, "❌ **Limit Reached.** Your team already has 2 captains."
    if member.id in {slot.captain_1_id, slot.captain_2_id}:
        return False, "That user is already a captain for your team."
    if captain_assignments(ctx.guild.id, member.id):
        return False, "That user is already registered as a captain for another team."
    async with scrim.state_lock:
        if not is_active(scrim) or scrim.slots.get(slot.number) is not slot:
            return False, "This slot assignment changed before the command was applied."
        if not await grant_manager_access(scrim, member):
            return False, "The co-captain could not be granted the Captain role and team access."
        with repository.transaction():
            slot.captain_2_id = member.id
    refreshed = await refresh_cap_board(scrim)
    message = f"✅ **Co-Captain Added.** <@{member.id}> is now a co-captain for your team!"
    if not refreshed:
        message += " The slot board could not be refreshed."
    await send_scrim_log(scrim, "CAPTAIN ADDED", f"Slot {slot.number:02d} · team **{slot.team_name}**.")
    return True, message


async def perform_cap_transfer(ctx, member, assignment):
    _sync()
    scrim, slot, position = assignment
    if member.id == ctx.author.id:
        return False, "You cannot transfer the captain role to yourself."
    if member.id in {slot.captain_1_id, slot.captain_2_id}:
        return False, "That user is already a captain for your team."
    async with scrim.state_lock:
        if not is_active(scrim) or scrim.slots.get(slot.number) is not slot:
            return False, "This slot assignment changed before the command was applied."
        if not await grant_manager_access(scrim, member):
            return False, "The leadership transfer could not grant the Captain role and team access."
        with repository.transaction():
            if position == 1:
                slot.captain_1_id, slot.manager_id = member.id, member.id
            else:
                slot.captain_2_id = member.id
    await revoke_manager_access_if_unused(scrim, ctx.author.id, member=ctx.author)
    refreshed = await refresh_cap_board(scrim)
    message = f"✅ **Leadership Transferred** to <@{member.id}>."
    if not refreshed:
        message += " The slot board could not be refreshed."
    return True, message


async def perform_cap_remove(ctx, member, assignment):
    _sync()
    scrim, slot, position = assignment
    if member.id == ctx.author.id:
        return False, "You cannot remove yourself with this command."
    if position == 1 and slot.captain_2_id != member.id:
        return False, "That user is not your co-captain for this team."
    if position == 2 and slot.captain_1_id != member.id:
        return False, "As co-captain, you can only remove the primary captain to take over."
    async with scrim.state_lock:
        if not is_active(scrim) or scrim.slots.get(slot.number) is not slot:
            return False, "This slot assignment changed before the command was applied."
        with repository.transaction():
            if position == 1:
                slot.captain_2_id = None
            else:
                slot.captain_1_id, slot.manager_id, slot.captain_2_id = ctx.author.id, ctx.author.id, None
    await revoke_manager_access_if_unused(scrim, member.id, member=member)
    refreshed = await refresh_cap_board(scrim)
    message = (
        f"✅ **Co-Captain Removed.** <@{member.id}> is no longer a co-captain for your team."
        if position == 1 else
        f"✅ **Captain Role Taken Over.** <@{member.id}> was removed and you are now the primary captain."
    )
    if not refreshed:
        message += " The slot board could not be refreshed."
    return True, message


async def execute_cap_action(action, ctx, target_user, assignment):
    return await {
        "add": perform_cap_add,
        "transfer": perform_cap_transfer,
        "remove": perform_cap_remove,
    }.get(action, lambda *_: (False, "This captain command is not supported."))(
        ctx, target_user, assignment
    )


async def _cap_command(ctx, action, member):
    if not await require_cap_channel(ctx):
        return
    assignment = await captain_assignment_or_selection(ctx, action, member)
    if assignment is None:
        return
    if not await require_cap_channel(ctx, assignment[0]):
        return
    success, message = await execute_cap_action(action, ctx, member, assignment)
    await send_private_command_feedback(
        ctx, message or "", **({"allowed_mentions": cap_feedback_mentions()} if success else {})
    )


async def cap_command(ctx):
    _sync()
    if await require_cap_channel(ctx):
        await send_private_command_feedback(
            ctx, "Use `!cap add @User`, `!cap transfer @User`, or `!cap remove @User`.",
            silent=False,
        )


async def cap_add(ctx, member):
    await _cap_command(ctx, "add", member)


async def cap_transfer(ctx, member):
    await _cap_command(ctx, "transfer", member)


async def cap_remove(ctx, member):
    await _cap_command(ctx, "remove", member)


async def remind_managers(ctx):
    _sync()
    scrim = await require_staff_scrim(ctx, allow_public=True)
    if scrim is None:
        return
    reserved = [slot.snapshot() for slot in scrim.slots.values()
                if slot.status == STATUS_RESERVED and slot.manager_id is not None]
    if not reserved:
        embed = discord.Embed(title="✅ No reminder needed",
                              description="There are no Reserved slots waiting for captain confirmation.",
                              color=discord.Color.blue())
        embed.set_footer(text=f"Scrim: {scrim.name}")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), delete_after=30)
        await delete_command_message(ctx)
        return
    if scrim.pending_role_id is None:
        await send_private_command_feedback(ctx, "This scrim has no Pending Captain Role configured.", silent=False)
        return
    embed = discord.Embed(title="🔔 Slot Confirmation Reminder", color=discord.Color.blue())
    embed.add_field(name="Slots waiting", value=", ".join(f"`{slot.number:02d}`" for slot in reserved), inline=False)
    embed.set_footer(text=f"Scrim: {scrim.name}")
    await ctx.send(content="***Reserved teams: please Confirm or Cancel your Slot.***\n"
                   f"<@&{scrim.pending_role_id}>", embed=embed,
                   allowed_mentions=discord.AllowedMentions(roles=True))
    await delete_command_message(ctx)


async def confirm_slot(ctx, *, slot_numbers):
    _sync()
    scrim = await require_staff_scrim(ctx, allow_public=True, silent=False)
    if scrim is None:
        return
    try:
        numbers = _parse_numbers(slot_numbers)
    except ValueError:
        await send_private_command_feedback(ctx, "Invalid slot number. Use `!confirm <slot>` with a valid slot number.")
        return
    async with scrim.state_lock:
        if not is_active(scrim):
            await send_private_command_feedback(ctx, "This scrim no longer exists.")
            return
        slots = [scrim.slots.get(number) for number in numbers]
        if any(slot is None or slot.status not in {STATUS_RESERVED, STATUS_PENDING} for slot in slots):
            await send_private_command_feedback(ctx, "Only Reserved or Pending slots can be confirmed.")
            return
        with repository.transaction():
            confirmed = []
            for slot in slots:
                slot.assignment_id += 1; slot.status = "Confirmé"; confirmed.append(slot.snapshot())
    managers = [slot for slot in confirmed if slot.manager_id is not None]
    updates = await asyncio.gather(*(confirm_captain_role(scrim, slot.manager_id) for slot in managers))
    refreshed = await refresh_public_slots(scrim)
    details = ", ".join(f"{slot.number:02d} · team **{slot.team_name}**" for slot in confirmed)
    result = [f"✅ Confirmed slot(s): {details}."]
    if any(not result for result in updates):
        result.append("⚠️ Captain roles could not be synchronized.")
    if not refreshed:
        result.append("⚠️ The slot board could not be refreshed. Check bot permissions and run `!update`.")
    await send_private_command_feedback(ctx, "\n".join(result), delete_after=30)
    await send_scrim_log(scrim, "SLOTS FORCE CONFIRMED", details)


async def remove_team(ctx, *, slot_numbers):
    _sync()
    scrim = await require_staff_scrim(ctx, silent=False)
    if scrim is None:
        return
    try:
        numbers = _parse_numbers(slot_numbers)
    except ValueError as error:
        await send_private_command_feedback(ctx, f"❌ {error} Use `!remove <slot>` or `!remove <slot> <slot> ...`.", silent=False)
        return
    async with scrim.state_lock:
        if not is_active(scrim):
            await send_private_command_feedback(ctx, "This scrim no longer exists. No slots were changed.", silent=False); return
        slots = [scrim.slots.get(number) for number in numbers]
        if any(slot is None or slot_is_assignable(slot) for slot in slots):
            invalid = [number for number, slot in zip(numbers, slots) if slot is None or slot_is_assignable(slot)]
            await send_private_command_feedback(ctx, "No changes were made. These slots are invalid or unassigned: " + ", ".join(f"{n:02d}" for n in invalid) + ".", silent=False); return
        with repository.transaction():
            removed = [slot.snapshot() for slot in slots]
            for slot in slots: slot.clear()
    failures = []
    for captain_id in {cid for slot in removed for cid in (slot.captain_1_id, slot.captain_2_id) if cid is not None}:
        if not await revoke_manager_access_if_unused(scrim, captain_id): failures.append(captain_id)
    refreshed = await refresh_public_slots(scrim)
    details = ", ".join(f"{slot.number:02d} · team **{slot.team_name}**" for slot in removed)
    result = [f"✅ Removed slot(s): {details}."]
    if failures: result.append("⚠️ Captain access could not be synchronized.")
    if not refreshed: result.append("⚠️ The slot board could not be refreshed. Check bot permissions and run `!update`.")
    await send_private_command_feedback(ctx, "\n".join(result), silent=False, delete_after=30)
    await send_scrim_log(scrim, "SLOTS FORCE REMOVED", details)
    await delete_command_message(ctx)


def install_staff_slot_commands(bot, owner):
    bind_owner(owner); _sync()
    bot.remove_command("cap"); bot.remove_command("remind")
    bot.remove_command("confirm"); bot.remove_command("remove")
    cap = bot.group(name="cap", invoke_without_command=True, hidden=True)(commands.guild_only()(cap_command))
    cap.command(name="add")(commands.guild_only()(cap_add))
    cap.command(name="transfer")(commands.guild_only()(cap_transfer))
    cap.command(name="remove")(commands.guild_only()(cap_remove))
    bot.command(name="remind", aliases=["rem"])(commands.guild_only()(remind_managers))
    bot.command(name="confirm", aliases=["cf"])(commands.guild_only()(confirm_slot))
    bot.command(name="remove", aliases=["rm"])(commands.guild_only()(remove_team))
    return {name: globals()[name] for name in (
        "CaptainSlotSelectView", "refresh_cap_board", "send_captain_slot_selector",
        "captain_assignment_or_selection", "perform_cap_add", "perform_cap_transfer",
        "perform_cap_remove", "execute_cap_action",
        "cap_command", "cap_add", "cap_transfer", "cap_remove", "remind_managers",
        "confirm_slot", "remove_team", "_parse_numbers",
    )}
