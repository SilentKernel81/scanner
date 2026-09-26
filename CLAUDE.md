# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Camera-based scanner for Magic: The Gathering cards, and Pokémon (Flask + Socket.IO web app). Cards are
dropped onto a pile in a box; each new card is found by its outline (OpenCV), captured once,
identified by a vision AI (Gemini / OpenAI / Anthropic / local Ollama: name, collector number,
set code, ★/• foil marker), matched to the exact printing in a local Scryfall SQLite database,
and added to an inventory. Runs on a Raspberry Pi or any Linux PC (USB webcam or Pi camera).

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
```

Dependencies: `requirements.txt` (core, ~300 MB, Python 3.10+); `requirements-yolo.txt` adds
the optional YOLO fallback detector + PyTorch (~1 GB; PyTorch may lag the newest Python). The
dev venv on this machine is Python 3.12 with YOLO installed.

There is no automated test suite. Verify changes by running the app (or a copy of it on another
port with a copy of the database - never test adds against the real `data/cards_database.db`
inventory), and for scanner logic by feeding recorded/synthetic frames through `CardScanner`
with a fake camera (patch `detect_camera_type` / `_initialize_usb_camera`).

## Where Things Live

| Area | Code |
|---|---|
| Outline detection, warp, YOLO fallback | `object_detector.py`: `find_card_outline` (+ `_track_outline` following the previous card, `_outline_from_edge_groups` for broken outlines), `warp_card`, `ObjectDetector.detect` |
| Capture loop, stability, auto-capture, new-card detection | `scanner.py`: `_capture_frames`, `_is_card_settled`, `_new_card_arrived`, `_mark_captured`; fixed area (sleeves): `_fixed_area_step`, `set_fixed_area` |
| Focus sweep / lock / automatic refocus | `scanner.py`: `focus_sweep`, `refocus`, `_run_focus_sweep`, `_run_focus_probe` (drift tracking between drops), `_move_focus` (approach from below: the lens has play), `_check_focus_drift`, `set_continuous_autofocus` |
| AI providers, foil check, Ollama warm-up | `card_identifier.py`: `_ask_*`, `identify_card`, `read_foil_symbol`, `warm_up` |
| Prompts (built-in + edited per model) | `prompts.py`: `BUILT_IN`, `prompt`, `save`, `reset`; editor events in `app.py` (`save_prompt`, `test_prompt`) |
| Card games (the active one drives search, finishes, exports) | `games/`: `base.Game`, `mtg.Magic`, `pokemon.Pokemon` (TCGdex), `games.active()`; plan in `MULTI_GAME_IMPLEMENTATION_PLAN.md` |
| Card data updates (staged import, update check) | `database.py`: `replace_table`, `card_data_info`; `Game.check_for_update`; `app.py`: `start_card_data_update`, `check_card_data_updates` |
| Card search / printing match / confidence (Magic) | `database.py`: `search_card_exact`, `search_card`, `find_printings`, `CONFIRMED_MATCHES`, `search_key`, `names_match` |
| Capture orchestration, AI queue, auto-add gate, events | `app.py`: `handle_auto_capture` (in `initialize_components`), `ai_processing_worker`, `search_and_emit_card`, `set_auto_add`; automatic adds on the server (`add_automatically`, `Game.suggested_finish`) |
| Review queue (unconfirmed cards while adding automatically) | `review.py` (`review_queue`, `data/review/`); `app.py`: `queue_for_review`, `review_open`/`review_skip`/`review_close`; `scanner.js`: `renderReview`, `reviewSearch` |
| Inventory add/merge/undo/split/export, capture thumbnails | `inventory.py` (`inventory_captures`, `data/captures/`); capture → add: `app.py` `pending_capture`, `card['capture']` |
| UI logic (finish suggestion, printing picker, status) | `static/js/scanner.js`: `suggestedFinish`, `displayCard`, `displayPrintings`, `updateDetectionStatus` |
| Settings | `config.yaml` (+ `config.py`), `.env` (API keys), `data/api_keys.env` (keys entered in the UI, `api_keys.py`), `data/settings.json` (UI choices: AI provider/model, `auto_add`, `focus_value`), `data/prompts.json` (edited prompts) |

## Conventions and Pitfalls

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
- **Frame sizes**: with a raw-JPEG camera ≥ 1920 px wide, `current_frame`, detections, corners
  and the stream are **half size**; only captures decode full size (`get_detected_card`,
  `get_full_frame`). Don't crop captures from `current_frame` directly.
- **CPU**: `cv2.setNumThreads(2)` in scanner.py - OpenCV's default (all cores) doubled CPU use.
- **Focus timing**: a `focus_absolute` change takes ~0.4 s to show up in frames; measure after
  that, or sweeps score the previous lens position.
- **Ollama**: requests must send `think: false` (thinking models otherwise return empty answers)
  and `keep_alive`; the parser accepts answers with or without `NAME:/NUMBER:/SET:` labels.
- **Thread safety**: frames/detection state under `scanner.frame_lock`; DB and inventory use
  their own `RLock`. Auto-capture callbacks and the AI worker run in their own threads.
- **Prompts**: change the built-in text in `prompts.py:BUILT_IN` (instructions + fixed
  `answer_format`); the parser relies on the answer format, which the editor cannot change.
  A user's saved prompt in `data/prompts.json` overrides built-in edits - check it when a prompt
  change seems to have no effect.
- **Never send full API keys to the browser** (no login on the web UI): `api_keys.credential_status()`
  masks them; the UI can only replace or remove a key.
- **No native `confirm()` / `alert()`** in the web UI: browsers can silently block them ("prevent
  this page from creating additional dialogs"), after which `confirm()` always returns false - this
  broke "Clear all". Use `confirmDialog()` / `choiceDialog()` / `notify()` in scanner.js.
- **New Socket.IO events** need a handler in `app.py` and in `static/js/scanner.js`, and a line
  in PROGRAM_DOCUMENTATION.md.
- **Logs**: `data/logs/app.log`, `ai.log`, `scanner.log`, `database.log`, `scanned_cards.log`.
