---
name: GitHub source publication
description: Safe way to publish the current bot runtime when the local Git branch has diverged from GitHub.
---

Publish the runtime files from the live workspace through the connected GitHub API, using one commit based on the current remote `main`. Do not rely on a stale durable file snapshot or move large source files through truncated shell output.
Do not use `ShellExec` base64 stdout as transport for large binary files: it can return partial output while reporting no truncation. Verify each uploaded binary's Git blob SHA and byte size before updating branch refs.

**Why:** A divergent local branch and a large-file transfer introduced Git conflict markers and a truncated Python file into the remote bot repository, causing the hosting process to fail before Discord login.
Large base64 output can also be silently truncated by the execution wrapper, leaving an invalid but syntactically present asset in the repository.

**How to apply:** Read current files directly from the live workspace, publish runtime files together in one GitHub tree/commit, check for conflict markers, run the local Python compile/tests, verify remote blob hashes and sizes (especially binaries), update the local remote-tracking ref to the published commit, align local `main` to it without discarding unrelated local assets, and tell the user to pull and restart the external bot host.