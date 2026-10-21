# The phone reads cards by the picture (standalone mode)

*Plan, 2026-10-21. Nothing of it is built yet. Asked for by the owner instead of the OCR port (`ANDROID_OCR_PLAN.md`): standalone mode with picture
detection only, tried on the Pixel; client mode is unchanged (the server reads).*

**The owner's answers (2026-10-21):** a switch for the vision AI in standalone mode - on, what the picture cannot settle goes to the AI as today;
off, it goes to the phone's review queue (no AI call at all); **Read by the picture first ON by default** (not off until measured: the Pixel
tests come before a release, and the guards keep it from adding what the index cannot vouch for); the index comes from the server; the OCR
plan is paused.

## What it is for

In standalone mode the Android app asks the vision AI for EVERY card (`CaptureProcessor.identifyOnce`: `identifier.identify(card)`, then
`scryfall.find`) - a key or a local model, a network, 1-2 s and a cost per card. The server reads by the picture first: on the rehearsal set it
settles most cards without OCR and without the AI. The pieces of that reading are already ported to Kotlin and pass the server's test files -
`reading/Fingerprint.kt`, `reading/Decide.kt` (rule A2, the finish, the guards) - but **nothing calls them**. This plan wires them into
standalone mode: the picture first, then the AI (or review) for what the picture cannot settle.

**The baseline to compare with** (the server, measured 2026-10-21 with `rehearse_matrix.py` on a copy of the real data, 39 pictures with known
answers - 15 Deadpool Pixel captures, 12 ordinary, 12 hard): OCR off + AI on: **39 added, 29 by the picture alone + 10 by the AI, 0 wrong**,
median 0.08 s; OCR off + AI off: 29 added, 10 to review, 0 wrong. The phone has no OCR, so it is the "OCR off" column: a phone that reads by the
picture first should settle about 29 of those 39 by the picture. Far fewer means the fingerprint or the coverage count is wrong - look there first.

## What exists already (checked 2026-10-21)

| Need | Where it is |
|---|---|
| The fingerprint | `reading/Fingerprint.kt` (`hash`, `distance`, `similarity`): the same as the Python `image_index.hash_card` on three pictures - `FingerprintParityTest`, **on an x86-64 emulator only, never on a phone** |
| The decision and its guards | `reading/Decide.kt`: `printing` (rule A2), `finish`, `acceptPrinting`, `confirmByOcr` - passing the server's four case files (`ReadingDecideParityTest`) |
| The index, for a phone | the server: `GET /api/image_index/download` (station token; descriptor, generation, count, built, and three files: `hashes`, `ids`, `printings` - a gzipped TSV of id, set, number, oracle id, finishes, name), each file resumable and checked by SHA-256; 409 when the index was rebuilt or the card data changed meanwhile (`stations_api.image_index_download`, `tests/test_index_download.py`). About 22 MB |
| The phone's card data | `scryfall/CardDatabase.kt`: Scryfall's `default-cards`, `art_series` skipped - **the same source and the same filter as the server's** (`Config.SCRYFALL_BULK_URL`, `database._fill_import`); ids are Scryfall ids, as the index's; `byId(id)` exists |
| The foil network | `reading/FoilNet.kt` (`probability(crop)`): the network only. **No fixed-place crop** (the server's `foil_marker.fixed_crop`), and the printings table has no `released_at` / `frame` (what the server's `prints_a_marker` needs) |

## Design

### 1. The index on the phone (`reading/ImageIndex.kt`, `reading/IndexDownload.kt`)

- Settings -> Standalone: **Picture index**, *Download from the server* (the client mode's address and token, which a person who has a server has
  already entered), its count, its `built` date and the card data date it was made for; *Delete*. A download that is interrupted resumes (Range);
  a 409 starts again from the listing. A `descriptor` other than `phash_rgb_v1` is refused (another fingerprint: every distance would be wrong).
- Kept like the server's: the three files in the app's own storage, written first, then a small manifest naming them replaced in one rename - a
  half-written download is never read.
- In memory: the hashes as one `ByteArray` (112,000+ printings x 96 bytes, about 11 MB) or `LongArray`; distances by XOR + `bitCount` over the
  whole index, both orientations - the server's `ImageIndex.distances`. **To be measured on the Pixel** (step 3): the server takes about 18 ms
  for both orientations on its CPU.

### 2. The gatherer (`reading/PictureReader.kt`) - the phone's `PrintingCheck.live_read` with no OCR reader

Of the flat card (`foilCard`, the picture the server fingerprints too):
1. `Fingerprint.hash` as given and turned 180 degrees; distances to the whole index each way; the better orientation (`orientation_established`'s
   rule is ported as a pure function: the better one at least `ORIENTATION_MIN_SIMILARITY`, ahead of the other by `ORIENTATION_MIN_MARGIN`).
2. The nearest candidates in it, with their facts from the printings table -> `Decide.printing` -> a `PrintingDecision` (with no strip: `path` is
   `image` or nothing - never `strong`).
