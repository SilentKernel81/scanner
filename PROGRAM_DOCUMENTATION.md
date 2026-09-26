# Card Scanner - Technical Documentation

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
| `card_identifier.py` | Vision AI providers (`_ask`), response parsing, foil marker check, model warm-up |
| `prompts.py` | Built-in prompts and the ones edited in Settings (`data/prompts.json`), per model |
| `database.py` | Scryfall download and import, schema/migrations, searches, printing lookup, match confidence |
| `games/` | Card games: `base.Game` (the interface the app uses), `mtg.Magic` (Scryfall data, matching, finishes, exports), `pokemon.Pokemon` (TCGdex data, matching, prices, finishes, export); `games.active()` is the game being scanned |
| `card_search.py` | Magic search helpers combining name, number, set and treatment |
| `inventory.py` | Inventory table for every game: schema + migration, add (merging duplicates), undo, edit/split, delete, stats, CSV import/export |
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
| Background tasks | Card database update/rebuild, startup image cleanup, model warm-up, card data update check (10 s after startup, then daily) |

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

**Rotation** (`camera.rotate` or **Settings → Camera rotation**, saved as `camera_rotation`;
`set_camera_rotation` / `camera_rotation_updated`): every frame is rotated right after it is
decoded - live and the full-size capture (`CardScanner._rotate`) - so detection, crops, the
fixed area and focus all see an upright card. The card stands along the frame's short side
(1440 px) otherwise: measured over a 67-card lot, the card grew 16% (1038 → 1204 px) as the
pile rose ~20 mm, i.e. the camera sat ~14.5 cm above the box floor, and the card would reach
the frame's edge after ~115 cards. With the camera mounted sideways and the image rotated 90°,
the card's long side runs along 2560 px: at the same distance the pile can grow ~270 cards, or
the camera can come closer (~10 cm) for ~45% more detail per card and still ~125 cards.
Changing the rotation turns the fixed area off (it was drawn for the other orientation).

`object_detector.find_card_outline(frame, previous=...)` runs on every frame (~3 ms):

0. **Follow the previous card** (`_track_outline`, when the scanner passes the last outline -
   across detector gaps of up to 2 frames; after a new card is detected it starts afresh, since
   following the old outline once latched onto a new foil's inner frame, and the photo lost its
   set line): fit a line to the edges along each side of the
   previous outline (within 6 px at 640 px, corner zones left out) and intersect them. The
   result is kept if it is card-shaped, within 15% of the previous size, has edges along ≥ 80%
   of its perimeter and moved ≤ 2% (more means it was fitted to another edge, e.g. the inner
   frame of a blurry card - taking it made the outline flip); otherwise it is only a last
   resort after steps 1-5. On a sleeved pile the top card's outline often merges with the
   edge of a card underneath or with the box's corner crease once the pile is high - then no
   closed contour exists (in recorded pile frames the detector found nothing in most frames
   of a still card), or the largest outline flips between the top card and the whole pile.
   Both made cards wait 8-19 s and caused duplicate captures (the flip looks like a drop).
   Replaying 5 recorded pile moments through `CardScanner`: a card the current detector never
   captured in its 4 s recording was captured 1.3 s after landing, and a duplicate case gave
   one capture.

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
5. **Broken outline fallback** (only when step 3 finds nothing): where a card's edge is as
   bright as the background - a borderless foil's silver frame against the white box - the
   outline has a gap and no closed contour exists (a Gwen Stacy borderless: the right edge
   along the text box, ~100 px, had no edge at all). Edge pieces within ~15 px of each other
   are grouped; a group's hull counts as the card if its rectangle passes the same ratio,
   portrait and inner-frame checks, the hull fills ≥ 90% of it, and edges run along ≥ 80% of
   its perimeter. On 100 recorded frames it found only that card (no false detection on empty
   boxes, piles or screenshots); it costs ~5 ms more on frames where it runs.
