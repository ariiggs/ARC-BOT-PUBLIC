---
name: Global staff authorization
description: The Staff role is a server-wide authorization setting, not a per-scrim configuration.
---

The Staff role is configured globally through `!set` and reused for all scrims. It must not appear as a required field inside `!setup` for an individual scrim.

**Why:** Staff permissions, including access to `!setup`, `!results`, and `!res`, should remain consistent across the server instead of changing with each scrim.

**How to apply:** Treat `!set` as the source of truth for Staff authorization. Scrim setup should configure scrim-specific channels and settings only; it may show whether global authorization is configured, but must not redefine it.