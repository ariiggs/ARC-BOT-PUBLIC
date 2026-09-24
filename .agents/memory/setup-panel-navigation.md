---
name: Setup panel navigation states
description: Durable navigation rules for the private Discord scrim setup interface.
---

The `!setup` interface uses three distinct states: a global dashboard, a scrim selector, and a selected-scrim management menu. The dashboard must contain only global Add Scrim and Manage Scrims actions; per-scrim controls belong behind selection.

**Why:** Combining the dropdown and per-scrim actions on the dashboard made the entry point crowded and caused the selected scrim context to be unclear.

**How to apply:** Keep selector callbacks acknowledged immediately before rebuilding or replacing views, and preserve detailed configuration plus operational tools behind Edit Configuration so the dashboard remains a stable overview.

The Add Scrim wizard and Edit Configuration screen share one strict four-row configuration grid. Custom maps are optional; an empty map list is persisted as an unconfigured rotation, and changing match count clears it before showing the user how to configure new maps.

**Why:** Match count and map rotation are independent settings, while separate one-input modals make each configuration change predictable on Discord mobile.

**How to apply:** Keep the wizard checklist and edit values in sync with the same grid state, persist edit changes immediately, and surface match-count map clearing as an ephemeral warning.