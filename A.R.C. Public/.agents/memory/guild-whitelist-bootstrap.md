---
name: Guild whitelist bootstrap
description: Operational constraint for authorizing Discord guilds before normal bot access.
---

The guild whitelist starts empty by design. The bot owner or a persisted bot admin must authorize a guild ID before normal commands are available; `!auth add` always takes a numeric duration, where `0` means unlimited and positive values expire after that many days. An unauthorized newly joined guild is notified and left immediately.

**Why:** Strict join-time rejection prevents an unauthorized server from using the bot, but it also means authorization cannot depend on running a normal server command after the bot joins.

**How to apply:** The bot owner manages persisted bot admins with `!admin add/remove/list`; the owner and those admins can use `!auth add <Guild_ID> <Days>` from an owner-accessible context such as an existing authorized server or DM, using `0` for unlimited access, then invite the bot to the target guild.