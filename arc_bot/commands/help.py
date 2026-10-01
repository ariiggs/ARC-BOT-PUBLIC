"""Categorized help command and its interactive view."""

from __future__ import annotations

import discord
from discord.ext import commands

from arc_bot.utils.discord_views import delete_message_or_clear, disable_view_items
from arc_bot.views.base import ExpiringView
from scrim_state import MAX_MATCHES


HELP_CATEGORIES = {
    "Getting started": {
        "description": "Setup, server access, and help",
        "text": (
            "`!help` — command categories.\n"
            "`!setup` — configure scrims.\n"
            "`!set @Staff` — server Staff role.\n"
            "`!setres` — leaderboard settings.\n"
            "`!sub` — private status (Owner/Manager)."
        ),
    },
    "Scrims and slots": {
        "description": "Boards, teams, and staff operations",
        "text": (
            "`!slots [Scrim]` — view a slot summary.\n"
            "`!update [Scrim]` — publish or refresh a slot board.\n"
            "`!open` / `!close` — open or close slot interactions/registrations.\n"
            "`!add Team / TAG / @Captain` — add a team to the selected scrim.\n"
            "`!confirm 03 [04 ...]` — staff force-confirm Pending slots.\n"
            "`!remove 03 [04 ...]` — remove occupied slots.\n"
            "`!move 03 06` — move to empty slot (`!move 03` lists choices).\n"
            "`!switch 03 06` — swap occupied teams and their statuses.\n"
            "`!reset` — clear a scrim after confirming the warning.\n"
            "`!remind` — remind Pending teams to confirm.\n"
            "`!say <message>` — publish a staff announcement.\n"
            "`!msg <key> <text>` — edit an operational message."
        ),
    },
    "Registration and captains": {
        "description": "Team registration and captain tools",
        "text": (
            "`!register Team / TAG [/ @Captain]` — request a registration.\n"
            "`!cap add @User` — grant captain access.\n"
            "`!cap transfer @User` — transfer captain access.\n"
            "`!cap remove @User` — remove captain access.\n"
            "Configured roles and channels apply."
        ),
    },
    "Ban management": {
        "description": "Staff-only team bans and the public ban board",
        "copy_text": (
            "Staff-only: `!ban` opens private Manual/Live forms; every ban needs "
            "a reason. `!ban Team Name / TAG` pre-fills. "
            "`!unban Team Name / TAG` removes the ban. Configure via "
            "`!setup` → Ban System."
        ),
        "text": (
            "Staff-only; Staff role required.\n"
            "Ban setup: `!setup` → Ban System (scope, board, role).\n"
            "`!ban` — opens short-lived Manual Ban and Live Ban buttons in a staff slots "
            "channel. Manual Ban asks for Team Name, TAG, duration, and a required reason, "
            "then lets you select the Captain privately. Live Ban selects an occupied team, "
            "then asks for duration and a required reason privately. Use `<weeks> <days>` "
            "(Weeks 0–52, Days 0–7) or `perm`/`permanent` for a permanent ban. "
            "The reason appears on the public ban board.\n"
            "`!ban Team Name / TAG` — opens the manual form with Team Name and TAG prefilled. "
            "`!unban Team Name / TAG` — removes that exact ban.\n"
            "`!banconfig channel #bans` — set the public auto-updating ban board.\n"
            "`!banconfig emoji 🔨` — set the blocked-registration reaction."
        ),
    },
    "Match results": {
        "description": "Score entry, review, and leaderboard",
        "text": (
            "`!res` / `!lb` — generate the leaderboard image.\n"
            f"`!resg1-{MAX_MATCHES} slot kills` — enter results in rank order, "
            "best team first. Omit teams that did not play; review before saving.\n"
            "`!editres <match>` — choose one team and correct its rank/kills.\n"
            "Confirming a full match replaces that match's previous result set."
        ),
    },
    "Room ID and password": {
        "description": "Fixed or per-match room access details",
        "text": (
            "`!idpw <room> / <minutes>` — saved password.\n"
            "`!idpw <room> / <password> / <minutes>` — dynamic password.\n"
            f"`!idpwg1-{MAX_MATCHES}` accepts the same formats for a specific "
            "match; reminder settings come from scrim setup."
        ),
    },
}

HELP_COPY_TEXT = "A.R.C. HELP\n\n" + "\n\n".join(
    f"{category.upper()}\n{details.get('copy_text', details['text'])}"
    for category, details in HELP_CATEGORIES.items()
) + "\n\nUse `!` for every command. Configured roles and channels apply."


def build_help_text(category: str | None = None) -> str:
    """Return the selected help category or the interactive panel prompt."""
    if category not in HELP_CATEGORIES:
        return (
            ">>> **A.R.C. HELP**\n"
            "Choose a category below to see commands and their formats. "
            "Select one to enable **Copy text** for that category."
        )
    details = HELP_CATEGORIES[category]
    return (
        f">>> **A.R.C. HELP · {category.upper()}**\n"
        f"{details['text']}\n\n"
        "_Use `!` for every command. Configured roles and channels apply._"
    )


class HelpView(ExpiringView):
    """Owner-only controls for the categorized help panel."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.selected_category: str | None = None
        self.copy_text_button = next(
            item
            for item in self.children
            if isinstance(item, discord.ui.Button)
            and item.label == "Copy text"
        )
        self.copy_text_button.disabled = True
        self.timeout_notice = "⏱️ This help panel expired. Run `!help` again."
        self.message: discord.Message | None = None
        category_select = discord.ui.Select(
            placeholder="Choose a help category...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=category,
                    value=category,
                    description=details["description"],
                )
                for category, details in HELP_CATEGORIES.items()
            ],
        )

        async def select_category(interaction: discord.Interaction) -> None:
            category = category_select.values[0]
            previous_category = self.selected_category
            self.selected_category = category
            self.copy_text_button.disabled = False
            try:
                await interaction.response.edit_message(
                    content=build_help_text(category),
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                self.selected_category = previous_category
                self.copy_text_button.disabled = previous_category is None
                raise

        category_select.callback = select_category
        self.add_item(category_select)

    async def on_timeout(self) -> None:
        disable_view_items(self)
        await delete_message_or_clear(
            self.message,
            log_context="expired help panel",
            fallback_content="This help panel expired.",
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "This help panel belongs to another user.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Copy text",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
    )
    async def copy_text(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.selected_category not in HELP_CATEGORIES:
            await interaction.response.send_message(
                "Select a help category before copying its text.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            build_help_text(self.selected_category),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Close",
        emoji="✖️",
        style=discord.ButtonStyle.danger,
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.send_message(
            "Help closed.",
            ephemeral=True,
        )
        await delete_message_or_clear(
            getattr(interaction, "message", None),
            log_context="closed help panel",
            fallback_content="This help panel has ended.",
        )


def create_help_command(send_feedback):
    """Create the prefix command with the app's privacy-aware feedback helper."""

    @commands.command(name="help", aliases=["h"])
    async def help_command(ctx: commands.Context) -> None:
        view = HelpView(ctx.author.id)
        view.message = await send_feedback(
            ctx,
            build_help_text(),
            delete_after=None,
            view=view,
        )

    return help_command