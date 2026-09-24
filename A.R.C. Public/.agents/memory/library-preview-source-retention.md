---
name: Library preview source retention
description: Replit asset-card previews may depend on the source file remaining at its registered workspace-relative path.
---

Keep files at the exact workspace-relative path recorded by `presentAsset` until preview access is confirmed. Do not delete temporary source files immediately after registering them; if a card reports that preview is unavailable, restore the source at that path or re-present it from a stable location.

**Why:** Replit's asset metadata records a `file://` source URI alongside the remote storage URI. Removing the original file left preview cards unavailable even though the remote asset record and content hash remained.

**How to apply:** For generated HTML and images, use a stable ignored review directory, validate each file against its registered content hash, and keep the sources in place. Do not copy review outputs into bot runtime assets unless the user approves.