6. **Rectangle check** for the outlines of steps 0 and 5, which are built from edge pieces or
   fitted lines: opposite sides within 8% of each other and corners within 8° of square (the
   camera looks straight down). A holo Pokémon card (N's Zoroark ex) once gave a skewed outline
   whose "top edge" was a streak of the holo art running from the name to the top-right corner,
   and the photo lost the card name. Rejected outlines leave the photo to the other steps or,
   in fixed area mode, to the area itself. On 1,825 recorded frames the check changed nothing.

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

- the corners moved less than **1%** of the card size since the previous frame **and** since
  the still streak began - a sleeved card sliding slowly (a few px per frame) passes the
  frame-to-frame test on every frame and used to be captured mid-slide: blurred, then captured
  again when it stopped (seen live with a Lake-town),
- sharpness (variance of the Laplacian on a 160 px wide crop) changed by less than **20%**
  (autofocus still adjusting changes it a lot),
- sharpness is at least `auto_capture.min_sharpness` (**250**) - a camera that hasn't focused
  yet is steady but blurry.

A capture needs `auto_capture.stability_frames` (5) such frames in a row, or
`fast_scan.stability_frames` (6, about 0.3 s) when cards are added automatically. Simulated
sleeve slides (0.8 s after landing): with 4 frames and only the frame-to-frame test, 12 of 12
cards were captured mid-slide; with the drift test and 6 frames none (slides of 2-3 px/frame),
captured ~0.5 s after the card stopped. 4 frames with the drift test still let a 2 px/frame
slide through. The status
pill shows *Focusing*, *Stabilizing n/N*, *Ready*, *Capturing - wait for the beep*, or
*Captured - drop the next card*.

**Why is it waiting?** When auto scanning waits more than 2 s for a card to become ready,
`scanner.log` says why, once a second (`_trace_waiting`): *no card outline found* (every 5 s), or
*card not ready (n/N)* with the frame's movement, drift, sharpness change and sharpness against
their limits (1%, 1%, 20%, `min_sharpness`). With **Settings → Debug trace** on, the scanner also
keeps the last 3 s of frames (640 px wide - what the outline detector works on) and saves them,
plus the next second, to `data/debug_frames/<time>_<reason>/` when a card waits over 2 s or a
new card is detected less than 2 s after a capture (a likely duplicate); the newest 20 dumps
are kept. Replaying such frames through `CardScanner` with a fake camera reproduces the case.

**The capture beep is the signal to drop the next card.** `auto_capture_triggered` (beep +
flash) is sent by `app.handle_auto_capture` once the image is taken and a focus probe started by
that capture is done (`announce_capture`; `scanner.capture_pending` / status `capturing` until
then). Auto-captures take the image at once (`capture_card_image_only(settle=0)` - the card has
already been still for `stability_frames`); the beep used to come *before* the image, which was
taken 0.3 s later, and a card dropped right away could land in it. Adding a card (after the AI,
1-2 s later) only plays the success ding. Measured with a fake camera and a probe on every
capture: image at t, probe done and beep at t + 1.0 s; without a probe, right after the image.

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

When the only sign was the outline vanishing for a frame and coming back shifted (a hand
approaching), the card must not settle as the very image just captured (`_outline_flicker`:
thumbnail difference < 0.05) - in a 67-card lot a card was captured twice this way (0.02), while
real copies of a card differed 0.07-0.50 and came with bigger signs (jumps, image changes).
Over that lot the pile grew 16% in height; pace (2.0-2.5 s per card), text sharpness and focus
stayed the same.

### Fixed area (sleeved cards)

On a pile of sleeved cards the outline is unreliable: the top card's outline merges with the
card underneath or the box's corner crease, or flips between the top card and the whole pile -
cards waited seconds and some were captured twice. The **Fixed area** toggle in the camera panel
(`set_fixed_area`, saved as `fixed_area_enabled` / `fixed_area` in `data/settings.json`) judges
cards by the image inside an area instead (`CardScanner._fixed_area_step`). The area is drawn
on the video (**Area**: drag around the card; saved as fractions of the frame) or taken from the
detected card plus 5% (**Use detected card**).

Per frame, a 48×64 grayscale thumbnail of the area with its mean brightness removed (so a
shadow or exposure change is not a change) is compared with the previous frame's:

| Measured on recorded sleeved piles | Thumbnail difference |
|---|---|
| Still card, frame to frame | ≤ 2.4 (≤ 5.4 over 1 s with the light changing) |
| A hand's shadow | ≤ 0.6 |
| A card falling in, frame to frame | 10-38, several frames in a row |
| A card settling after its capture (sleeve slide) | 13 in a single frame |
| A different card settled, compared with the last one | ~21 |

- **card present:** area sharpness ≥ `min_sharpness` (empty box ~30, a card ~1,600);
- **still:** change < 3 frame to frame and < 4 since the still streak began (a slow slide
  drifts), for `stability_frames` frames;
- **new card after a capture:** change > 8 in 2 frames in a row (a card falling in - also an
  identical copy), or the settled area differs > 10 from the captured one.

**The photo is the area as drawn.** The outline detector still runs, only for the ★/• foil
check: an outline inside the area gives the flat card whose corner is read (no outline, no foil
check). The photo used to be cut along that outline, but a holo Pokémon card's streak passed for
its top edge and the photo lost the card name - the area is what the user chose, so nothing
inside it is cut off. The outline is not drawn on the video in this mode. Replaying the five
recorded pile moments: every card captured once (the outline mode duplicated one); simulator
drops of identical copies, sleeve slides and focus probes: every card captured once, none
mid-slide.

### Adding automatically vs. reviewing

With **Add cards automatically** (default; internally `fast_scan_mode`, saved as `auto_add`):
the capture is queued, the AI worker identifies it in the background, and a confirmed printing
is added immediately (one Near Mint copy in the suggested finish). The page adds it by a token
(`auto_cards`), not through the server's current card, so it can't replace a card being reviewed
(the "current card" once changed under a pending review 2 s later). The web page shows each
added card with an **Undo** button (`undo_last_add`). A token no page claims within
`AUTO_ADD_TIMEOUT` (15 s - no page open, phone asleep) puts the card in the review queue instead
of losing it; with two pages open the first add wins and the other is ignored.

Anything uncertain - printing not confirmed, name not found, no name read - goes to the
**review queue** (`review.py`, table `review_queue`, a copy of the capture in `data/review/`)
and scanning goes on (it used to pause until the card was reviewed). Items keep what the AI
read, the ★/• result and the best match. The *Review* counter opens them oldest first
(`review_open` → `review_item`): the capture beside the suggested card, the AI read, and a
search prefilled with it (without a match kept, the search runs at once). Add resolves the item
- the capture becomes the entry's thumbnail - and the server sends the next; Skip drops it
(`review_skip`); Close leaves the rest (`review_close`). Cards added automatically meanwhile
don't disturb the open item, and a card captured during a review in any other way (manual
capture, auto scanning without automatic adds) goes to the queue too (`route_identified`)
instead of taking the reviewed card's place and capture. The review belongs to the page that
opened it (`review_sid`): when that page disconnects the server closes it, and the page opens
it again when it reconnects. Per game; kept across restarts.

