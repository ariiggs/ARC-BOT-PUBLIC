---
name: GitHub source publication
description: Safe way to publish the current bot runtime when the local Git branch has diverged from GitHub.
---

Publish the runtime files from the live workspace through the connected GitHub API, using one commit based on the current remote `main`. Do not rely on a stale durable file snapshot or move large source files through truncated shell output.

**Why:** A divergent local branch and a large-file transfer introduced Git conflict markers and a truncated Python file into the remote bot repository, causing the hosting process to fail before Discord login.

**How to apply:** Read current files directly from the live workspace, publish runtime files together in one GitHub tree/commit, check for conflict markers, run the local Python compile/tests, update the local remote-tracking ref to the published commit, align local `main` to it without discarding unrelated local assets, and tell the user to pull and restart the external bot host.