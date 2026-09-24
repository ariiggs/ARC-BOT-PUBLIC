---
name: Discord command privacy
description: Privacy rules for legacy prefix-command feedback in Discord.
---

Prefix commands cannot produce true ephemeral responses. Keep successful feedback short-lived in the invoking channel, delete the public invocation, and suppress command errors entirely; reserve DMs for the one-time guild welcome.

**Why:** Routine command DMs create unwanted private-message clutter, while public error messages expose staff-only validation failures.

**How to apply:** Use temporary channel responses for successful prefix commands, silently delete failed invocations, keep public boards and intentional staff notifications public, and send only `on_guild_join` onboarding to the owner by DM.