With the switch off, each capture is identified synchronously and waits for **Add** / **Skip**;
a card dropped meanwhile is captured right after.

## Identification (vision AI)

`CardIdentifier.identify_card()` sends the flat card image (JPEG, longest side
`vision_ai.image_size`, 1024 px) with a prompt asking for three values from fixed places on the
card:

```
NAME: <card name>            top of the card
NUMBER: <collector number>   bottom-left, line 1 ("U 0014")
SET: <set code>              bottom-left, line 2 ("HOB • EN")
```

The answer format asks for all three lines with their labels ("…, or Unknown"): with a plain
"exactly three lines" qwen3.5:9b often dropped the labels and sometimes the number line
("Mirkwood / HOB"), which lost 4 of 25 cards in one session; with the labels it answered 25/25.
The parser still accepts answers without labels, or with only some of them, and places bare
lines by their shape (number or set code). It also copes with chatty answers - Markdown
(`- **NAME**: Riolu (The card is ...)`), a comment in parentheses, a trailing ★, a number buried
in a sentence (`The number at the bottom left is "84/145"`), a language code after the set
(`PAL EN`). Letters read for digits in a mostly-digit number are
corrected (`018B` → `0188`: O/D→0, B→8, I/l→1, S→5, Z→2). Ollama
answers are capped (`num_predict`), so a model that starts reasoning aloud can't take seconds.

