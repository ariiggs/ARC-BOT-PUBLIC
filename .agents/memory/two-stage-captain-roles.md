---
name: Two-stage captain roles
description: Durable migration and side-effect rules for the Pending/Confirmed captain role split.
---

Legacy snapshots with the former per-scrim manager role must migrate that role to the Pending Captain role and leave the Confirmed Captain role unset. New registrations stay Reserved while receiving only the Pending role; captain confirmation moves them to Pending, and staff confirmation swaps to Confirmed.

**Why:** Existing scrims need to remain loadable without silently assigning an unchosen confirmed role, while the check-in flow must distinguish awaiting staff confirmation from confirmed participation.

**How to apply:** Keep the persisted schema at the two explicit role IDs, validate that both differ from Staff and each other, and update Discord roles only after the durable slot transition succeeds. Put reminder role mentions in message content, not only an embed description, so Discord can notify the role.