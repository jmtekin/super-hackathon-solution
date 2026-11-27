# First impression: the Overview (2026-11-27)

Jonas 14:57: "The first impression needs to be better. More clear what it is and not mess" / "They will just look at it and decide".
Process: frontend-design + design-taste-frontend skills, v5 tokens kept (Radix slate neutrals, state hue only on marks).

Concepts (1440, real data of drill 14:41):
- A `concept-a-tracks.png`: one track per backup across the six stages; the poisoned one stops at the sandbox. Picked over B.
- B `concept-b-diagram.png`: stage boxes with counts and a quarantine branch. Rejected: counts need decoding, card-kit look, red border.
- Timeline (Jonas 15:07 "try a timeline format", 15:16 "Do the timeline design, much simpler, versions from left to right, on hover, details appear", "colors for if its working", "some way to show whats the latest workign"): built, replaces A.

Overview = title, one-line promise, verdict sentence, one rail of the watched backups (oldest left), a mark per backup coloured by whether it works (green restores natively, teal restores through a healed bridge, amber not restorable, red blocked), "Latest restorable" flag, details of the hovered/tapped mark below with its path through the six stages and the one action (Restore again). Evidence and Certificate open the Backups view one click deeper; the Drill tab is unchanged.

Three points (Jonas 15:21 "3 strong points that we really drive into the judges brains", "each must be confirmed 3 times"): Restored, not just backed up / Heals schema drift / Blocks poisoned backups. Each lands three times in one vocabulary (Restored, Healed, Blocked): the point with the watcher's live numbers above the rail, the mark's word on the rail, and the hovered backup's details (verdict, path labels, checks in the spec's three groups, every check visible); the Backups view and certificate use the same words.

Critic rounds:
1. Judge with 10 seconds (tracks build, 1440/390): stage colours not applied (CSS variable order), rail lines struck through labels, last label overflowed the grid. Fixed.
2. Click path (timeline): mark -> Evidence -> Backups view with the cell selected; latest restorable -> Certificate -> certificate; poisoned mark -> contained attacks + static scan one click deeper. Fixed: lines drawn past a stop, a "Queued" label on backups that were not queued, orphan word in the 390 title (text-wrap: balance).

Not verified: the live pipeline of a rerun on this layout (writes need the VM's sign-in; a backup the watcher fetched at 15:14 was seen moving through it live), Restore again's sign-in prompt in a real browser (headless Chrome waits on it).
