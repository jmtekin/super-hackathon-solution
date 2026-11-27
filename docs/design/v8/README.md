# UI v8: one design system for Overview, Timeline, Settings (2026-11-27)

Jonas 16:51 "the site is bad", 16:53 "The UI in not goot". Earlier today: colours/contrast "how do pros do them", spacing unclear, first impression must say what it is, toy/childish look rejected.

## What made v7 look amateur
- No real typeface: `system-ui` fell back to DejaVu/Segoe, so the same page looked different everywhere and heavy everywhere.
- One flat tone: page, log and timeline were all #111-ish with no raised surface, so nothing grouped and nothing led.
- Three pages, two headers: the timeline had a bold two-line tagline instead of the nav the other pages had.
- Ad-hoc details: 3-4 px radii, square status marks next to round ones, 13/14/16/20 picked per element, empty `stderr:` lines in the log.

## Direction (references)
- Linear: layered graphite surfaces, Inter, quiet chrome, the selected nav item as a raised chip.
- Vercel dashboard: one 56 px top bar on every page, content in hairline panels with a faint top highlight.
- Grafana / Datadog: dense mono run log in its own panel, time axis lanes, colour only on state marks.

## System (ui/static/airlock.css)
Inter (self-hosted, inter.woff2) · type 12/13/14/16/20/24/28 · space 4-64 · radius 6 controls, 10 panels · neutrals bg #0d0e10 / surface #141518 / raised #1a1c20 · state: done green #3fb950, healed teal, not restorable amber, blocked/fail red #f0706a only on marks and the fail word · primary button = strongest neutral fill, never brighter than text.

## Changes
1. Shared top bar (mark + Overview / Timeline / Settings) on all three pages; on the timeline the Run controls sit in it, the tagline is gone.
2. Inter + one type scale; titles 24-28 px with tight tracking, labels 12 px muted.
3. Depth: the airlock, the run log, the ERP shot, the backup list and each settings section are raised panels; everything else sits on the page.
4. Timeline checks: 32 px rows with hover, round marks everywhere; run log drops empty `stderr:` lines.
5. Mobile: two-row bar on the timeline (pages + Reset/Run, then the three scenarios), settings stacked.

Shots: before/ and after/ (1440 + 390), before-after-*.png. Rendered with headless Chromium on the VPS (bin/browse.js).
