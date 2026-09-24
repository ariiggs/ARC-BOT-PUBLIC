---
name: GitHub source publication
description: Safe way to publish the current bot runtime when the local Git branch has diverged from GitHub.
---

Publish the runtime files from the live workspace through the connected GitHub API, using one commit based on the current remote `main`. Do not rely on a stale durable file snapshot or move large source files through truncated shell output.
Do not use `ShellExec` base64 stdout as transport for large binary files: it can return partial output while reporting no truncation. Verify each uploaded binary's Git blob SHA and byte size before updating branch refs.
The GitHub connector's Git Data API can expose a branch update even when the client-side sequence later reports a stale-ref error. A successful blob-create response may omit `size`; verify the blob SHA, then confirm SHA, mode, and byte size in the resulting recursive tree. After any apparent write failure, re-read the live ref and tree before retrying.
Commits created through the GitHub API are not automatically present in the workspace's local Git object database. Before moving local refs, reconstruct/import the exact commit object and verify its SHA; REST timestamps are normalized, while the stored commit header may use a non-UTC timezone. Raw-media Accept headers may still return JSON through the connector.

**Why:** A divergent local branch and a large-file transfer introduced Git conflict markers and a truncated Python file into the remote bot repository, causing the hosting process to fail before Discord login. A later local-ref update failed because the API-created commit object was missing locally; the exact commit hash required its stored timezone offset.
Large base64 output can also be silently truncated by the execution wrapper, leaving an invalid but syntactically present asset in the repository.
An API response error can arrive after a write has become visible, so retrying from an old branch snapshot can duplicate or conflict with a completed publication.

**How to apply:** Read current files directly from the live workspace, publish runtime files together in one GitHub tree/commit, check for conflict markers, run the local Python compile/tests, verify remote blob hashes and sizes (especially binaries), and re-read the live ref/tree after any API error before retrying. Recreate the remote commit object locally and confirm its exact SHA before updating local branch and tracking refs; never point refs at missing objects or invent a local upstream SHA. Then align the local branch without discarding unrelated local assets and tell the user to pull and restart the external bot host.