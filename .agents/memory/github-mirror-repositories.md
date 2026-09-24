---
name: GitHub mirror repositories
description: How to keep the private beta bot and public bot source copies in sync.
---

The GitHub targets are `ariiggs/arc-beta-bot` on `beta` and `ariiggs/ARC-BOT-PUBLIC` on `public`. The workspace root is the beta source, and `A.R.C. Public/` is a nested mirror directory in the same Replit checkout. When the user asks for exact branch copies, mirror the entire tracked beta tree—including workspace assets and archives—to `public`, and remove public-only paths. Keep the existing branch history by committing on the current public head; do not force-update refs.

**Why:** The user wants to select either branch when hosting the bot, so path, blob, and mode equality matters more than preserving public-only files. The branches have separate histories, and local tracking refs can lag or diverge from GitHub.

**How to apply:** After every bot-code modification, publish the matching root bot to `beta` and the `A.R.C. Public/` bot copy to `public`, even if the user does not repeat the reminder. Use the connected GitHub integration rather than the shell remote. Refresh both live refs, scan tracked files and archive contents for credentials, and verify every path, blob SHA, and mode. When exact branch copies are requested, copy the full beta tree and update `public` with a non-force commit. Never publish actual secrets, even when the requested mirror includes workspace files.