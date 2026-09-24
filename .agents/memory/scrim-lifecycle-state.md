---
name: Scrim lifecycle state
description: Durable rules for multi-scrim lifecycle, cancellation visibility, and channel-scoped archival.
---

Cancelled assignments are intentionally retained as durable slot records with a red status until staff reuse the slot; reuse increments the assignment generation.

**Why:** Managers need a visible cancelled/liberated state, while generation checks prevent an old Discord button or notification from changing a replacement assignment.

**How to apply:** Persist lifecycle fields with every scrim snapshot, keep public/staff/log/history channels isolated per scrim, and archive state after staff decisions or lifecycle changes.

When staff closes a scrim, remove the public board's manager controls/reactions in place and do not post a confirmation message in the public channel.

**Why:** Closing is a quiet control-state change; the existing board should remain visible without adding chat noise.

**How to apply:** Keep the close command's success path silent in the public channel while preserving the durable closed state and configured audit/archive behavior.

The public board does not need an OPEN/CLOSED status label; its manager buttons are the visible interaction signal.

**Why:** A separate status sentence adds noise and duplicates the actionable control state.

**How to apply:** Keep the internal open/closed state and click validation, but omit the status label from public board content.

Server-wide Head Staff, Staff, and logs settings are stored separately from individual scrim records; global logs take precedence for audit entries while old per-scrim log settings remain a compatibility fallback.

**Why:** Onboarding configuration applies across all current and future scrims, but existing snapshots must continue to load without losing their older channel assignments.

**How to apply:** Read the server configuration for authorization and audit routing first, and only fall back to scrim-level fields when no global configuration exists.

Discord `ChannelSelect.values` may contain an `AppCommandChannel` reference rather than a concrete `TextChannel`; resolve its ID through the guild cache or API before using channel methods.

**Why:** A valid existing channel selection was rejected by an `isinstance(TextChannel)` check even though Discord supplied the channel ID.

**How to apply:** Treat select values as references, resolve them to guild channel objects, then validate type and permissions.

Manager access is a temporary member-specific overwrite on each scrim's public slots channel, not a required Discord role.

**Why:** Managers need to use board buttons without being granted a broad server role, and access must disappear when their last active assignment ends.

**How to apply:** Grant view/history access when `manager_id` is assigned; after cancellation, staff release, or reset, remove the overwrite only when that manager has no other active slot in the same scrim.

Staff review prompts are one-shot messages: keep them compact and delete the prompt after a valid Confirm or Remove decision instead of editing it into a permanent result.

**Why:** Repeated manager actions otherwise fill the staff channel with stale review cards and disabled buttons.

**How to apply:** Put only the team name, its pending confirmation status, and the two decision buttons in the prompt; send any detailed result privately or to logs/history.

When staff resets a scrim, clear both the invoking staff channel and the manager-facing public channel, then refresh or recreate the durable public board even if staff-channel cleanup fails.

**Why:** Resetting only the command channel leaves managers with stale assignments, and a purge permission/API failure must not prevent the saved slot reset from becoming visible.

**How to apply:** Treat each channel purge as best effort, retain the public board reference until cleanup succeeds, and always run the public refresh after state persistence.

Audit coverage should include a persisted-state snapshot at bot startup plus
individual entries for every captain mutation and configuration change.

**Why:** Older snapshots do not contain a complete event timeline, so current
configuration and captain assignments must still become visible when logging
is enabled or the bot is updated.

**How to apply:** Route snapshots and new events through the configured global
logs channel first, fall back to the scrim channel when needed, and split long
audit details into Discord-sized messages.

Registration requests use a durable request record while Staff Validation is
active; the real slot stays Available until staff approval creates a Reserved
assignment. Auto-accept creates Reserved immediately.

**Why:** An unapproved registration must not occupy or populate a real slot,
and review buttons must survive restarts without losing the requested team.

**How to apply:** Exclude pending request slot numbers from registration
selection, validate the captured assignment generation on approval/rejection,
change the original 🆗 registration reaction to ✅ after approval, and treat
only literal `True` registration mode as Auto-accept.

Reset cleanup must preserve pinned messages and treat the registration channel
as optional; when it is not configured, reset only the staff and public
channels.

**Why:** Registration is an optional feature, and pinned operational guidance
must survive routine resets.

**How to apply:** Filter the configured channel IDs before resolving or
purging them, and pass a pinned-message check to the channel purge operation.

Public manager activation and registration-channel activation are independent
durable states; route `!open` and `!close` by the channel where the command is
used.

**Why:** Staff may need to accept registrations while keeping manager slot
buttons inactive, or manage the public board without reopening registrations.

**How to apply:** Public-channel commands toggle scrim manager controls; a
configured registration-channel command toggles that channel role's
`send_messages` permission and the separate registration state.