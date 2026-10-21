# A card back stops the auto-capture

*Plan, 2026-10-21. Revised the same day after a Codex review: the check sits before the upload route branches, not in `submit_capture`.*

**Built 2026-10-21 (steps 2-6), step 1 half done, step 7 not.** The owner's answers: stop auto-capture (not only the adds), on by default, "Teach this back" later.
What differs from the design below, and why: the setting is kept like `auto_add` (`scanner.settings`), not in `CAMERA_SETTINGS` - the station never needs it; the
back's picture is KEPT in `scanned_cards/` (the measurement of step 1 needs real backs; the clean-up removes it with the others); who is answered `card_back` is told
by the upload (`camera=1`: a computer station; `accepts=card_back`: the phone app from now on), not by the station's id; and `resume_pending` does NOT check - every
capture on record there passed the check at its upload (see PROGRAM_DOCUMENTATION.md, "A card back stops auto-capture"). Step 1: the fronts are measured (16,881
captures, at most 0.219), the backs are not - `CARD_BACK_MIN_SIMILARITY` is 0.35 until they are.

## What it is for

A face-down card dropped on the pile becomes a **stop sign**: when a camera captures the back of a
Magic card, the server switches that camera's auto-capture off, says so on the camera's page (with a
sound), and throws the capture away - it is not a card to add or to review. The person turns
auto-capture on again when the next pile is ready, as they do today.

Typical use: the last card of a stack is put face down; the camera stops by itself after the stack
instead of capturing whatever is dropped next.

## What exists already

