---
name: Cap Transfer authorization
description: Durable behavior for the optional channel and role gate on captain transfer commands.
---

Cap Transfer is an optional per-scrim channel. When configured, `!cap` commands are restricted to that channel and require the scrim's Pending or Confirmed Captain role; administrators and configured staff retain access.

**Why:** Captain transfer commands need a predictable private operating channel while still allowing the two captain lifecycle roles to manage their own assignments.

**How to apply:** Preserve the optional setup/disable path and enforce both channel and role authorization at command entry and again before applying a delayed slot-selection action.