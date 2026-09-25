---
name: Leaderboard result publication
description: Runtime text-only leaderboard rendering and the separate dimensioned blueprint download.
---

The runtime renderer fits the selected background to the configured canvas and
adds text only: date, sequential ranks, team names, wins, kills, placement
points, and total points. On the generated default background, also show the
scrim name as a title in the header. Do not composite table or date-badge
artwork, draw row dividers, or add a footer; leave custom backgrounds free of an
injected scrim title. Render every configured rank even when there is no
corresponding team row; leave that team's name and scores blank. In horizontal
layouts, continue rank numbering across the second column.

Leaderboard rows use an 80px pitch horizontally and a 60px pitch vertically;
horizontal mode places consecutive ranks in two columns and vertical mode uses
one column. Reserve 440px outside the row area for framing, including 180px
header and 120px footer bands, with 10px section gaps. The first data row starts
at y=282; its text center is y=322 horizontally and y=312 vertically. Use
Montserrat Regular for generated text. The configured text color applies to all
generated values and ranks; no background color or artwork is added to improve
contrast.

Background images, their preview metadata, and generated text colors are
scoped independently by orientation and team count. A profile without saved
customization uses the default; restoring a background affects only the active
profile. Migrate legacy global settings to the profile selected during upgrade.

The header and footer bands are fixed at 180px and 120px for every orientation
and team count. The first data row starts at the same y-position in all layouts,
while row pitch and text center vary by orientation. Normalize legacy custom
heights to these fixed dimensions.

Dimensioned references exist for every supported orientation/team-count profile.
Offer the active profile's native-size reference from both the blueprint panel
and the wrong-dimension recovery flow, alongside its exact-size blank canvas.
Clearly label the reference as informational, not an upload background; do not
use it in result rendering.

**Why:** Static table artwork previously added non-text elements over selected
backgrounds, contrary to the text-only publication requirement. The generated
default is a plain dark canvas, so the user expects the scrim name to identify
the results when that default is active; custom backgrounds remain untouched.
The rank capacity can exceed registered teams, so ranks must remain visible
without inventing data. Dimensioned references contain measurements and sample
content, so keep them separate from upload-ready backgrounds and results. Their
native dimensions must track the same orientation/team-count layout rules as
blank canvases. Orientation and team-count layouts are independent
configurations, and fixed framing prevents orientation changes or legacy
per-scrim values from shifting the first row and header/footer bands.

**How to apply:** Keep both bot copies aligned. On the generated default
background, center the scrim name in the fixed header and reserve room for the
date; on custom backgrounds, do not inject the name. Preserve the selected
background and avoid table/date artwork, row dividers, or decorative shapes.
Keep each blank profile canvas and its matching dimensioned reference as
separate downloads. Keep backgrounds, preview links, text colors, and
dimensioned references keyed by orientation and team count. Use the fixed 180px
header and 120px footer bands and the orientation-specific row pitches; do not
reintroduce per-scrim height settings.