| Need | Where it is today |
|---|---|
| Turning a station's auto-capture off from the server | `scanner.auto_capture_enabled = False` inside `with in_desk(...)`, then `emit_desk('auto_capture_toggled', {'enabled': False})` - exactly what `settings_events.handle_switch_game` does for every camera when the game changes. `auto_capture_enabled` is one of `RemoteScanner.SENT`, so the station follows. |
| Where captures come in | **Not one place** (a Codex review of this plan, 2026-10-21): `server/stations_api.py: station_capture` takes every upload and then BRANCHES - mode `review` (`camera=1&mode=review`: auto-capture that waits for Add / Skip, and the page's manual Capture button - the station sends both as `review`, `station_client.py: auto_captured` / `manual_capture`) is read at once with `identification.identify` + `announce_and_route` and never reaches `submit_capture`; every other upload (`auto`, the phone in client mode) goes through `server/captures.py: submit_capture`. Captures waiting when the server stopped come back through `captures.resume_pending` -> `submit_capture`. |
| A fingerprint of a picture and its comparison | `core/image_index.py: hash_card`, `similarity` - the same 96-byte fingerprint the picture index uses. |
| A capture settled without a card | `pending.py: PendingCaptures.settle` with an outcome dict; the station and the phone read the outcome of their upload. |

## Measured before planning (2026-10-21)

Scryfall's image of the standard Magic card back (`backs.scryfall.io/large/0/a/0aeebaf5-....jpg`) against the
picture index of the development machine (112,765 printings):

| | Similarity |
|---|---|
| The back against the nearest card FRONT | **0.229** (then 0.224, 0.214) |
| The back turned 180 degrees against the nearest front | 0.208 |
| The back against a blurred, darker, slightly cropped copy of itself | **0.667** |
| For comparison: a real capture against its own printing | about 0.68 (median) |

So "a card back" and "some card" are far apart: a first threshold of **0.45** leaves a margin on both
sides. **Not measured: a real camera photo of a back** - glare, a sleeve, a worn back. Step 1 of the work
measures it before anything acts on it.

## Design

### Detection (`core/card_back.py`, new)

- `CARD_BACK_FINGERPRINT`: the 96-byte fingerprint of Scryfall's back image, **computed once and written
  into the code as hex** (with the image's address in a comment and a script that recomputes it) - no
  download at run time, no file to ship.
- `card_back_similarity(rgb) -> float`: the best of the picture as given and turned 180 degrees (a back is
  not symmetric; a card can lie either way up).
- `is_card_back(rgb) -> bool`: at least `CARD_BACK_MIN_SIMILARITY` (0.45 to begin with, set from the
  measurement in step 1).
- A pure module: no database, no index, no OCR - about 5 ms a picture. It reads the **flat card**
  (`foil_image`, the perspective-corrected one, when the station sends it), like the picture index.

### Where it acts: one check, before the upload route branches

One function, `server/captures.py: stop_at_card_back(image_path, image, foil_image, station, key)`, returning the outcome `{'status': 'card_back',
'similarity': ...}` when it acted and None otherwise, called in **two** places so that no path misses it:

- **`stations_api.station_capture`, after the upload is saved and its repeat check (`seen_captures` / `settled_captures`) and BEFORE the branch
  into mode `review` / `submit_capture`.** Both branches are covered: an `auto` upload and a `review` one (auto-capture waiting for Add / Skip,
  and a manual capture - the server cannot tell those two apart, and a manual capture of a back stopping auto-capture is harmless). The
  outcome is remembered under the upload's key (`remember_outcome`), so a repeated upload of the same capture gets the same answer and is
  not checked or acted on twice. For a `review` upload the station holds its auto-capture until Add / Skip (`scanner.card_under_review`):
  it is released (`card_under_review = False`) when the capture is dropped, or the station waits for an Add / Skip that never comes.
  For an `auto` upload nothing goes on record in `pending` at all (the capture is settled before it would be).
- **`captures.resume_pending`**, for each capture waiting since before a restart, before it is submitted again: such a capture is on record
  (`pending`), so a back there is settled on record (`pending.settle` with the outcome) instead of being queued.

What it does when the camera's setting is on and `is_card_back(flat card)`:

1. the capture is **settled at once** with the outcome `{'status': 'card_back'}` - not added, not sent to
   review, not read by OCR or the AI; its picture is deleted like a skipped capture (on the `review` path the page has already been sent
   `capture_received` with the picture; the notice below replaces it);
2. that station's auto-capture is switched off - the same three lines as the game switch, inside
   `with in_desk(desk_for(station))`;
3. the camera's page hears it: `emit_desk('card_back_seen', ...)` - a line in the activity log ("Card back
   seen: auto-capture stopped"), a notice, and the review sound (`Sounds.review`) as the audible signal;
4. `data/logs/scanned_cards.log` gets a line with the reader `card-back` and the similarity, and the reading log (`data/logs/reading.jsonl`)
   a record with `result.outcome: card_back`, so it can be counted afterwards.

Every other capture goes on exactly as today; a failure in the check is "not a back" (it never loses a
card). The check is never inside `submit_capture` or `Identification`: either would leave a path out (the `review` branch never reaches
`submit_capture`), and `Identification` must not change a camera's settings.

### The setting

Per camera, under **Settings > Cameras > Scanning**: *Stop auto-capture at a card back* - **on by
default**. A new key in `RemoteScanner.CAMERA_SETTINGS` (`stop_at_card_back`), so it is kept per station
like the others (`StationSettings`).

### The phone in client mode

The phone runs its own auto-capture. Its upload's answer (`ServerOutcome.parse`) today maps an unknown
status to an **error** - so the Android app must learn the new status before the server sends it to a
phone:

- `ServerOutcome.Status.CARD_BACK`, its row text ("Card back: auto-capture stopped"), and the app switching
  its own auto-capture off when it receives it (`ScannerViewModel`);
- `ServerOutcomeTest` with the new answer.

Until that app is installed, the server sends `card_back` only to stations that are not the phone (the
station's id tells), or the phone's capture of a back would show as an error.

### Capture synchronization

A back is settled without a copy anywhere: the phone's "where is this capture's copy" route
(`station_capture_sync`) must answer for it as for a capture that cannot be followed (409) - never
`resolved`. A test pins it.

## Not covered (said in the docs when built)

- **Sleeved cards**: a face-down sleeved card shows the **sleeve's** back, not the card's. Not
  recognised - a later step could add "Teach this back" on the camera's page: the last capture becomes a
  second fingerprint for that camera.
- **Other backs**: some products have a back of their own (special editions, art cards). They are read
  as cards (and land in review, as now) - the safe failure.
- **A double-faced card's back** is a card face, not the Magic back: read as a card, as now.
- **The standalone phone app** reads cards itself; the same check could run there (the fingerprint code is
  ported already, `Fingerprint.kt`) - a separate, later step.

## Work, in order

1. **Measure a real back.** Drop five to ten face-down cards under a camera with auto-capture on, keep the
   captures (they go to review today), and compute their similarity to `CARD_BACK_FINGERPRINT`; also
   check that none of the 1,628 evaluation fronts reaches the threshold. Set
   `CARD_BACK_MIN_SIMILARITY` from that, between the two.
2. `core/card_back.py` + its tests (synthetic pictures: the back itself, turned, degraded; a front; a blank).
3. `stop_at_card_back` and its two calls (`station_capture` before the branch, `resume_pending`), the outcome, stopping the camera,
   releasing a `review` capture's hold, the page's event and sound, the log lines; the setting in `CAMERA_SETTINGS` and on the Settings
   page; `tests/test_app_surface.py --record` for the new event. Tests, through Flask's test client as `tests/test_capture_sync.py` does,
   with `identification` a stand-in that fails the test if it is called: a back uploaded with mode `auto`, one with mode `review`, and one
   resumed from `pending` - each stops the camera, is answered `card_back`, and is never read; a repeated upload of the same back gets the same
   answer and stops nothing twice; a `review` back releases `card_under_review`; with the setting off, or a front, every path is as today.
4. The sync route's answer for a back, with its test.
5. The Android app: the new status, its row, stopping its auto-capture; `ServerOutcomeTest`. Built, and
   installed on the phone when the person asks.
6. Docs: `PROGRAM_DOCUMENTATION.md` (the capture path, the new event, the setting), `FEATURES.md` (with what
   was never tried), `CLAUDE.md` (where it lives), the Android README.
7. Rehearsal on a copy of the data (a back among the 39 pictures: it stops, nothing else changes), then the
   deploy; the first live test is one face-down card.

## Open questions for the owner

- Stop **auto-capture** (the camera stops capturing), as planned - or only stop **adding automatically**
  (captures go on, into review)?
- Should the setting be **on by default** for every camera, as planned?
- Is a "Teach this back" step for sleeves wanted now, or later?
