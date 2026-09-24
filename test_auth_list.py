import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from main import auth_list, authorized_guild_name, bot, SubscriptionStatusView


class AuthListTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_cached_guild_name(self):
        guild = SimpleNamespace(name="A.R.C. Scrims")
        with patch.object(bot, "get_guild", return_value=guild):
            self.assertEqual(
                await authorized_guild_name(123),
                "A.R.C. Scrims",
            )

    async def test_fetches_guild_name_when_not_cached(self):
        guild = SimpleNamespace(name="Fetched Scrims")
        with (
            patch.object(bot, "get_guild", return_value=None),
            patch.object(bot, "fetch_guild", new=AsyncMock(return_value=guild)),
        ):
            self.assertEqual(
                await authorized_guild_name(456),
                "Fetched Scrims",
            )

    async def test_returns_none_when_guild_cannot_be_fetched(self):
        with (
            patch.object(bot, "get_guild", return_value=None),
            patch.object(
                bot,
                "fetch_guild",
                new=AsyncMock(
                    side_effect=discord.NotFound(
                        SimpleNamespace(status=404, reason="Not Found"),
                        "not available",
                    )
                ),
            ),
        ):
            self.assertIsNone(await authorized_guild_name(789))

    async def test_auth_list_uses_subscription_embed_style(self):
        ctx = SimpleNamespace()
        with (
            patch(
                "main.repository.list_authorizations",
                return_value=[(123, None, 0)],
            ),
            patch(
                "main.authorized_guild_name",
                new=AsyncMock(return_value="A.R.C. Scrims"),
            ),
            patch(
                "main.repository.get_server_license_type",
                return_value="Gold",
            ),
            patch(
                "main.send_private_command_feedback",
                new=AsyncMock(),
            ) as send_feedback,
        ):
            await auth_list.callback(ctx)

        kwargs = send_feedback.await_args.kwargs
        self.assertEqual(
            kwargs["embed"].title,
            "💎 **A.R.C. Subscription Status**",
        )
        self.assertIn("**Plan:** Gold", kwargs["embed"].fields[0].value)
        self.assertIn("**Status:** 🟢 Active", kwargs["embed"].fields[0].value)
        self.assertIn(
            "**Time Remaining:** ♾️ Lifetime / Unlimited",
            kwargs["embed"].fields[0].value,
        )
        self.assertIsInstance(kwargs["view"], SubscriptionStatusView)


if __name__ == "__main__":
    unittest.main()