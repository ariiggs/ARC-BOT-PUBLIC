---
name: Setup panel navigation states
description: Durable navigation rules for the private Discord scrim setup interface.
---

The `!setup` interface uses three distinct states: a global dashboard, a scrim selector, and a selected-scrim management menu. The dashboard must contain only global Add Scrim and Manage Scrims actions; per-scrim configuration controls belong behind selection.

**Why:** Combining the dropdown and per-scrim actions on the dashboard made the entry point crowded and caused the selected scrim context to be unclear.

**How to apply:** Keep selector callbacks acknowledged immediately before rebuilding or replacing views, and keep the selected-scrim panel focused on configuration. Do not show open/closed registration state, slot confirmation state, or other live operational state in setup panels.

The selected-scrim management menu has three primary actions: Edit Required Settings, Edit Additional Settings, and Delete Scrim. Open/close, reset, and other runtime controls belong in future `!` commands rather than setup buttons.

**Why:** Registration availability and slot confirmation are independent per-scrim runtime states; showing them beside static configuration creates misleading or ambiguous setup information.

**How to apply:** Use the management summary for required values (name, channels, roles, slot range) and optional values (Matches & Maps, Registrations configuration, ID&PW). Delete must require confirmation and must not delete Discord channels.

The Add Scrim wizard and Edit Configuration screen share one strict four-row configuration grid. Custom maps are optional; an empty map list is persisted as an unconfigured rotation, and changing match count clears it before showing the user how to configure new maps.

**Why:** Match count and map rotation are independent settings, while separate one-input modals make each configuration change predictable on Discord mobile.

**How to apply:** Keep the wizard checklist and edit values in sync with the same grid state, persist edit changes immediately, and surface match-count map clearing as an ephemeral warning.

The public registration configuration is exposed as one `Registration` button in the configuration grid. Its submenu shows Channel, Role, and Mode selectors together plus Back.

**Why:** Showing the three related selectors together matches the captain-role flow and lets staff configure registration without opening nested button menus.

**How to apply:** Preserve the submenu as the entry point in both the setup wizard and edit configuration views; keep each selector wired to the existing channel, role, and mode handlers.

When a registration setting is saved through the edit flow, include the channel, role, and auto-accept fields in both the prior-value snapshot and the update value set used for the success/audit message.

**Why:** The edit can persist successfully before formatting the change summary; omitting one of these fields turns a successful save into a generic interaction error.

**How to apply:** Treat the registration fields as one atomic group whenever the edit flow builds change summaries or passes values to storage.

Post-save dashboard refreshes and edit-view rebuilds are ancillary to persistence and must not be allowed to turn a successful save into a generic interaction failure.

**Why:** Discord users only see the final interaction response; an exception in refresh or view construction can falsely imply that the stored registration change was rejected.

**How to apply:** Log ancillary failures, report that the configuration is active, and provide a reopen/retry path instead of routing the interaction through the generic pre-save error handler.

Version 18 snapshots can contain an explicit `null` registration mode, so migration must replace non-boolean values rather than relying on `setdefault`.

**Why:** Existing snapshots with `registration_auto_accept: null` caused selecting a registration channel to fail validation before the channel could be saved.

**How to apply:** Normalize legacy and already-loaded null values to staff validation (`False`) before registration edits reach storage.

Registration selector saves must rebuild the Registration submenu, not the parent Edit Configuration view; its role picker must include `@everyone` explicitly.

**Why:** Staff configure channel, role, and handling mode as one flow, and Discord's native role selector does not expose the server-wide `@everyone` role.

**How to apply:** Keep the submenu active after each successful registration save and use a bounded role-options select containing `@everyone` plus selectable un-managed roles.