3. `Decide.acceptPrinting(decision, Facts(acceptImageAlone = the setting, indexCovers = the coverage count is 0, inData = the phone's card data
   has the printing, stripNames = null, ocrOn = false))`. With `ocrOn = false`, `needsOcr` is always false: **`confirmByOcr` is never called on
   the phone** - the server's "picture only" configuration (OCR off), with the same trade-off, documented there.
4. Accepted: the card is `CardDatabase.byId(printingId)` - by its id, never looked up again by name / number / set (the server's 14% rule:
   promos, `CMR-77`, `245p` do not come back as themselves).

The reference for this code is the server's own: `PrintingCheck.live_read(rgb, None, None, None, True)` run on pictures with known answers, its
decision per picture written to a JSON file (`reading_reference.py`, as the other case files) that an instrumented test on the emulator passes
with the same index files. `Decide.kt` itself is not changed (its case files pin it).

### 3. The coverage guard (`indexCovers`)

The server adds nothing by the picture alone while its index lacks any printing of its card data (`PrintingCheck.missing_printings`). The phone
counts the same: printings of ITS card data whose id is not in the index's ids, once per index and card data download. Both come from the same
Scryfall file with the same filter, so the count is 0 only when **the phone's card data is not newer than the server's index**: download both on
the same day; after a Scryfall update, the server's index extends itself (since 2026-10-21) and the phone downloads it again. Settings shows the
count ("the index lacks N printings of the card data - the picture alone adds nothing") as the server's banner does.

### 4. The finish

First version: `Decide.finish` with no strip and no network reading - `unknown` - so a picture-decided card gets the finish as today: the AI's
foil check (`identifier.readFoilSymbol`) when the AI is on and *Foil check* is on, else `FinishPolicy.suggest` with an unknown marker. The
fixed-place foil network of the server needs `released_at` and `frame` per printing (a change to the server's printings table) and the fixed
crop ported - a later step, measured on its own.

### 5. The setting and the screen

Settings -> Standalone, two switches:
- **Read by the picture first** - **on by default** (the owner's choice). On, with an index that covers the card data, `identifyOnce` asks the
  picture first; without an index (or one that lacks printings) it is as today.
- **Ask the vision AI** - on by default (as today). On: what the picture does not settle goes to the AI exactly as now. Off: it goes to the
  phone's review queue with its photo and the picture's best guesses - no AI call at all, so standalone works with no key and no network for
  reading. Every place that asks the AI asks it through one check (as the server's `Identification._ai()`), the foil check included.
The row under a card says who read it, with the client mode's words (`ServerOutcome.readerLabel`): *picture*, or *AI (model)* - or the tests
cannot be read.

## Steps

0. **The fingerprint on the Pixel** (half a day): `FingerprintParityTest` on the user's phone is refused by its own rules (it wipes the app's
   data); instead a debug-only screen or a test APK that only hashes the parity pictures and the 41 Pixel captures (`~/.cache/mtg-image-match/truth/rescan_cards/`)
   and writes the hashes to a file, compared with the Python code's. A few differing bits are tolerated (the test's own rule); more and nothing
   below is worth building.
1. **The reference** (half a day): `live_read(rgb, None, None, None, True)` on the 41 Pixel captures and the 39 rehearsal pictures, against
   the server's index, written as a case file (decision, printing, lead, similarity, orientation).
2. **The index on the phone** (1 day): download (resumable, checked, generation), storage, loading, distances; JVM tests on a small synthetic
   index; the coverage count.
3. **The gatherer** (1 day): orientation, candidates, `Decide`, the facts; the instrumented test against step 1's file on the emulator; the time
   per card on the Pixel.
4. **Wiring** (half a day): the two settings, `identifyOnce` (picture first, then the AI or review), the add by id, the reader on the row, the
   finish as today.
5. **On the Pixel** (with the owner): the same 15 Deadpool cards and a stack of ordinary ones, standalone, picture first + AI; then picture first
   without the AI. Compare with the baseline above.
6. **Docs**: the Android README (the file map's "NOT USED BY THE APP" line, the setting), the server's FEATURES.md (what was tried on the phone and
   what never).

## Risks

- **The fingerprint differs on the phone's CPU** -> step 0 finds it before anything is built on it.
- **The phone's card data newer than the server's index** -> the picture adds nothing (the guard, as designed); Settings says why.
- **11 MB in memory and a full pass per card too slow** -> step 3 measures it; a pass is about 1 M popcounts per orientation.
- **Standalone without a server**: the index comes from the server (the only place it exists: building it needs every Scryfall picture, hours).
  A phone that has never seen a server cannot read by the picture.

## Not in this plan

- OCR on the phone (`ANDROID_OCR_PLAN.md`) - so no strip: no `strong` decision, no `picture+strip`, no `confirmByOcr`.
- The fixed-place foil network on the phone (needs the server's printings table extended).
- Card backs in standalone mode (`card_back.py` ported: a later, small step - the fingerprint is the same).
- Any change to the server's reading or to `Decide.kt`.

## The owner's answers (2026-10-21)

- What the picture cannot settle: a switch - the AI when it is on, review when it is off.
- Read by the picture first: on by default.
- The index comes from the server.
- `ANDROID_OCR_PLAN.md`: paused (a note at its top).
