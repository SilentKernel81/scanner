# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Camera-based scanner for Magic: The Gathering cards (Flask + Socket.IO web app). Cards are
dropped onto a pile in a box; each new card is found by its outline (OpenCV), captured once,
identified by a vision AI (Gemini / OpenAI / Anthropic / local Ollama: name, collector number,
set code, ★/• foil marker), matched to the exact printing in a local Scryfall SQLite database,
and added to an inventory. Runs on a Raspberry Pi or any Linux PC (USB webcam or Pi camera).

The Android app (a Kotlin port of detection, auto-capture, AI, matching and the inventory) lives
in its own repository, [SilentKernel81/mtg-scanner-android](https://github.com/SilentKernel81/mtg-scanner-android)
(checked out beside this one, `../mtg-scanner-android`); its README maps each Kotlin file to the
Python code here - keep both in step when changing that logic.

**How everything works is documented in [PROGRAM_DOCUMENTATION.md](PROGRAM_DOCUMENTATION.md)**
(detection, auto-capture rules, matching, foil logic, schema, events, measured thresholds) -
read the relevant section before changing behavior, and keep it up to date.
User docs: [README.md](README.md); deployment: [INSTALL.md](INSTALL.md).

## Common Commands

```bash
./scripts/deploy.sh                       # install/repair: packages, venv, .env, camera check, card DB
./scripts/deploy.sh --service             # + systemd service (template: scripts/mtg-scanner.service)
./scripts/deploy.sh --update              # git pull + update + restart service
venv/bin/python app.py                    # run (http://localhost:5000)
venv/bin/python setup_database.py         # (re)download the Scryfall card database
venv/bin/python cleanup.py --stats        # scanned images; --days N / --dry-run / --all
./scripts/build-appimage.sh               # dist/Card_Scanner-<arch>.AppImage (own Python, packages, Node.js)
```

Dependencies: `requirements.txt` (~300 MB, Python 3.10+). The dev venv on this machine is
Python 3.12 - which accepts syntax 3.10/3.11 cannot parse: never nest an f-string (or any
string) with the same quotes inside an f-string's `{}`, and no backslash there.

There is no automated test suite. Verify changes by running the app (or a copy of it on another
port with a copy of the database - never test adds against the real `data/cards_database.db`
inventory or `data/scan_inventory.db`), and for scanner logic by feeding recorded/synthetic frames through `CardScanner`
with a fake camera (patch `detect_camera_type` / `_initialize_usb_camera`).

## Where Things Live

| Area | Code |
|---|---|
| Outline detection, warp | `object_detector.py`: `find_card_outline` (+ `_track_outline` following the previous card, `_outline_from_edge_groups` for broken outlines, `_card_inside_box` when the largest outline is the box itself), `warp_card`, `ObjectDetector.detect` |
| Capture loop, stability, auto-capture, new-card detection | `scanner.py`: `_capture_frames` (camera missing / lost: `_open_camera`, `_read_failed`, `_wait_for_camera`), `_is_card_settled`, `_new_card_arrived`, `_mark_captured`; fixed area (sleeves): `_fixed_area_step`, `set_fixed_area` |
| Focus sweep / lock / automatic refocus | `scanner.py`: `focus_sweep`, `refocus`, `_run_focus_sweep`, `_run_focus_probe` (drift tracking between drops), `_move_focus` (approach from below: the lens has play), `_check_focus_drift`, `set_continuous_autofocus` |
| OCR first (light-ocr reader process, parsers, AI fallback) | `card_ocr.py`: `CardOcr`, `parse_magic`, `PARSERS`; `ocr/server.mjs` (Node.js); `scanner.py`: `identify_card_from_image`; `Game.confirmed_read` |
| AI providers, foil check, Ollama warm-up | `card_identifier.py`: `_ask_*`, `identify_card`, `read_foil_symbol`, `warm_up` |
| Prompts (built-in + edited per model) | `prompts.py`: `BUILT_IN`, `prompt`, `save`, `reset`; editor events in `app.py` (`save_prompt`, `test_prompt`) |
| Card games (the active one drives search, finishes, exports / imports per site: `export_formats`, `import_rows`) | `games/`: `base.Game`, `mtg.Magic`, `games.active()`; plan in `MULTI_GAME_IMPLEMENTATION_PLAN.md` |
| Card data updates (staged import on its own connection, checked before it is published, update check) | `database.py`: `populate_database`, `_fill_import_table`, `replace_table`, `card_data_info`; `Game.check_for_update`; `app.py`: `start_card_data_update`, `check_card_data_updates` |
| Card search / printing match / confidence (Magic) | `database.py`: `search_card_exact`, `search_card`, `find_printings`, `CONFIRMED_MATCHES`, `search_key`, `names_match` |
| Capture orchestration, AI queue, auto-add gate, events | `app.py`: `handle_auto_capture` (in `initialize_components`), `ai_processing_worker`, `search_and_emit_card`, `set_auto_add`; automatic adds on the server (`add_automatically`, `Game.suggested_finish`) |
| Review queue (unconfirmed cards while adding automatically) | `review.py` (`review_queue`, `data/review/`); `app.py`: `queue_for_review`, `review_open`/`review_skip`/`review_close`; `scanner.js`: `renderReview`, `reviewSearch` |
| Inventory add/merge/undo/split/export, locations and tags, bulk edits, capture thumbnails | `inventory.py` (`KEY_COLUMNS`, `update_card`, `bulk_update`, `inventory_captures`, `data/captures/`); capture → add: `app.py` `pending_capture`, `card['capture']`; `scan_location` |
| Collection page (`/collection`: inventory filters / grid / bulk bar, deck builder, statistics) | `templates/collection.html`, `static/js/collection.js`, `static/css/collection.css`; shared with the scanner page: `static/js/common.js`, `templates/_topbar.html` (the header of both pages), `templates/_dialogs.html`, `templates/_icons.html` |
| Backups of the collection, scanned cards and decks (collection page's settings drawer) | `backups.py`: `create`, `create_daily` (at startup, one per day), `restore`, `list_backups`, `archive` / `add_archive` (download / upload as a zip) (`data/backups/<date_time>/`); `app.py`: `collection_backups`, `restore_backup`; `collection.js`: `openSettings`, `renderBackups`, `backupAction` |
| Decks (lists; ownership computed from the inventory), deck checks, decklist text | `decks.py`: `DeckManager`; `games/mtg_decks.py`: `DECK_FORMATS`, `check_deck`, `parse_decklist`; `app.py`: `deck_payload`, `resolve_entries`; `database.py`: `search_cards`, `cards_by_names` |
| Deck ideas from other sites (EDHREC, MTGJSON, Archidekt, Moxfield) | `recommendations.py`: `Recommendations` (`_get` cache + throttle, `Unavailable`); `app.py`: `deck_suggestions`, `popular_decks`, `run_deck_ideas` |
| UI logic (finish suggestion, printing picker, status) | `static/js/scanner.js`: `suggestedFinish`, `displayCard`, `displayPrintings`, `updateDetectionStatus` |
| AppImage, running as a desktop program | `scripts/build-appimage.sh` (run by `.github/workflows/appimage.yml` when a release is published), `packaging/` (`AppRun`, desktop entry, icon); `paths.py`: `USER_DIR`, `tool`; `app.py`: `--app` / `--no-browser` / `--quit` (`parse_arguments`, `open_browser_when_ready`, `quit_program`); restart (`restart_program`, `os.execv` in `main`); `templates/_program.html`, `common.js`: `quitProgram`, `restartProgram` |
| Settings | `config.yaml` (+ `config.py`), `.env` (API keys), `data/api_keys.env` (keys entered in the UI, `api_keys.py`), `data/settings.json` (UI choices: AI provider/model, `auto_add`, `focus_value`), `data/prompts.json` (edited prompts) |

## Conventions and Pitfalls

- **Paths**: `paths.py` splits `BASE_DIR` (shipped, maybe read-only: code, templates, static,
  `ocr/`, default `config.yaml`) from `USER_DIR` (`data/`, `scanned_cards/`, `.env`, the user's
  `config.yaml` overlay) - the same folder in a checkout, `~/.local/share/mtg-scanner` when
  packaged, `MTG_SCANNER_HOME` to choose. Never write under `BASE_DIR` or build a data path
  from `__file__`: use `Config.DATA_DIR` / `Config.IMAGES_DIR`; name such a file in a message
  with `Config.shown('logs', 'app.log')`, not a literal `data/...`. Programs the app starts
  (`node`, `v4l2-ctl`) are found with `paths.tool()` (the AppImage's `bin/` first, then PATH);
  `v4l2-ctl` may be missing - the camera then runs without focus control.
- **Schema**: cards columns are defined once in `database.py:CARD_COLUMNS`; missing columns are
  added on startup (`initialize_database`). Rows are `sqlite3.Row` - access by column name.
  New search-relevant columns may need filling in the migration (see `name_search`).
- **Match confidence**: `search_card_exact` tags results (`card['match']`); only the game's
  `confirmed_matches` (Magic: `CONFIRMED_MATCHES`) may be added without review - check with
  `game.is_confirmed(card)`. Keep new search paths tagged.
- **Games**: app code goes through `games.active()` (identify, find_printings, card_payload,
  inventory_fields, export_formats), never straight to `database`/`CardSearcher`. Inventory rows
  carry `game` and `finish` (a key of `game.finishes`) and are addressed by `id`; the inventory
  schema lives in `inventory.py`, not `database.py`.
- **Auto-capture thresholds** in `scanner.py` come from measured camera noise and a live drop
  test (documented in PROGRAM_DOCUMENTATION.md). Re-measure before changing them.
- **JSON from NumPy**: values sent through `jsonify`/Socket.IO must be plain Python types
  (`bool(...)`, `float(...)`) - a `numpy.bool_` broke `/api/detection_status` once.
- **Restart after template changes**: Flask caches `templates/scanner.html`; a running app keeps
  serving the old page. Static files are served fresh - bump the `?v=N` query in the template
  when changing CSS/JS so browsers don't use cached copies.
- **The camera is exclusive**: only one process can open it; stop the running app before
  testing with the real camera. Camera *controls* (`v4l2-ctl -c ...`) can be changed while
  another process streams - handy for focus experiments measured through `/video_feed`.
- **Machine-specific values don't belong in `config.yaml`**: it ships in the AppImage. The
  camera (`usb_index: auto`), the Ollama endpoint (localhost) and the listen address
  (`host: auto`) are neutral there; a setup's own values go in Settings (`data/settings.json`,
  `data/api_keys.env`). The capture thread alone opens and closes the camera (`set_camera`
  only flags the change).
- **The camera may be missing**: the app must start and serve the collection without it.
  `scanner.camera_error` says why (also in `/api/detection_status`); `scanner.camera` is `None`
  then and the capture thread retries (`_open_camera`). Don't log per failed frame or retry.
- **Frame sizes**: with a raw-JPEG camera ≥ 1920 px wide, `current_frame`, detections, corners
  and the stream are **half size**; only captures decode full size (`get_detected_card`,
  `get_full_frame`). Don't crop captures from `current_frame` directly.
- **CPU**: `cv2.setNumThreads(2)` in scanner.py - OpenCV's default (all cores) doubled CPU use.
- **Focus timing**: a `focus_absolute` change takes ~0.4 s to show up in frames; measure after
  that, or sweeps score the previous lens position.
- **Ollama**: requests must send `think: false` (thinking models otherwise return empty answers)
  and `keep_alive`; the parser accepts answers with or without `NAME:/NUMBER:/SET:` labels.
- **OCR parser**: keep `parse_magic` strict - a loose collector-number pattern once confirmed
  the wrong printing. Re-run a batch of `scanned_cards/` through `CardOcr.read_card` +
  `Game.confirmed_read` (on a copy of the database) and compare with the AI before loosening it.
- **Edits are transactions**: a method of `InventoryManager` / `DeckManager` that changes
  anything does it inside `with self._transaction():` (`storage.py`) and never calls
  `conn.commit()` itself - an exception then rolls the whole edit back. Delete entries with
  `_delete_entries` (captures and batches go along; files only after the commit). Triggers
  refuse a quantity of 0: delete the row instead of counting it down to zero.
- **Schema changes are numbered steps**: add a function to the manager's `SCHEMA_STEPS`
  (`storage.upgrade`, one transaction per step, version in `schema_versions`); never edit a
  step that has shipped. Test on a copy of a real database and compare totals.
- **Batches**: every way copies come into an inventory writes `inventory_batches`
  (`_note_batch`), and every way they leave or move keeps it in step (`_take_from_batches`,
  `_trim_batches`) - an entry's batch rows never exceed its quantity.
- **Thread safety**: frames/detection state under `scanner.frame_lock`; DB and inventory use
  their own `RLock`. Auto-capture callbacks and the AI worker run in their own threads.
- **Prompts**: change the built-in text in `prompts.py:BUILT_IN` (instructions + fixed
  `answer_format`); the parser relies on the answer format, which the editor cannot change.
  A user's saved prompt in `data/prompts.json` overrides built-in edits - check it when a prompt
  change seems to have no effect.
- **Requests from other web pages are refused** (`reject_other_sites`, `socket_origin_allowed`
  in `app.py`): never set `cors_allowed_origins="*"` or add CORS headers, and keep changes
  behind POST/DELETE (a GET from another site is only stopped under `/api/`).
- **Never send full API keys to the browser** (no login on the web UI): `api_keys.credential_status()`
  masks them; the UI can only replace or remove a key.
- **No native `confirm()` / `alert()`** in the web UI: browsers can silently block them ("prevent
  this page from creating additional dialogs"), after which `confirm()` always returns false - this
  broke "Clear all". Use `confirmDialog()` / `choiceDialog()` / `notify()` in common.js.
- **Two pages share code**: helpers used by both the scanner and the collection page live in
  `static/js/common.js` / `templates/_dialogs.html`; the header is `templates/_topbar.html`
  (brand, page links and the settings button must stay in the same place on every page - only
  the `.topbar-page` part is a page's own; each page defines `openSettings()`); each page defines `inventoryChanged()`.
  `scanner.js` runs scanner-only code on load - never include it in another page.
- **`hidden` vs. display**: `.btn`, `.chip-toggle` etc. set `display`, which beats the `hidden`
  attribute; `collection.css` has `[hidden] { display: none !important }` for that page.
- **Two inventories**: `scan_inventory` (the scanner page: `data/scan_inventory.db`) and
  `inventory` (the collection). Scanning code adds to `scan_inventory`; decks, statistics and
  "owned" read `inventory`; REST routes pick with `inventory_area()` (`?area=scan`). The
  `stats` in `inventory_*` Socket.IO events are the scanned cards' (the scanner's top bar).
- **Inventory key**: `location` is part of `inventory.KEY_COLUMNS` (and the table's UNIQUE);
  anything that looks an entry up by its key must include it. Changing the key means rebuilding
  the table (`_migrate_add_location` is the pattern: backup, keep ids, check counts).
- **Other sites** (`recommendations.py`, cache in `data/web_cache.db` - disposable, bounded): only MTGJSON is a published API. Go through `_get`
  (cache, 1 request/s per site), catch shape changes and raise `Unavailable`; never call these
  from scanning code paths.
- **New Socket.IO events** need a handler in `app.py` and in `static/js/scanner.js`, and a line
  in PROGRAM_DOCUMENTATION.md.
- **Logs**: `data/logs/app.log`, `ai.log`, `scanner.log`, `database.log`, `scanned_cards.log`; `requests.log` (web requests) only with Debug mode on.
