---
name: Captain and co-captain slots
description: Durable rules for the two-captain relationship stored on each team slot.
---

Each occupied slot stores a primary captain and at most one co-captain. Existing `manager_id` data migrates to `captain_1_id`, while the configured per-scrim Manager role remains the Captain role.

**Why:** The bot already models team ownership through `manager_id` and a per-scrim Manager role; preserving those fields avoids breaking existing boards while adding the strict two-person relationship.

**How to apply:** Keep `manager_id` synchronized with `captain_1_id`, persist `captain_2_id` separately, reject a third captain, and let the co-captain take over by removing the primary captain.

Interactive captain commands use a composite scrim-and-slot selection key and
revalidate the captured assignment before mutating it.

**Why:** Slot numbers repeat across scrims, and a dropdown can outlive a slot
assignment change.

**How to apply:** Never use the numeric slot number alone for delayed captain
actions; map the selected composite key back to the current assignment and
verify its assignment generation before applying the command.