"""Subscription status and support-link commands."""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord.ext import commands


SUPPORT_SERVER_URL = "https://discord.gg/S8uaGEJGv8"


class SubscriptionStatusView(discord.ui.View):
    """Link-only view attached to subscription status messages."""

    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="🎧 Support Server",
                url=SUPPORT_SERVER_URL,
            )
        )


def subscription_remaining_text(expires_at: datetime) -> str:
    remaining_seconds = max(
        0, int((expires_at - datetime.now(timezone.utc)).total_seconds())
    )
    days, remainder = divmod(remaining_seconds, 24 * 60 * 60)
    hours = remainder // (60 * 60)
    return f"{days} day(s), {hours} hour(s) remaining"


def build_subscription_embed(
    *, authorized: bool, expires_at: datetime | None, duration_days: int | None
) -> discord.Embed:
    if authorized and duration_days == 0 and expires_at is None:
        embed = discord.Embed(
            title="💎 **A.R.C. Subscription Status**",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Status", value="🟢 Active", inline=False)
        embed.add_field(
            name="Time Remaining",
            value="♾️ Lifetime / Unlimited",
            inline=False,
        )
        return embed

    if authorized and expires_at is not None:
        expires_at = expires_at.astimezone(timezone.utc)
        if expires_at > datetime.now(timezone.utc):
            embed = discord.Embed(
                title="💎 **A.R.C. Subscription Status**",
                color=discord.Color.green(),
            )
            embed.add_field(name="Status", value="🟢 Active", inline=False)
            embed.add_field(
                name="Time Remaining",
                value=subscription_remaining_text(expires_at),
                inline=False,
            )
            return embed
        expired_text = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    else:
        expired_text = "Expired"

    embed = discord.Embed(
        title="💎 **A.R.C. Subscription Status**",
        color=discord.Color.red(),
    )
    embed.add_field(name="Status", value="🔴 Expired", inline=False)
    embed.add_field(
        name="Time Remaining",
        value=(
            f"Expired on {expired_text}"
            if expired_text != "Expired"
            else "Expired"
        ),
        inline=False,
    )
    return embed


def create_subscription_command(
    repository,
    send_dm_feedback,
    send_private_feedback,
    delete_command_message,
):
    """Build the status command with explicit app dependencies."""

    @commands.command(name="sub", aliases=["status"])
    async def subscription_status(ctx: commands.Context) -> None:
        access_denied = (
            "❌ **Access Denied.** Only the Server Owner or a designated "
            "Bot Manager can view the subscription status."
        )
        if ctx.guild is None:
            await send_dm_feedback(ctx, access_denied)
            return

        config = repository.get_server_config(ctx.guild.id)
        role_ids = {
            getattr(role, "id", None) for role in getattr(ctx.author, "roles", ())
        }
        is_owner = getattr(ctx.guild, "owner_id", None) == getattr(ctx.author, "id", None)
        manager_role_id = (
            getattr(config, "manager_role_id", None)
            if config is not None
            else None
        )
        if manager_role_id is None and config is not None:
            # !set currently persists the designated Bot Manager role as staff_role_id.
            manager_role_id = config.staff_role_id
        is_manager = bool(
            manager_role_id is not None and manager_role_id in role_ids
        )
        if not is_owner and not is_manager:
            await send_dm_feedback(ctx, access_denied)
            return

        authorized, expires_at, duration_days = repository.get_guild_subscription(
            ctx.guild.id
        )
        try:
            await ctx.author.send(
                embed=build_subscription_embed(
                    authorized=authorized,
                    expires_at=expires_at,
                    duration_days=duration_days,
                ),
                view=SubscriptionStatusView(),
                delete_after=60,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await send_private_feedback(
                ctx,
                "I could not DM the subscription status. Enable DMs from this server "
                "and run `!sub` again.",
                delete_after=30,
            )
        finally:
            await delete_command_message(ctx)

    return subscription_status


def create_support_link_command(delete_command_message):
    """Build the support link command as raw text for Discord's link preview."""

    @commands.command(name="link")
    async def support_link(ctx: commands.Context) -> None:
        await ctx.send(
            SUPPORT_SERVER_URL,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await delete_command_message(ctx)

    return support_link