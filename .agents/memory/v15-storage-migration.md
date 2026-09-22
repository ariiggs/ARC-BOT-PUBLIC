---
name: V1.5 storage migration
description: How V1.5 scrim configuration fields are persisted in this bot.
---

V1.5 configuration is migrated through the versioned JSON snapshot stored in
the SQLite `bot_state` table; this project does not have a per-scrim SQLite
table to alter. Older snapshots receive defaults during repository decoding
and are saved back at the latest snapshot version without dropping slots.

**Why:** The existing storage deliberately serializes the complete repository
as one atomic payload, so adding a duplicate `scrims` table would create two
competing sources of truth.

**How to apply:** Add future scrim fields to `Scrim.payload()`, default and
validate them in `_decode`, bump the snapshot version, and preserve existing
records through the repository transaction path.