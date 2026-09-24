---
name: Guild whitelist bootstrap
description: Operational constraint for authorizing Discord guilds before normal bot access.
---

The guild whitelist starts empty by design. Only the bot owner can add or change access through `!auth` → a tier → Add; the modal collects the guild ID and duration. Bot admins can remove access from the selected tier. Gold enables horizontal leaderboards. Store the tier separately from `ServerConfig`; authorizing a guild must not create a role-less setup record. An unauthorized newly joined guild is notified and left immediately.

**Why:** Strict join-time rejection prevents an unauthorized server from using the bot, but it also means authorization cannot depend on running a normal server command after the bot joins.

**How to apply:** Keep tier selection ahead of guild-ID entry, accept non-negative day counts or Unlimited, show remaining time and expired entries in the selected tier panel, and keep tier state independent from staff-role setup. The owner can authorize the target guild before inviting the bot.