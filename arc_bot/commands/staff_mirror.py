"""Standalone staff-mirror controls bound to one bot entry point."""
from __future__ import annotations

import hashlib
import json
import re
from types import ModuleType
import discord
from discord.ext import commands

_owner: ModuleType | None = None
_legacy_functions = {}

def _d(name):
    return getattr(_owner, name)

def staff_slot_identity(slot):
    state = {k: getattr(slot, k, None) for k in (
        "assignment_id", "status", "team_name", "tag", "manager_id",
        "captain_1_id", "captain_2_id")}
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]

def staff_slot_pair_identity(source, destination):
    return hashlib.sha256(f"{staff_slot_identity(source)}:{staff_slot_identity(destination)}".encode()).hexdigest()[:20]

async def staff_mirror_interaction_allowed(interaction, scrim, *, require_mirror_message=False):
    allowed = (_d("is_active")(scrim) and _d("repository").is_guild_authorized(scrim.guild_id)
        and getattr(interaction, "guild", None) is not None
        and interaction.guild.id == scrim.guild_id
        and interaction.channel_id == scrim.staff_channel_id
        and _d("member_is_staff")(interaction.user, scrim)
        and (not require_mirror_message or getattr(getattr(interaction, "message", None), "id", None) == scrim.staff_message_id))
    if not allowed:
        await interaction.response.send_message("These controls are for staff in this scrim's configured staff channel.", ephemeral=True)
    return allowed

class StaffMirrorView(discord.ui.View):
    def __init__(self, scrim):
        super().__init__(timeout=None); self.scrim_id = scrim.id
        options = [discord.SelectOption(label=f"{s.number:02d} · {discord.utils.escape_markdown(s.team_name or 'Available')}"[:100], value=str(s.number), description=s.status.replace("_", " ").title()[:100]) for s in list(scrim.slots.values())[:25]]
        select = discord.ui.Select(placeholder="Select a slot to manage", options=options or [discord.SelectOption(label="No slots configured", value="0")], custom_id="slots:staffmirror:select")
        select.callback = self.select_slot; self.add_item(select)
    async def select_slot(self, interaction):
        scrim = _d("repository").get(self.scrim_id)
        if scrim is None or not await _d("staff_mirror_interaction_allowed")(interaction, scrim, require_mirror_message=True): return
        try: number = int(self.children[0].values[0])
        except (ValueError, IndexError):
            await interaction.response.send_message("Select a valid slot.", ephemeral=True); return
        slot = scrim.slots.get(number)
        if slot is None:
            await interaction.response.send_message("That slot no longer exists.", ephemeral=True); return
        snapshot = slot.snapshot()
        await interaction.response.send_message(content=f"Staff actions for slot **{number:02d}** · **{discord.utils.escape_markdown(snapshot.team_name or 'Available')}** ({_d('slot_status_label')(snapshot)}).", view=StaffMirrorActionView(scrim.id, snapshot), ephemeral=True)

class StaffMirrorActionView(discord.ui.View):
    def __init__(self, scrim_id, slot):
        super().__init__(timeout=None)
        for action in ("confirm", "remove", "move", "switch"):
            self.add_item(StaffMirrorActionButton(scrim_id, slot.number, slot.assignment_id, staff_slot_identity(slot), action))

class StaffMirrorRemoveConfirmationView(discord.ui.View):
    def __init__(self, scrim_id, slot_number, assignment_id, expected_identity):
        super().__init__(timeout=None)
        self.add_item(StaffMirrorActionButton(scrim_id, slot_number, assignment_id, expected_identity, "remove_confirm"))
        self.add_item(StaffMirrorActionButton(scrim_id, slot_number, assignment_id, expected_identity, "remove_cancel"))

class StaffMirrorActionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"slots:mirror:(?P<scrim>[a-f0-9]{16}):(?P<number>[0-9]{1,2}):(?P<assignment>[0-9]+):(?P<identity>[a-f0-9]{20}):(?P<action>confirm|remove|move|switch|remove_confirm|remove_cancel)"):
    def __init__(self, scrim_id, number, assignment_id, expected_identity, action):
        self.scrim_id, self.number, self.assignment_id = scrim_id, number, assignment_id
        self.expected_identity, self.action = expected_identity, action
        labels = {"confirm": ("Force Confirm", discord.ButtonStyle.success, "✅"), "remove": ("Remove", discord.ButtonStyle.danger, "🗑️"), "move": ("Move Slot", discord.ButtonStyle.primary, "➡️"), "switch": ("Switch Slots", discord.ButtonStyle.secondary, "🔄"), "remove_confirm": ("Confirm Remove", discord.ButtonStyle.danger, "⚠️"), "remove_cancel": ("Cancel", discord.ButtonStyle.secondary, "↩️")}
        label, style, emoji = labels[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"slots:mirror:{scrim_id}:{number}:{assignment_id}:{expected_identity}:{action}"))
    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["scrim"], int(match["number"]), int(match["assignment"]), match["identity"], match["action"])
    async def callback(self, interaction):
        scrim = _d("repository").get(self.scrim_id)
        if scrim is None:
            await interaction.response.send_message("That scrim no longer exists.", ephemeral=True); return
        if not await _d("staff_mirror_interaction_allowed")(interaction, scrim): return
        slot = scrim.slots.get(self.number)
        if self.action == "remove_cancel":
            await interaction.response.edit_message(content="Removal cancelled.", view=None); return
        if slot is None or slot.assignment_id != self.assignment_id or staff_slot_identity(slot) != self.expected_identity or str(slot.status).casefold() == "available":
            await interaction.response.send_message("That slot has changed. Select it again from the staff mirror.", ephemeral=True); return
        if self.action == "remove":
            await interaction.response.send_message(f"Remove **{discord.utils.escape_markdown(slot.team_name)}** from slot **{self.number:02d}**? This releases the assignment.", view=StaffMirrorRemoveConfirmationView(self.scrim_id, self.number, self.assignment_id, self.expected_identity), ephemeral=True); return
        if self.action in {"move", "switch"}:
            await interaction.response.send_modal(StaffMirrorDestinationModal(self.scrim_id, self.number, self.assignment_id, self.expected_identity, self.action)); return
        await perform_staff_mirror_action(interaction, scrim, self.number, self.assignment_id, "remove" if self.action == "remove_confirm" else self.action, expected_identity=self.expected_identity)

class StaffMirrorDestinationModal(discord.ui.Modal):
    destination = discord.ui.TextInput(label="Destination slot", placeholder="Enter the slot number", min_length=1, max_length=2)
    def __init__(self, scrim_id, source, assignment_id, expected_identity, action):
        super().__init__(title="Move team to a slot" if action == "move" else "Switch slot assignments", timeout=300)
        self.scrim_id, self.source, self.assignment_id, self.expected_identity, self.action = scrim_id, source, assignment_id, expected_identity, action
    async def on_submit(self, interaction):
        scrim = _d("repository").get(self.scrim_id)
        if scrim is None or not await _d("staff_mirror_interaction_allowed")(interaction, scrim): return
        try: destination = int(str(self.destination.value).strip())
        except ValueError:
            await interaction.response.send_message("Enter a valid destination slot number.", ephemeral=True); return
        await prepare_staff_mirror_transfer(interaction, scrim, self.source, self.assignment_id, self.action, self.expected_identity, destination=destination)

class StaffMirrorTransferConfirmationView(discord.ui.View):
    def __init__(self, scrim_id, source, destination, action, pair_identity):
        super().__init__(timeout=None)
        self.add_item(StaffMirrorTransferButton(scrim_id, source.number, source.assignment_id, destination.number, destination.assignment_id, pair_identity, f"{action}_confirm"))
        self.add_item(StaffMirrorTransferButton(scrim_id, source.number, source.assignment_id, destination.number, destination.assignment_id, pair_identity, "cancel"))

