import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import (
    SubscriptionStatusView,
    build_subscription_embed,
    parse_auth_duration,
    support_link,
    subscription_status,
)


class SubscriptionStatusTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, *, author_id=10, owner_id=99, roles=()):
        return SimpleNamespace(
            author=SimpleNamespace(id=author_id, roles=list(roles)),
            guild=SimpleNamespace(id=123, owner_id=owner_id),
        )

    def test_unlimited_embed(self):
        embed = build_subscription_embed(
            authorized=True,
            expires_at=None,
            duration_days=0,
        )

        payload = embed.to_dict()
        self.assertEqual(payload["color"], 0xF1C40F)
        self.assertEqual(payload["fields"][0]["value"], "🟢 Active")
        self.assertEqual(
            payload["fields"][1]["value"],
            "♾️ Lifetime / Unlimited",
        )

    def test_active_embed_includes_remaining_time(self):
        embed = build_subscription_embed(
            authorized=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=2, hours=5),
            duration_days=2,
        )

        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Status"], "🟢 Active")
        self.assertIn("day(s)", values["Time Remaining"])
        self.assertIn("hour(s)", values["Time Remaining"])
        self.assertEqual(embed.color.value, 0x2ECC71)

    def test_expired_embed_includes_expiration_date(self):
        expires_at = datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)
        embed = build_subscription_embed(
            authorized=True,
            expires_at=expires_at,
            duration_days=1,
        )

        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Status"], "🔴 Expired")
        self.assertIn("Expired on 2025-01-02 03:04 UTC", values["Time Remaining"])
        self.assertEqual(embed.color.value, 0xE74C3C)

    async def test_owner_can_view_status_and_gets_support_button(self):
        ctx = self.make_context(author_id=99, owner_id=99)
        with (
            patch("main.repository.get_server_config", return_value=None),
            patch(
                "main.repository.get_guild_subscription",
                return_value=(True, None, 0),
            ),
            patch(
                "main.send_private_command_feedback",
                new=AsyncMock(),
            ) as send_feedback,
        ):
            await subscription_status.callback(ctx)

        kwargs = send_feedback.await_args.kwargs
        self.assertIsInstance(kwargs["view"], SubscriptionStatusView)
        self.assertEqual(kwargs["embed"].fields[0].value, "🟢 Active")
        button = kwargs["view"].children[0]
        self.assertEqual(button.label, "🎧 Support Server")
        self.assertEqual(
            button.url,
            "https://discord.gg/S8uaGEJGv8",
        )

    async def test_configured_bot_manager_can_view_status(self):
        ctx = self.make_context(
            roles=[SimpleNamespace(id=456)],
        )
        with (
            patch(
                "main.repository.get_server_config",
                return_value=SimpleNamespace(staff_role_id=456),
            ),
            patch(
                "main.repository.get_guild_subscription",
                return_value=(False, None, None),
            ),
            patch(
                "main.send_private_command_feedback",
                new=AsyncMock(),
            ) as send_feedback,
        ):
            await subscription_status.callback(ctx)

        self.assertEqual(
            send_feedback.await_args.kwargs["embed"].fields[0].value,
            "🔴 Expired",
        )

    async def test_other_users_are_denied(self):
        ctx = self.make_context()
        with patch(
            "main.send_private_command_feedback",
            new=AsyncMock(),
        ) as send_feedback:
            await subscription_status.callback(ctx)

        self.assertEqual(
            send_feedback.await_args.args[1],
            "❌ **Access Denied.** Only the Server Owner or a designated "
            "Bot Manager can view the subscription status.",
        )

    def test_auth_duration_accepts_days_and_unlimited(self):
        self.assertEqual(parse_auth_duration("30"), 30)
        for value in ("unlimited", "illimité", "0"):
            self.assertEqual(parse_auth_duration(value), 0)
        with self.assertRaises(ValueError):
            parse_auth_duration("-1")

    def test_status_alias_is_registered(self):
        self.assertIn("status", subscription_status.aliases)

    async def test_link_posts_raw_support_url_for_discord_preview(self):
        ctx = SimpleNamespace(
            send=AsyncMock(),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        await support_link.callback(ctx)

        ctx.send.assert_awaited_once()
        self.assertEqual(
            ctx.send.await_args.args[0],
            "https://discord.gg/S8uaGEJGv8",
        )
        self.assertNotIn("embed", ctx.send.await_args.kwargs)
        ctx.message.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()