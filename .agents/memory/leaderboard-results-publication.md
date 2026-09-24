---
name: Leaderboard result publication
description: Approved separation between designer-provided leaderboard artwork and bot-generated content.
---

The bot uses pre-rendered leaderboard artwork for the dark table, column
headings, row dividers, ranks, and date badge. At runtime, draw only the date,
team names, wins, kills, placement points, and total points. Do not generate a
scrim title or footer. Preserve a custom background outside the fixed table and
date-badge overlays.

Leaderboard body rows are 48px high in both orientations. Horizontal mode
shows two teams side by side per row; vertical mode shows one team per row.
Use Montserrat Regular for generated values and fixed column headings/ranks.
The configured team count defines visible rank capacity even when fewer teams
are registered: retain every rank, including 24, while leaving missing teams'
names and scores blank. The configurable text color applies only to generated
values; the dark artwork, labels, and ranks stay fixed. Blueprints should show
the same dark static artwork with date, team names, and scores blank. Put
dimensions and annotations outside the exact-size design canvas.

Background images, their preview metadata, and generated text colors are
scoped independently by orientation and team count. A profile without saved
customization uses the default; restoring a background affects only the active
profile. Migrate legacy global settings to the profile selected during upgrade.

The header and footer bands are fixed at 180px and 120px for every orientation
and team count. The first data row starts at the same y-position in all layouts
(row top y=294; generated text center y=318), including both horizontal columns.
Normalize legacy custom heights to these fixed dimensions.

**Why:** The table is fixed artwork so runtime image generation cannot alter its
layout or text contrast; only result content changes between publications. The
configured display capacity may exceed registered slots, so static rank labels
must remain without inventing a team or changing the registration range. Empty
blueprints need to show the actual dark treatment and geometry without sample
result data. Orientation and team-count layouts are independent configurations,
so sharing one pair's settings with another would unexpectedly change its output.
Fixed framing prevents orientation changes or legacy per-scrim values from
shifting the first row and header/footer bands.

**How to apply:** Keep both bot copies, static template assets, and all
vertical/horizontal previews aligned. When changing leaderboard layouts, update
the templates as well as the empty blueprints; keep annotations outside the
native canvas and leave every dynamic content cell empty. Keep saved backgrounds,
preview links, and text-color overrides keyed by orientation and team count.
Do not reintroduce configurable per-scrim header/footer heights; use the fixed
bands for rendering, blueprints, and background validation.