class StaffMirrorTransferButton(discord.ui.DynamicItem[discord.ui.Button], template=r"slots:transfer:(?P<scrim>[a-f0-9]{16}):(?P<source>[0-9]{1,2}):(?P<srcgen>[0-9]+):(?P<destination>[0-9]{1,2}):(?P<dstgen>[0-9]+):(?P<pair>[a-f0-9]{20}):(?P<action>move_confirm|switch_confirm|cancel)"):
    def __init__(self, scrim_id, source, source_generation, destination, destination_generation, pair_identity, action):
        self.scrim_id, self.source, self.source_generation, self.destination, self.destination_generation, self.pair_identity, self.action = scrim_id, source, source_generation, destination, destination_generation, pair_identity, action
        label, style = {"move_confirm": ("Confirm Move", discord.ButtonStyle.success), "switch_confirm": ("Confirm Switch", discord.ButtonStyle.success), "cancel": ("Cancel", discord.ButtonStyle.secondary)}[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"slots:transfer:{scrim_id}:{source}:{source_generation}:{destination}:{destination_generation}:{pair_identity}:{action}"))
    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["scrim"], int(match["source"]), int(match["srcgen"]), int(match["destination"]), int(match["dstgen"]), match["pair"], match["action"])
    async def callback(self, interaction):
        scrim = _d("repository").get(self.scrim_id)
        if scrim is None or not await _d("staff_mirror_interaction_allowed")(interaction, scrim): return
        if self.action == "cancel":
            await interaction.response.edit_message(content="Transfer cancelled.", view=None); return
        source, target = scrim.slots.get(self.source), scrim.slots.get(self.destination)
        if source is None or target is None:
            await interaction.response.send_message("One of those slots no longer exists.", ephemeral=True); return
        await perform_staff_mirror_action(interaction, scrim, self.source, self.source_generation, self.action.removesuffix("_confirm"), expected_identity=staff_slot_identity(source), destination=self.destination, expected_destination_generation=self.destination_generation, expected_pair_identity=self.pair_identity)

async def prepare_staff_mirror_transfer(*args, **kwargs):
    return await _invoke_legacy("prepare_staff_mirror_transfer", *args, **kwargs)

async def perform_staff_mirror_action(*args, **kwargs):
    return await _invoke_legacy("perform_staff_mirror_action", *args, **kwargs)

async def _invoke_legacy(name, *args, **kwargs):
    """Compatibility bridge for callbacks retained by older integrations."""
    function = _legacy_functions.get(name) or getattr(_owner, name)
    return await function(*args, **kwargs)

def install_staff_mirror_commands(bot, owner):
    global _owner, _legacy_functions
    _owner = owner
    _legacy_functions = {
        name: getattr(owner, name)
        for name in ("prepare_staff_mirror_transfer", "perform_staff_mirror_action")
    }
    bot.remove_command("move"); bot.remove_command("switch")
    @bot.command(name="move")
    @commands.guild_only()
    async def move_team(ctx, *, slot_numbers=""):
        return await owner.transfer_slots_command(ctx, action="move", slot_numbers=slot_numbers)
    @bot.command(name="switch")
    @commands.guild_only()
    async def switch_teams(ctx, *, slot_numbers=""):
        return await owner.transfer_slots_command(ctx, action="switch", slot_numbers=slot_numbers)
    return {name: globals()[name] for name in ("StaffMirrorView", "StaffMirrorActionView", "StaffMirrorRemoveConfirmationView", "StaffMirrorActionButton", "StaffMirrorDestinationModal", "StaffMirrorTransferConfirmationView", "StaffMirrorTransferButton", "staff_mirror_interaction_allowed", "staff_slot_identity", "staff_slot_pair_identity", "prepare_staff_mirror_transfer", "perform_staff_mirror_action")} | {"move_team": move_team, "switch_teams": switch_teams}