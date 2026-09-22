---
name: Match map rotation
description: Durable rules for configuring games and their map rotation.
---

Custom Maps is an independent unique pool. Each configured game has one selected map from that pool; assignments may repeat, and changing the match count trims or adds unassigned slots. If a change makes the current match number out of range, the next match starts at 1.

**Why:** Managers need to reuse a pool across any number of games while `!idpwgX` still needs a durable per-game map to publish.

**How to apply:** Validate the pool independently, validate each assignment against it, migrate legacy one-map-per-game snapshots into both fields, and preserve both in the existing scrim snapshot.

Discord map selectors support at most 25 options, so both the reusable pool and match count are capped at 25.