### Prompts

Prompts live in `prompts.py` and are editable in Settings → Vision AI → **Edit prompts**. Each
prompt (card identification, foil marker) has two parts:

- **instructions** - what the card looks like and where each value is; this is what the editor
  changes;
- **answer format** - fixed (`NAME: / NUMBER: / SET:`, or "one word: star, dot, or unclear")
  and appended by the code, so an edit can never break the parser.

Edited instructions are saved in `data/prompts.json`, keyed by game (`mtg`; ready for other
games), prompt kind, and either `default` (all models) or `provider:model`. The prompt used is
this model's, else the all-models one, else the built-in one. **Restore default** removes the
saved prompt in effect (the model's first).

**Test on last capture** runs the AI on the last captured card (`scanner.last_capture`) with the
text in the editor, without saving, and shows the raw answer, how it was read, and the database
match it would get (confirmed → added automatically, or review). The foil test needs a capture
with a detected outline.

**The built-in identification prompt has no example values.** With examples ("E 0367",
"LTR · EN", "Lightning Bolt / 0367 / M21") models copied them on blurry cards instead of
answering Unknown - once producing a real but wrong LTR #367 printing. Measured on 90 recorded
scans (the 4 blurry ones checked by eye), with the native Ollama API:

| Prompt, image | qwen3.5:9b (server) | qwen3.5:4b (RTX 3050) |
|---|---|---|
| old prompt with examples, 2048 px | 86/90 correct, 84 confirmed, 1.42 s | - |
| same, answer format moved last, 2048 px | 87/90, 85 confirmed, 1.41 s | 89/90, 85 confirmed, 2.42 s |
| **no examples, 1024 px (built-in)** | **89/90, 83 confirmed, 0.92 s** | **89/90, 86 confirmed, 1.50 s** |
| no examples, 2048 px | 88/90, 84 confirmed, 1.27 s | 90/90, 85 confirmed, 2.23 s |
| no examples, 768 px | 87/90, 80 confirmed, 0.75 s | 88/90, 83 confirmed, 1.13 s |

No variant produced a confirmed (auto-added) wrong printing. Halving the image cuts the image
tokens (~1,400 → ~900), which is most of the prompt; shortening the text saved ~350 tokens.

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
| 3 | Name only (exact, flavor name, shortened), else fuzzy (`difflib`, cutoff 0.6, candidates sharing the first letters) - then the number to pick the printing (`name_number`), else the printing from the same set (if any) whose collector number is closest to the one read. If the name was read exactly and exactly one printing of it in the set read is one digit off the number read - a digit misread, dropped or doubled, compared as printed with leading zeros ("0189" for #188, "6186" for 0186, "017" for 0117) - that printing is certain (with two printings a digit away it goes to review) | `name_set_digit` / `name_set` / `name` / `fuzzy` |
| 4 | Name unrecognizable but set + number exist: trust the printed set + number | `set_number_unverified` |

Collector numbers are compared in their variants ("0014" → 14, 0014, 14s, 0014s). Names are
compared through `name_search` / `flavor_search` columns - lowercase, accent-free copies
(`search_key`: "Fíli" → "fili", "Æther" → "aether").

Only `CONFIRMED_MATCHES` (`set_number`, `name_number`, `name_set_digit`) are added automatically; the card panel
warns "Printing not confirmed" for the others.

**Manual search** (`CardSearcher.find_printings`): set + number go straight to the printing;
otherwise all printings of the name (optionally filtered by a treatment: regular, borderless,
showcase, extended art, full art, retro frame, etched, surge foil) are listed newest first and
shown as a picker when there's more than one.

### Pokémon

`games/pokemon.py` reads the same three values with its own prompt: the name with its suffix
(ex, V, VMAX, GX...), the number as printed with the set total (`012/193`, `TG05/TG30`, promo
codes like `SWSH095`), and the set abbreviation printed next to it on Scarlet & Violet era
cards (`PAL`); older cards only have a set symbol (Unknown). `Pokemon.identify` tries:

| Step | Match | Tag |
|---|---|---|
| 1 | Set abbreviation + number, if the name matches (same, one plus a suffix - "Charizard"/"Charizard ex", only real suffixes: ex, V, VMAX, GX...; "Energy" must not match "Energy Retrieval" -, ≥ 80% similar, or the card's name inside a sentence answer). A code no set has may be a misread one ("SYE" for SVE, "PREN" for PRE + EN): the codes one letter away count if exactly one has a card of that name and number | `set_number` |
| 2 | Name + number, narrowed by the set total (`/193` = the set's official card count); the exact name wins over longer ones (Charizard before Charizard δ). One card left → confirmed; several → first one for review; a total no set has → review | `name_number` / `name_number_ambiguous` / `name_number_other_total` |
| 3 | Name only (exact or fuzzy): the newest printing, for review | `name` / `fuzzy` |
| 4 | Name not recognized: the printed set + number | `set_number_unverified` |

Confirmed: `set_number`, `name_number`. Measured with qwen3.5:9b on official card images
(160 cards from Base Set to Mega Evolution, 4 random samples): 39 of 40 confirmed correctly in
each sample, ~0.94 s per card, **no confirmed wrong card**. The prompt is kept short and plain:
an earlier, longer one (what not to read, format hints) got answers as Markdown sentences for
14-23 of 40 cards, 35-38 confirmed, and 2-2.4 s per card on camera captures (now ~0.8-1.1 s).
The rest went to review - mostly basic Energy cards
(read as "ENERGY"), promos without a readable code, and cards the same name + number/total
exist in twice (Dugtrio 19/102 is in Base Set and Triumphant). An early version confirmed an
Eevee promo from its Pokédex number ("133/189") - no set has 189 cards, which is now a review.

**Basic Energy cards** print "Basic ⟨symbol⟩ Energy"; the data names them "Water Energy". The
model mostly copies "Basic Energy", and when asked for the type it can misname the symbol (Metal
read as Fairy), and it once read 011 as 017. So a basic Energy is matched by set code + number
only (`_identify_energy`; misread codes are tried one letter away, with and without a trailing
EN): confirmed when the type read agrees with the card, otherwise `set_number_energy` - shown
for review - so a misread number can't add another type. Without a set code + number that
finds exactly one Energy (older Energies print none) only the name is used: the newest
printing, for review. In the first real session all 4
Energies went wrong (a fuzzy "Basic Fire Energy", or "Energy Retrieval" by the prefix rule);
with this, 3 of the 4 captures give the right card (the 4th, number misread, goes to review).

**Finishes** (`normal`, `holo`, `reverse`, `first_edition`): the card panel only offers the
finishes the printing exists in (TCGdex `variants`). With one, it is certain; otherwise the
suggestion is Normal (Holo when there is no normal print) - reverse holos are not recognized
from the image and must be set by hand before adding.

**Prices** are TCGplayer market prices (USD) per finish from the TCGdex card details, which the
bulk data doesn't include: fetched when a card is matched or picked (~0.2 s) and cached in the
table for a day.

## Foil and finish

Modern cards print a star instead of a dot between set code and language on foil copies
(`HOB★EN` vs `HOB•EN`). For captures found by outline detection - where the corner is known to
be in the image - `read_foil_symbol()` sends the bottom-left corner (lowest 14% × left 45% of
the flat card, enlarged 3×) and asks which symbol it is (`vision_ai.detect_foil`).

The wording matters. Asked only "is the separator a STAR or a DOT?", qwen3.5:9b called regular
cards foil when the capture was a little soft: 25 of 84 regular cards (from a scanning session
where auto-added lands came out foil). Describing both shapes ("a dot is a plain round point; a
star has five sharp points") fixed that on the same set - checked by eye: 84/84 regular and
40/40 readable foils right; 3 unreadable, blurry foils were called dot. qwen3.5:4b still called
12 of the 84 regular cards foil (23 with the old wording). The prompt also says to answer
*unclear* when the set line is cut off: a crop that missed it (see below) was answered "dot"
and a foil went in as regular; with the sentence that crop gives *unknown*, and 160/160
readable cards (the set above plus later captures) stay right. The taller crop keeps the set line in
view when the detected outline also takes in the edge of the card underneath in the pile.

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

**`inventory`** (created and migrated by `inventory.py`) - one row per game + card name + set +
number + condition + finish (`UNIQUE`); adding an existing combination increases `quantity`.
Rows are addressed by `id` (edit, delete, undo). Also stores the printing id (`card_id`), set
code, rarity, type, mana cost, colors, color identity, price and timestamp. `finish` is one of
the game's finish keys (Magic: `regular`, `foil`, `surge`). Editing the finish of part of a stack
splits the row; an edit that makes a row identical to another merges them. A new finish takes
the printing's price in that finish (`Game.get_card` + `inventory_fields`; Pokémon prices are
refetched when older than a day); rows without a printing id (CSV imports) keep their price.

**`inventory_captures`** (also `inventory.py`) - one row per captured copy behind an entry:
`inventory_id`, `file` (a thumbnail in `data/captures/`, 400 px tall, ~25 KB - the captures in
`scanned_cards/` are deleted after `cleanup.days`), `captured_at`. The capture follows the card
from the AI worker to the add: `search_and_emit_card` puts it on the matched card
(`card['capture']`, so a queued automatic add can't take another card's photo), and
`pending_capture` keeps the one under review for a manual search (the automatic "not found"
dismissal keeps it, Skip drops it). Several finishes added at once arrive as one
`add_to_inventory` event (`items`) - separate events ran in parallel threads - and the photo
goes with the first. The photos follow the copies: undo removes that add's photo; moving
copies to another finish moves the newest photos with them; merging moves all; lowering a
quantity drops the newest photos (the usual reason is a card captured twice); deleting or
clearing entries deletes their files. Entries added before this, or imported, have none.
`/api/inventory` returns each entry's `captures` (newest first, URLs under `/captures/`).

Inventories from before multi-game support (`foil`/`surge` flags) are rebuilt once on startup:
the old table is first copied to `data/backups/inventory_before_multigame_<time>.db`, the
migration checks that the card count is unchanged, and it runs in one transaction.

Exports are per game (`Game.export_formats`; Magic: CSV with the classic columns, Moxfield CSV;
Pokémon: CSV with a `Finish` column); CSV import reads the `Finish` column or the older
`Foil`/`Surge` columns and merges duplicates.

**`pokemon_cards`** / **`pokemon_sets`** (created by `games/pokemon.py`) - TCGdex data: one
GraphQL request returns every card (~21k paper cards, a few MB); the printed set abbreviations
come from the REST set details (8 requests in parallel). Pokémon TCG Pocket (digital) is left
out. A download takes about 4 s; it happens automatically the first time Pokémon is selected.
Columns: identity (`id` "sv02-001", `name`, `set_id`, `set_code` "PAL", `set_name`,
`set_total`, `serie`, `released_at`, `number` as printed, `number_key` for comparing "012" /
"12" / "TG05"), card data (`rarity`, `category`, `types`, `stage`, `hp`, `trainer_type`,
`energy_type`), `finishes` (JSON), `image_url` (+ `/high.webp`, `/low.webp`), and the cached
`prices` / `prices_updated`. `pokemon_sets` lists every set of the last download, for the update
check. Prices are fetched per card when older than a day (5 s timeout) - never while matching
(`identify`), so scanning doesn't wait for them: a card shown for Add / Skip gets them before it
is shown, a review item when it is opened, and every add updates its entries' prices in the
background afterwards (`update_added_prices`, `Game.fetches_prices` / `with_prices`; the page
reloads the totals on `inventory_prices_updated`). After a failed
connection no price is requested for 5 minutes (`PRICE_RETRY_OFFLINE`), so scanning offline
doesn't wait 5 s per card - the cached prices, or none, are used.

~21k cards is the whole paper catalogue on TCGdex (checked 2026-09-25: every set within a few
cards of its total, newest set 9 days old). Pokémon prints far fewer cards than Magic (~112k
Scryfall printings), and holo / reverse holo are finishes of one row, not separate cards. What
TCGdex lacks: Jumbo cards (160), Radiant Collection as its own set (25), a few sample / promo
cards.

**Imports never leave a half-filled table.** Both games fill a staging table (`cards_import`,
`pokemon_cards_import`), committing every 5,000 rows so the inventory can still write, and swap
it in at the end in one step (`CardDatabase.replace_table`, under the database lock, then the
indexes are rebuilt) - scanning keeps using the old data while an update runs.

**`card_data_info`** - per game: the source's own date (Scryfall's `updated_at`; for Pokémon
the newest set's release date), when it was downloaded, and the card count.

**Update check.** 10 s after startup and then once a day, `Game.check_for_update()` runs for
each game with data. Magic: Scryfall's bulk data description (one small request) - since
Scryfall republishes every day for prices, the data only counts as outdated once it is
`database.update_after_days` (7) older than Scryfall's, or when its date is not recorded (data
downloaded before this check existed). Pokémon: any TCGdex set not in `pokemon_sets`. A result
is kept in `data_update_notices`, sent as `database_update_available`, and returned by
`/api/stats`; the page puts a dot on the Database counter (click → confirm → update) and shows
one notification per page load. Updates are never started without the user, except the first
Pokémon download.

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
- **The lens has play**: the same `focus_absolute` reached from above measured up to 5× blurrier
  than from below (395: 22 vs 118, peak 2,480). Sweeps measure while moving up, and every final
  or restored position is approached from 30 below (`_move_focus`).
- **Focus probe while scanning** (`_run_focus_probe`, every `auto_capture.refocus_every`
  captures, default 3): the best position drifts - in one session it moved from 395 to 430 in
  about 45 minutes (lens warming up, pile height), and the text in the captures became ~10×
  blurrier while the card as a whole still passed `min_sharpness`, so the automatic refocus
  never started. In the gap after a capture (~1 s, the image is already taken) the probe
  measures the card here and one step (10) away and keeps the sharper position (> 5% better);
  an improvement keeps the direction for the next probe, otherwise the next one tries the other
  way. Near the peak the sharpness changes ~3× per step, far more than the noise. Probes
  approach their positions from only 15 below (instead of 30) to keep the card readable.
- **While the lens moves** (sweep or probe, and 0.6 s after) the new-card rules are paused: the
  blur can hide the card for a few frames and shift its outline, and the "reappeared elsewhere"
  / "card gone" rules then took the same card for a new one (a probe caused two duplicate
  captures in a live session, reproduced in simulation: 22 captures for 15 drops). A real drop
  during a probe is still recognized by its jump (> 3%; the blur shifted the outline ≤ 1.9%) -
  in fixed-area mode by the area changing > 8 in 2 frames in a row: the card counts as new
  once the focus is done, and the probe's measurement is discarded. Otherwise the card in view
  becomes the reference for "looks different from the captured one": after a probe a foil's
  glare and outline changed enough (thumbnail 0.34, other cards 0.39-0.62) that a foil was
  captured twice. In fixed-area mode only when the area is still within 10 of the capture.
  Simulated with 30 drops every 1.5 s and a peak 35 away: every card captured once, focus at
  the peak after 9 cards.
- **Automatic refocus** (`_check_focus_drift`): with a locked focus, a card that stays still but
  below `auto_capture.min_sharpness` for 3 s triggers a new sweep (at most every 15 s) - the pile
  grows toward the camera as cards are added.
- During a sweep the status shows *Focusing* and auto-capture pauses; new-card detection is off
  until 0.6 s after it (the heavy blur changes the card image like a drop would).
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
| `capture_card`, `search_card`, `select_printing`, `add_to_inventory` (`finish` + `quantity`, or `items` for several finishes), `undo_last_add`, `dismiss_card` (`keep_capture` from the automatic "not found" dismissal) | `card_captured`, `card_found`, `card_printings`, `similar_cards`, `card_not_found`, `inventory_updated`, `inventory_prices_updated` (prices fetched after an add), `inventory_undone`, `card_dismissed` |
| `toggle_auto_capture`, `toggle_fast_scan` (add automatically), `toggle_detection`, `toggle_anti_glare`, `toggle_debug_trace`, `reset_focus` (refocus + lock), `set_autofocus`, `set_fixed_area` (`enabled` / `area` / `use_detected`), `set_camera_rotation` | `auto_capture_triggered` (image taken, focus probe done: drop the next card), `processing_queue_update`, `*_toggled`, `focus_reset`, `fixed_area_updated`, `camera_rotation_updated` |
| `set_ai_provider`, `save_ai_credential`, `update_database` (the active game's data), `rebuild_database` | `ai_provider_set`, `ai_credential_saved`, `database_update_progress` / `_complete` / `_error`, `database_update_available` (update check found newer data), `database_rebuild_*`, `log`, `error` |
| `save_prompt` (scope `model` / `all`), `reset_prompt`, `test_prompt` | `prompts_updated`, `prompt_test_result` (sent only to the client that asked) |
| `review_open`, `review_skip`, `review_close` | `review_item` (the oldest item, or `id: null` when empty), `review_queue_update` (count) |
| `set_game` | `game_changed` (to every client; stops auto scanning; downloads the game's card data if it has none) |

HTTP endpoints are listed in the README.

**Inventory captures.** Each entry shows its newest capture as a thumbnail. Hovering an entry
(only on devices with a mouse) shows a grid of its captures - up to 8, then "+N more"; clicking
the thumbnail (tapping, on a phone) opens all of them in a viewer.

**Card games.** The page loads `/api/games` (games, their finishes and export formats) and
builds the quantity grid, the edit dialog's finish choices and the export buttons from the
active game; the manual search's Treatment filter (Magic only), the set / number examples and
the card data hint follow it too. The game selector in the top bar only appears when more than
one game exists. Card payloads may carry `finish_options` (only those finishes are offered),
`prices` (`[[finish label, USD]]`) and `thumb_uri` (printing picker).

## Configuration and files

| Where | What |
|---|---|
| `config.yaml` | Camera, detection, auto-capture, vision AI defaults, web server, cleanup |
| `.env` | API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`); `VISION_AI_PROVIDER` and `LOCAL_AI_ENDPOINT` override `config.yaml` |
| `data/api_keys.env` | Keys and local endpoint entered in Settings (`api_keys.py`, mode 600); overrides `.env`. The UI only ever receives masked keys (`/api/ai_credentials`) - the web interface has no login |
| `data/settings.json` | Choices made in the UI: AI provider/model, add automatically, locked focus position |
| `data/prompts.json` | Prompt instructions edited in Settings, per game / kind / model (`prompts.py`) |
| `data/review/` | Captures waiting in the review queue (deleted when resolved) |
| `data/captures/` | Thumbnails of the captures behind inventory entries (deleted with their entry) |
| `data/cards_database.db` | Card data (Magic `cards`, Pokémon `pokemon_cards` / `pokemon_sets`, `card_data_info`) and inventory |
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
| AI identification | ~0.9 s (qwen3.5:9b, 1024 px image); the foil check runs in parallel (+~0.3 s with Ollama); ~10 s once if the model has to load |
| Set + number lookup | 0.1 ms; fuzzy name search ~90 ms |

Vision models compared on 90 scans (identification + foil check, before the prompt and image
size change - identification alone is now 0.9 s / 1.5 s, see *Prompts*):

| Model | Hardware | Time per card | Notes |
|---|---|---|---|
| `qwen3.5:9b` (Q4_K_M, 6.6 GB) | Ollama server on the network | 1.6 s | reference; says "unknown" when the ★/• is unreadable |
| `qwen3.5:4b` (Q4_K_M, 3.4 GB) | laptop RTX 3050 6 GB (fits entirely) | 3.4 s | image processing ~920 vs ~2,560 tokens/s on the server; called 3 regular cards foil |

The 9B doesn't fit in a 6 GB GPU (it would be split with the CPU); the 4B is a usable fallback.

## Known limitations

- **White-bordered cards on a white background** have no visible outline; use
  `detection.method: auto` (YOLO fallback) or capture them manually with detection off.
- **An identical copy landing within ~0.7 mm of the previous card** without the fall hiding the
  card for 6 frames isn't recognized as new - press Capture.
- **Cards without the ★/• marker** (older printings) get their finish from printing data only.
- **Pokémon reverse holos** are not recognized from the image: set the finish by hand. Pokémon
  support was tested on official card images, not yet on camera captures.
- **Undo** takes back only the most recent add; older adds are edited in the inventory.
- **One camera, one instance**: the camera can only be opened by one process.
