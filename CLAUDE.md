# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Camera-based scanner for Magic: The Gathering cards (Flask + Socket.IO web app). Cards are
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
| Outline detection, warp, YOLO fallback | `object_detector.py`: `find_card_outline`, `warp_card`, `ObjectDetector.detect` |
| Capture loop, stability, auto-capture, new-card detection | `scanner.py`: `_capture_frames`, `_is_card_settled`, `_new_card_arrived`, `_mark_captured` |
| AI providers, prompts, foil check, Ollama warm-up | `card_identifier.py`: `_ask_*`, `CARD_IDENTIFICATION_PROMPT`, `read_foil_symbol`, `warm_up` |
| Card search / printing match / confidence | `database.py`: `search_card_exact`, `search_card`, `find_printings`, `CONFIRMED_MATCHES`, `search_key`, `names_match` |
| Capture orchestration, AI queue, auto-add gate, events | `app.py`: `handle_auto_capture` (in `initialize_components`), `ai_processing_worker`, `search_and_emit_card`, `set_auto_add` |
| Inventory add/merge/undo/split/export | `inventory.py` |
| UI logic (finish suggestion, printing picker, status) | `static/js/scanner.js`: `suggestedFinish`, `displayCard`, `displayPrintings`, `updateDetectionStatus` |
| Settings | `config.yaml` (+ `config.py`), `.env` (API keys), `data/settings.json` (UI choices: AI provider/model, `auto_add`) |

## Conventions and Pitfalls

- **Schema**: cards columns are defined once in `database.py:CARD_COLUMNS`; missing columns are
  added on startup (`initialize_database`). Rows are `sqlite3.Row` - access by column name.
  New search-relevant columns may need filling in the migration (see `name_search`).
- **Match confidence**: `search_card_exact` tags results (`card['match']`); only
  `CONFIRMED_MATCHES` may be added without review. Keep new search paths tagged.
- **Auto-capture thresholds** in `scanner.py` come from measured camera noise and a live drop
  test (documented in PROGRAM_DOCUMENTATION.md). Re-measure before changing them.
- **JSON from NumPy**: values sent through `jsonify`/Socket.IO must be plain Python types
  (`bool(...)`, `float(...)`) - a `numpy.bool_` broke `/api/detection_status` once.
- **Restart after template changes**: Flask caches `templates/scanner.html`; a running app keeps
  serving the old page. Static files are served fresh - bump the `?v=N` query in the template
  when changing CSS/JS so browsers don't use cached copies.
- **The camera is exclusive**: only one process can open it; stop the running app before
  testing with the real camera.
- **Ollama**: requests must send `think: false` (thinking models otherwise return empty answers)
  and `keep_alive`; the parser accepts answers with or without `NAME:/NUMBER:/SET:` labels.
- **Thread safety**: frames/detection state under `scanner.frame_lock`; DB and inventory use
  their own `RLock`. Auto-capture callbacks and the AI worker run in their own threads.
- **New Socket.IO events** need a handler in `app.py` and in `static/js/scanner.js`, and a line
  in PROGRAM_DOCUMENTATION.md.
- **Logs**: `data/logs/app.log`, `ai.log`, `scanner.log`, `database.log`, `scanned_cards.log`.
