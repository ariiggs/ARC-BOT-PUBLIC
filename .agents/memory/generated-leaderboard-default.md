---
name: Generated leaderboard default
description: Keep leaderboard rendering functional without storing a default background image.
---

When the bundled default leaderboard background is absent, generate a solid dark canvas in code. Do not re-add non-blueprint image files to the asset folders; custom uploaded backgrounds remain supported.

**Why:** The user chose to keep only the dimensioned blueprint images, while the old rendering and reset-preview paths still expected a stored default background.

**How to apply:** Preserve this fallback in both bot copies. If changing default leaderboard styling, keep background generation separate from the renderer's table and text drawing.