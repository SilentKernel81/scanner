# Plan: the Settings page redesigned (2026-10-22)

The page grew one feature at a time and each feature got a card where it was first needed: preferences,
one-off actions, status read-outs and admin controls look alike; things that belong together are apart (the
card data's update under *Collection and data*, the picture index's under *Card reading*); the hints speak the
program's own vocabulary; nothing shows which controls ask for the settings password; and which settings are one
camera's and which every camera's is unclear. The owner found it "very confusing".

Nothing on the server changes in what it does: the routes and events stay as they are (one is removed, below).
The work is in `templates/settings.html`, `static/js/settings.js`, `static/css/settings.css`, and the messages
and docs that name a place on the page.

## The sections, old -> new

| New (`#` in the address) | What is in it | Came from |
|---|---|---|
| **Overview** (`#overview`, the default) | One status line per area, each a link: cameras connected, card data (date, next automatic update), picture index (coverage), last backup, disk used, password | new - the status read-outs leave the hints |
| **Cameras** (`#cameras`) | *This camera* (under the chooser): name, location, actions, scanning, focus, debug trace. *All cameras*: sounds, capture size | Cameras, Sounds, Storage's capture size |
| **Card recognition** (`#reading`) | The three steps in their order - 1 Picture, 2 OCR, 3 Vision AI - each a switch and one line; "Add when only the picture decided" in the Picture step's details | Card reading (without the picture index) |
| **Card data** (`#carddata`) | The card database (status, update now), the picture index (status, update, stop), automatic updates (both schedules, "index follows the card data") | Collection and data's Card data, Card reading's picture index |
| **Backups** (`#backups`) | Schedule, back up now, upload, the list | Collection and data's Backups |
| **Storage** (`#storage`) | Disk used, how long received pictures are kept, clean up, compress, camera frames | Collection and data's Storage (without capture size) |
| **Server** (`#server`) | Restart / quit, network, settings password, debug mode | Server |

Old addresses keep working: `#sounds` -> Cameras, `#data` -> Backups (the collection page's old drawer was backups
and storage); a category remembered in the browser (`settingsCategory`) is mapped the same way. The Scanner
pages' stale-index banner links to `#carddata`.

## Rules for every control

- Keep every control's `id`: `settings.js` finds them by id. A script checks that each id it names is in the template.
- One plain line under a control; measured figures and mechanics in a *Details* fold.
- A lock badge on what asks for the settings password (`data-guarded`), shown only while a password is set.
- Preferences are switches and selects, actions are buttons, status is a grey panel.

## Removed

**Rebuild database schema** (button, `rebuild_database` event, `CardDatabase.rebuild_database_schema`): every card
data update already builds a new table in the canonical column order with fresh indexes (`populate_database` ->
`_create_cards_table` on `cards_import`, `replace_table(..., _create_card_indexes)`), more safely (a separate table,
swapped in only when the download is whole). The code reads columns by name, so their order changed nothing.

## Steps (one commit each)

**Status:** all five done on 2026-10-22 (b450a1c, 8dab2af, fe29197, 3922c63, and the docs). Not deployed.

1. Remove the rebuild.
2. The restructure: markup moved, new categories and their mapping, the strings that named the old places.
3. The overview and the lock badges.
4. The wording: one line per control, details folded.
5. The docs: every "Settings > ..." in code, docs and CLAUDE.md; a TEST_CASES row.

Tried in a server on copied data with headless Chromium (every category, with and without a password). That copy
has no camera station, so the Cameras controls cannot be tried there.
