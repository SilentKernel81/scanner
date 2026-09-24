# MTG Card Scanner - Technical Documentation

How the scanner works inside. For installing and using it see [README.md](README.md); for
deployment see [INSTALL.md](INSTALL.md).

## Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Card detection](#card-detection)
4. [Auto-capture](#auto-capture)
5. [Identification (vision AI)](#identification-vision-ai)
6. [Matching the printing](#matching-the-printing)
7. [Foil and finish](#foil-and-finish)
8. [Database](#database)
9. [Focus](#focus)
10. [Web interface](#web-interface)
11. [Configuration and files](#configuration-and-files)
12. [Performance](#performance)
13. [Known limitations](#known-limitations)

## Overview

The scanner turns a camera pointed into a box into a card-cataloguing station: cards are dropped
onto a pile, each new card is detected, captured once, identified down to the exact printing
and finish, and added to a local inventory.

Design choices:

- **Classic computer vision for detection.** A card is found by its outline, not by an object
  detection model: fast (milliseconds, fine on a Raspberry Pi), exact corners for a flat
  perspective-corrected crop, and unaffected by foil glare.
- **A vision AI only reads text.** It reads the name, collector number and set code from the
  crop, and the ★/• foil marker from a zoomed corner. Any provider works: Gemini, OpenAI,
  Anthropic, or a local Ollama model.
- **The local Scryfall database decides.** AI output is matched against 110,000+ printings;
  set code + collector number identify a printing exactly, and only confirmed matches are added
  without review.

## Architecture

### Modules

| Module | Responsibility |
|---|---|
| `app.py` | Flask + Socket.IO server: routes, events, capture orchestration, AI worker queue |
| `scanner.py` | Camera (USB via OpenCV/V4L2 or Pi camera), capture thread, detection state, stability, auto-capture |
| `object_detector.py` | Outline detection (`find_card_outline`), perspective warp (`warp_card`), optional YOLO fallback |
| `card_identifier.py` | Vision AI providers (`_ask`), card identification prompt and parsing, foil marker check, model warm-up |
| `database.py` | Scryfall download and import, schema/migrations, searches, printing lookup, match confidence |
| `card_search.py` | Search helpers combining name, number, set and treatment for the web app |
| `inventory.py` | Inventory table: add (merging duplicates), undo, edit/split, delete, stats, CSV import/export |
| `anti_glare.py` | Optional glare reduction applied to captures (CLAHE, bilateral filter, inpainting) |
| `settings.py` | UI preferences persisted in `data/settings.json` |
| `config.py`, `config_loader.py` | Settings from `config.yaml` (+ environment variables) |
| `cleanup.py` | Deletes old scanned images (on startup and as a CLI) |
| `setup_database.py` | Downloads and builds the card database |
| `templates/scanner.html`, `static/` | Single-page web interface |

### Threads

| Thread | What it does |
|---|---|
| Main | Flask + Socket.IO (threading mode) - HTTP routes, Socket.IO events, MJPEG stream |
| Capture (`scanner._capture_frames`) | Reads frames, detects the card, tracks stability, draws the overlay, triggers auto-captures |
| Auto-capture callback | One short-lived thread per auto-capture: crops, saves and (in review mode) identifies the card |
| AI worker (`ai_processing_worker`) | Identifies queued captures one by one when cards are added automatically |
| Background tasks | Card database update/rebuild, startup image cleanup, model warm-up |

Frames and detection state are shared under `scanner.frame_lock`; the card database and
inventory use a re-entrant lock each around a shared SQLite connection (WAL mode).

### Flow of a card

```
camera frame ──> outline detection ──> settled? ──> new card? ──> capture
                                                                     │
      flat, perspective-corrected card image <───────────────────────┘
                 │
                 ├──> vision AI: name, collector number, set code
                 └──> vision AI: foil marker (zoomed bottom-left corner)
                                    │
                    database: set + number (checked against the name)
                              → name + number → name → fuzzy name
                                    │
              confirmed printing ──> added to the inventory (with Undo)
              uncertain ──────────> shown for review, auto scanning paused
```

## Card detection

**Frames.** USB cameras deliver MJPEG; the scanner asks OpenCV for the raw JPEG
(`CAP_PROP_CONVERT_RGB = 0`) and `grab()`s every frame off the camera (so frames are never stale)
but decodes only `camera.fps` of them. For cameras of 1920 px and wider, live frames are decoded
at **half size** (2560 × 1440 → 1280 × 720): detection, stability, focus measurement and the
preview stream all work at that size, and a capture decodes the stored JPEG at full size and
scales the card's corners up (`get_detected_card`, `get_full_frame`). OpenCV runs with 2 threads
(`cv2.setNumThreads(2)`) - its default of one thread per core spent more CPU spin-waiting than
working. The preview stream encodes each new frame once, shared by all browser tabs
(`get_stream_jpeg`).

`object_detector.find_card_outline(frame)` runs on every frame (~3 ms):

1. Downscale to 640 px on the long side, grayscale, Gaussian blur.
2. Canny edges with thresholds derived from the median brightness, dilated to close gaps.
3. For each contour covering at least 2% of the frame, fit the minimum-area rectangle and keep
   it if:
   - its aspect ratio is within 18% of a card's (88 × 63 mm = 1.397),
   - the contour fills at least 85% of the rectangle (it really is rectangular),
   - it is portrait - unless `detection.allow_landscape` (a card's landscape art box has
     nearly the card's proportions),
   - it isn't the frame *inside* a card's dark border: the band just outside the rectangle
     must not be darker than the band just inside (this happens when the card's outer edge
     is cut off by the image border).
4. The largest remaining rectangle is the card; its four corners are returned.

`warp_card(frame, corners)` maps the corners to an upright rectangle with the card's aspect
ratio - the image sent to the AI is flat and tightly cropped, and the collector line is always in
the same place.

**YOLO fallback** (`detection.method: auto` or `yolo`, requires `requirements-yolo.txt`): the
pre-trained COCO YOLOv8n model has no card class, so it only gives a rough bounding box (it
labels cards "cell phone" or "book"); boxes are filtered by aspect ratio and smoothed. The model
(`yolov8n.pt`, AGPL-3.0) is downloaded into `data/` on first use. If
ultralytics isn't installed, the detector logs a warning and uses outlines only.

For display, the last detection is held for 6 s when the card is briefly lost ("HOLD"), but a
held detection never counts as a still card.

## Auto-capture

### When a card is ready

A frame counts toward `stable_frames` only if `_is_card_settled()` holds:

- the corners moved less than **1%** of the card size since the previous frame,
- sharpness (variance of the Laplacian on a 160 px wide crop) changed by less than **20%**
  (autofocus still adjusting changes it a lot),
- sharpness is at least `auto_capture.min_sharpness` (**250**) - a camera that hasn't focused
  yet is steady but blurry.

A capture needs `auto_capture.stability_frames` (5) such frames in a row, or
`fast_scan.stability_frames` (4, about 0.15 s) when cards are added automatically. The status
pill shows *Focusing*, *Stabilizing n/N*, *Ready*, or *Captured - drop the next card*.

### One capture per card

Cards are dropped onto a pile, so the view never becomes empty. After every capture (automatic
or manual) `awaiting_new_card` is set and a tiny normalized thumbnail of the card is stored.
Auto-capture re-arms (`_new_card_arrived`) when:

- the card jumps more than **3%** of its size, or its image (32 × 45 thumbnail) changes by more
  than **0.3** between frames - the drop itself, or a hand;
- after a gap in detection (a falling card usually can't be detected for a few frames), the card
  reappears more than **0.8%** away from where it was - a dropped card never lands exactly on the
  previous one, while a detector hiccup leaves it within ~0.3%;
- no card is seen for **6** frames or more;
- the settled card looks different from the captured one.

Identical copies are caught by the drop, not by their looks. Measured noise of a card lying
still: movement ≤ 0.4%, image change ≤ 0.07, detection gaps ≤ 2 frames. In a live test with
11 drops (including two identical copies) every card was captured exactly once; real drops
measured 2–5 frames without a card, jumps of 4.5–8.5% and image changes of 0.7–1.2.

Captures are also at least `auto_capture.delay` (1 s) apart.

### Adding automatically vs. reviewing

With **Add cards automatically** (default; internally `fast_scan_mode`, saved as `auto_add`):
the capture is queued, the AI worker identifies it in the background, and a confirmed printing
is added immediately (one Near Mint copy in the suggested finish). Anything uncertain sets
`card_under_review`, which pauses auto scanning until the card is added or skipped. The web page
shows each added card with an **Undo** button (`undo_last_add`).

With the switch off, each capture is identified synchronously and waits for **Add** / **Skip**;
a card dropped meanwhile is captured right after.

## Identification (vision AI)

`CardIdentifier.identify_card()` sends the flat card image (JPEG, max 2048 px) with a prompt
asking for three values from fixed places on the card:

```
NAME: <card name>            top of the card
NUMBER: <4-digit number>     bottom-left, line 1 ("U 0014")
SET: <set code>              bottom-left, line 2 ("HOB • EN")
```

The parser also accepts the three values without labels (some local models drop them).

Providers share one request function per API (`_ask_gemini`, `_ask_openai`, `_ask_anthropic`,
`_ask_local`). For Ollama, requests set `think: false` (thinking models otherwise spend the
whole token budget reasoning and return nothing), `temperature: 0`, and `keep_alive: 30m`; the
model is preloaded (`warm_up`) when auto scanning starts, because loading a 9B model takes about
10 s. Ollama error bodies (e.g. "model not found") are logged.

## Matching the printing

`CardDatabase.search_card_exact(name, number, set_code)` tries, in order:

| Step | Match | Tag |
|---|---|---|
| 1 | Set code + collector number (unique per printing), accepted if the name roughly matches (`names_match`: same, prefix, a double-faced card's face, ≥ 60% similar, or ≥ 75% similar to the short name before the comma - "Thands" / "Thanos, the Mad Titan") | `set_number` |
| 2 | Name + collector number, then shortened name ("Thanos" → "Thanos, the Mad Titan") + number | `name_number` |
| 3 | Name only (exact, flavor name, shortened), else fuzzy (`difflib`, cutoff 0.6, candidates sharing the first letters) - then the number to pick the printing (`name_number`), else the printing from the same set (if any) whose collector number is closest to the one read | `name_set` / `name` / `fuzzy` |
| 4 | Name unrecognizable but set + number exist: trust the printed set + number | `set_number_unverified` |

Collector numbers are compared in their variants ("0014" → 14, 0014, 14s, 0014s). Names are
compared through `name_search` / `flavor_search` columns - lowercase, accent-free copies
(`search_key`: "Fíli" → "fili", "Æther" → "aether").

Only `CONFIRMED_MATCHES` (`set_number`, `name_number`) are added automatically; the card panel
warns "Printing not confirmed" for the others.

**Manual search** (`CardSearcher.find_printings`): set + number go straight to the printing;
otherwise all printings of the name (optionally filtered by a treatment: regular, borderless,
showcase, extended art, full art, retro frame, etched, surge foil) are listed newest first and
shown as a picker when there's more than one.

## Foil and finish

Modern cards print a star instead of a dot between set code and language on foil copies
(`HOB★EN` vs `HOB•EN`). For captures found by outline detection - where the corner is known to
be in the image - `read_foil_symbol()` sends the bottom-left corner (lowest 9% × left 55% of
the flat card, enlarged 3×) and asks "star or dot?" (`vision_ai.detect_foil`).

The web page combines this with the printing's `finishes` in `suggestedFinish()`:

1. only printed in foil → Foil (Surge foil if the printing is a surge foil) - certain;
2. only printed non-foil → Regular - certain;
3. otherwise ★ → Foil / Surge foil, • → Regular;
4. unknown → Regular.

The suggested finish is pre-filled with quantity 1 and the reason is shown ("Foil: ★ next to
the set code"). Automatic adds use the same suggestion. Cards printed before the marker existed
(roughly 2020) rely on step 1–2 only.

## Database

One SQLite file, `data/cards_database.db`.

**`cards`** - Scryfall's "default cards" bulk data (gzipped JSON lines), tokens/emblems/art
cards excluded. Columns are defined once in `database.CARD_COLUMNS`:

| Group | Columns |
|---|---|
| Identity | `id` (Scryfall id), `name`, `flavor_name`, `set_code`, `set_name`, `collector_number`, `rarity`, `released_at` |
| Prices | `price_usd`, `price_usd_foil` |
| Card text | `type_line`, `mana_cost`, `oracle_text`, `colors` (JSON), `image_uri` (front face for double-faced cards) |
| Treatment | `border_color`, `frame`, `frame_effects` (JSON), `full_art`, `promo_types` (JSON), `finishes` (JSON) |
| Search | `name_search`, `flavor_search` |

Indexes: name, flavor name, search names, (set code, collector number), rarity, type.
Missing columns are added on startup (search names are filled in automatically; treatment data
arrives with the next card database update). "Rebuild database schema" copies the table into the
canonical column order by column name.

**`inventory`** - one row per card name + set + number + condition + foil + surge (`UNIQUE`);
adding an existing combination increases `quantity`. Also stores rarity, type, mana cost, colors,
color identity, price and timestamp. Editing the finish of part of a stack splits the row.
Exports: full CSV and Moxfield CSV; CSV import merges duplicates.

## Focus

USB cameras start in **continuous autofocus**, unless a focus position has been locked. Measured
on an Anker PowerConf C200 looking into the box, continuous autofocus settled at a position
about 4× less sharp than the best manual position, and re-hunts whenever the image changes -
so a card dropped at the wrong moment can end up blurry.

Because the camera-to-card distance is fixed, the scanner can **lock** the focus instead:

- **Refocus** (button, `reset_focus` → `CardScanner.refocus`): switches autofocus off and runs
  `focus_sweep()` - a coarse pass over the camera's `focus_absolute` range (step 50), then a fine
  pass (step 10) around the best position, scoring each position by the sharpness of the card
  (or the image centre when there's no card); finally a parabola through the best position and
  its neighbours predicts the peak between the fine steps, which is measured and kept if sharper.
  On the C200 the sweep found 444 against a measured peak of 446 (without the parabola: 440, 4%
  less sharp). A lens move takes ~0.4 s to show up in the frames
  (lens + camera buffer), so each position waits 0.45 s; if re-measuring the chosen position
  doesn't confirm it, the sweep repeats with 0.8 s. About 10 s in total. The position is saved
  (`focus_value` in `data/settings.json`) and restored on startup.
- **Automatic refocus** (`_check_focus_drift`): with a locked focus, a card that stays still but
  below `auto_capture.min_sharpness` for 3 s triggers a new sweep (at most every 15 s) - the pile
  grows toward the camera as cards are added.
- During a sweep the status shows *Focusing* and auto-capture pauses.
- **Settings → Camera autofocus** (`set_autofocus`) returns to continuous autofocus and forgets
  the locked position; switching it off runs a sweep.

Cameras without a `focus_absolute` control (and the Pi camera module) keep their own autofocus.

## Web interface

`templates/scanner.html` + `static/js/scanner.js` + `static/css/style.css` (dark/light theme via
CSS variables). Top bar with statistics; search bar and camera on the left, card panel on the
right, activity log below; settings in a slide-out drawer. The page polls
`/api/detection_status` every 500 ms for the status pill and talks to the server over Socket.IO.

Socket.IO events:

| Client → server | Server → client |
|---|---|
| `capture_card`, `search_card`, `select_printing`, `add_to_inventory`, `undo_last_add`, `dismiss_card` | `card_captured`, `card_found`, `card_printings`, `similar_cards`, `card_not_found`, `inventory_updated`, `inventory_undone`, `card_dismissed` |
| `toggle_auto_capture`, `toggle_fast_scan` (add automatically), `toggle_detection`, `toggle_anti_glare`, `toggle_debug_trace`, `reset_focus` (refocus + lock), `set_autofocus` | `auto_capture_triggered`, `processing_queue_update`, `*_toggled`, `focus_reset` |
| `set_ai_provider`, `update_database`, `rebuild_database` | `ai_provider_set`, `database_update_*`, `database_rebuild_*`, `log`, `error` |

HTTP endpoints are listed in the README.

## Configuration and files

| Where | What |
|---|---|
| `config.yaml` | Camera, detection, auto-capture, vision AI defaults, web server, cleanup |
| `.env` | API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`); `VISION_AI_PROVIDER` and `LOCAL_AI_ENDPOINT` override `config.yaml` |
| `data/settings.json` | Choices made in the UI: AI provider/model, add automatically, locked focus position |
| `data/cards_database.db` | Card data and inventory |
| `data/logs/` | `app.log`, `ai.log`, `scanner.log`, `database.log`, `scanned_cards.log` (one CSV line per identified card) |
| `scanned_cards/` | Captured images (deleted after `cleanup.days`) |

## Performance

Measured on an x86-64 laptop with an Anker PowerConf C200 at 2560 × 1440 and a local
`qwen3.5:9b` on Ollama over the network:

| Step | Time |
|---|---|
| Camera | 27–29 fps at 2560 × 1440 (MJPEG); 20 fps processed (`camera.fps`) |
| Per processed frame | ~10 ms (half-size decode, detection, stability) - was ~24 ms at full size |
| App CPU while scanning | ~20–25% of one core - was ~120% (full-size decode, 8 OpenCV threads) |
| Outline detection | ~3 ms per frame (YOLO on CPU: ~550 ms) |
| Card landed → capture | ~0.15–0.5 s (settling) |
| AI identification | 0.7–1.3 s; the foil check runs in parallel (+~0.3 s with Ollama); ~10 s once if the model has to load |
| Smaller images to the AI | tested and rejected: 1024 px was 30% faster but misread 2 of 16 cards |

Vision models compared on 90 scans (identification + foil check):

| Model | Hardware | Time per card | Notes |
|---|---|---|---|
| `qwen3.5:9b` (Q4_K_M, 6.6 GB) | Ollama server on the network | 1.6 s | reference; says "unknown" when the ★/• is unreadable |
| `qwen3.5:4b` (Q4_K_M, 3.4 GB) | laptop RTX 3050 6 GB (fits entirely) | 3.4 s | image processing ~920 vs ~2,560 tokens/s on the server; called 3 regular cards foil |

The 9B doesn't fit in a 6 GB GPU (it would be split with the CPU); the 4B is a usable fallback.
| Set + number lookup | 0.1 ms; fuzzy name search ~90 ms |

## Known limitations

- **White-bordered cards on a white background** have no visible outline; use
  `detection.method: auto` (YOLO fallback) or capture them manually with detection off.
- **An identical copy landing within ~0.7 mm of the previous card** without the fall hiding the
  card for 6 frames isn't recognized as new - press Capture.
- **Cards without the ★/• marker** (older printings) get their finish from printing data only.
- **Undo** takes back only the most recent add; older adds are edited in the inventory.
- **One camera, one instance**: the camera can only be opened by one process.
