---
name: GitHub terminal authentication
description: The distinction between Replit's GitHub integration and shell Git authentication in this workspace.
---

Replit's GitHub integration authenticates API operations for the agent, but it does not automatically configure the shell's HTTPS Git remote. Shell `git fetch`, `pull`, and `push` therefore require a separate authenticated Git path.

**Why:** The GitHub remote uses HTTPS, and GitHub rejects password authentication. An unauthenticated shell fetch failed even though API branch operations through the connected integration worked.

**How to apply:** Prefer GitHub CLI authenticated from a Replit Secret and `gh auth setup-git`, or use SSH. Never place a token in a remote URL, source file, commit, or chat.