---
name: Discord slot interaction safety
description: Concurrency and discord.py compatibility constraints for interactive slot decisions.
---

Treat Discord button callbacks and reaction-based selections as concurrent operations. Serialize slot transitions, capture immutable assignment data, and revalidate an assignment generation immediately before mutation.

**Why:** A manager or staff member can click an old button while a cancelled slot is being reassigned. Without generation checks and transition serialization, the stale action can modify the replacement team or produce an incorrect audit message.

**How to apply:** Any new command or interaction that changes a slot must participate in the same transition lock, validate the captured assignment generation, and use an immutable snapshot for delayed notifications. Serialize public-board edits so an older request cannot overwrite a newer state.

The installed discord.py version does not provide `View.disable_all_items()`.

**Why:** Calling that method raised `AttributeError` during interaction tests.

**How to apply:** Disable each child component explicitly before editing the interaction message.

Persistent dynamic buttons must enforce authorization and report errors in their
own dispatch path, not rely only on the original view subclass.

**Why:** discord.py reconstructs a generic view from the Discord message when
dispatching a `DynamicItem`. The original view's `interaction_check` and
`on_error` are not automatically used on that path, including after a restart.

**How to apply:** Explicitly invoke staff permission checks and error handling
from dynamic callbacks. Disable a dynamic button's wrapped item rather than
setting attributes on the wrapper. Test reconstructed callbacks as well as
direct view methods.

Scrim deletion must coordinate with asynchronous operations on its Discord
channels, not just mutations of its slot data. Use the board lock before the
state lock when both are needed.

**Why:** A deleted scrim's channels can immediately be reused by a new scrim.
A reset still purging messages or a publish still in flight could otherwise
affect the replacement scrim. Consistent lock ordering also avoids deadlocks.

**How to apply:** Serialize deletion against publishing and reset cleanup;
after awaited operations, revalidate the captured scrim identity before
persisting state. Private prompts must retain their original board identity
instead of silently following a replacement board.