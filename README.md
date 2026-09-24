# MTG Card Scanner

A camera-based scanner for Magic: The Gathering cards. Put a card under the camera, and the
scanner finds it in the video feed, identifies the exact printing with a vision AI, tells you
whether it's foil, and adds it to a local inventory you can export to CSV or Moxfield.

It runs as a small web app (Flask + Socket.IO) on a Raspberry Pi or any Linux machine with a
USB webcam or Raspberry Pi camera, and is used from a browser on the same network.

## How it works

1. **Find the card** - each camera frame is searched for the card's outline: the largest
   four-sided shape with a card's 88×63 mm proportions. This takes a few milliseconds and
   isn't fooled by foil glare inside the card.
2. **Capture** - the card is cut out and perspective-corrected into a flat, upright image.
3. **Identify** - a vision AI reads the card name, collector number and set code from that image.
4. **Foil check** - modern cards print a star (`HOB★EN`) instead of a dot (`HOB•EN`) next to
   the set code on foil copies. The AI is shown a zoomed crop of that corner and asked which
   one it is.
5. **Match the printing** - set code + collector number (unique for every printing), checked
   against the name, are looked up in a local copy of
   [Scryfall](https://scryfall.com)'s card data, which also provides prices, images and
   which finishes each printing exists in.
6. **Add to inventory** - you confirm (or Fast Scan adds it automatically) and the card is
   stored with quantity, condition and finish.

## Features

- **Live camera view** with the detected card outlined; the status turns from
  *Stabilizing* to *Ready* when the card is still.
- **Manual or automatic capture** - capture on demand, or start auto scanning. **Fast Scan**
  captures quickly, identifies cards in the background and adds them automatically.
- **Vision AI providers** - Google Gemini, OpenAI, Anthropic Claude, or a self-hosted model
  via Ollama (or another OpenAI-compatible server). Switch provider and model from the UI.
- **Exact printing identification** from the set code and collector number, with name
  matching that tolerates misreads, accents ("Fili" → "Fíli"), flavor names ("Bucklebury
  Ferry") and shortened legendary names ("Thanos" → "Thanos, the Mad Titan").
- **Foil detection** from the ★/• marker, combined with printing data: printings that only
  exist in foil (or only non-foil) are known for certain. The card panel pre-selects the
  finish and shows why.
- **Manual search with treatment filter** - borderless, showcase, extended art, full art,
  retro frame, etched, surge foil. When several printings match, pick the one you have from
  a grid of thumbnails.
- **Inventory** with automatic duplicate merging, condition, regular / foil / surge foil,
  prices, filtering, editing, CSV import, and export to CSV or
  [Moxfield](https://moxfield.com).
- **Anti-glare** preprocessing option for reflective cards, sound effects, and a
  dark/light interface that follows your system theme.
- **Housekeeping** - scanned images older than 7 days are cleaned up automatically, and the
  card database can be updated from the UI.

## Requirements

- **Computer**: Raspberry Pi 4 or 5, or any Linux machine (developed on x86-64 Linux)
- **Camera**: USB webcam (autofocus strongly recommended) or Raspberry Pi Camera Module
- **Python**: 3.11 or 3.12 (tested with 3.12; very new Python releases may not have
  wheels for the computer vision packages yet)
- **Disk**: ~1.5 GB for dependencies (with CPU-only PyTorch), ~70 MB for the card database
- **AI**: an API key for Gemini, OpenAI or Anthropic, **or** a local Ollama server with a
  vision model
- **Internet**: to download the card database, show card images, and reach cloud AI providers

## Installation

```bash
git clone https://github.com/SilentKernel81/scanner.git
cd scanner

# Create a virtual environment (with uv: uv venv --python 3.12 venv)
python3 -m venv venv
source venv/bin/activate

# Optional but recommended on machines without an NVIDIA GPU: install the CPU-only
# PyTorch first - it is much smaller than the default build
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

pip install -r requirements.txt

# Download the card database from Scryfall (a few minutes)
python3 setup_database.py

# Add your API key(s)
cp .env.example .env    # then edit .env
```

PyTorch and Ultralytics are only used by the optional YOLO fallback detector
(`detection.method: auto` or `yolo`). With the default outline detection they are never loaded.

### Raspberry Pi camera

The Pi camera library is installed through the system package manager, so the virtual
environment must be able to see system packages:

```bash
sudo apt install python3-picamera2
python3 -m venv --system-site-packages venv
```

USB cameras need no extra setup. `v4l2-ctl` (package `v4l-utils`) is used to reset
autofocus, sharpness and zoom on startup.

### Pick your camera

Set `camera.usb_index` in `config.yaml` to your camera's `/dev/videoN` number:

```bash
v4l2-ctl --list-devices
```

## Choosing a vision AI provider

API keys are read from environment variables; `app.py` loads them from `.env` automatically.

| Provider | Setting | Key / endpoint |
|---|---|---|
| Google Gemini | `gemini` | `GEMINI_API_KEY` ([get a key](https://aistudio.google.com/app/apikey)) |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Anthropic Claude | `anthropic` | `ANTHROPIC_API_KEY` |
| Local (Ollama, vLLM, LM Studio) | `local` | `vision_ai.local.endpoint` in `config.yaml`, or `LOCAL_AI_ENDPOINT` |

The default provider is set by `vision_ai.provider` in `config.yaml` (or `VISION_AI_PROVIDER`),
but you can switch provider and model at any time in **Settings → Vision AI**; the choice is
remembered in `data/settings.json`.

**Local models**: pick a vision-capable model in **Settings → Vision AI** - the list is loaded
from your server (or check with `curl http://<server>:11434/api/tags`). Thinking is switched off for Ollama
requests, so "thinking" models such as `qwen3.5` answer in about a second instead of
reasoning for tens of seconds. The model is preloaded when you start auto scanning and kept
loaded for 30 minutes, so the first card doesn't wait ~10 s for it to load.

The foil check sends one extra small request per card. It is free with a local model; for
cloud providers you can turn it off with `vision_ai.detect_foil: false`.

## Running

```bash
source venv/bin/activate
python3 app.py
```

Then open `http://localhost:5000` (or `http://<device-ip>:5000` from another device).

`scripts/start.sh` does the same but also loads `.env`, activates the virtual environment and
checks your API key and database first. To run the scanner as a service on a Raspberry Pi,
see `mtg-scanner.service` (it assumes the project lives in `/home/pi/scanner`).

## Using the scanner

### Scanning cards

1. Put a card in front of the camera. It gets an outline in the video and the status pill
   shows **Focusing** / **Stabilizing**, then **Ready** once the card is still and sharp.
2. Click **Capture card**, or **Start auto scanning** to capture every time a new card is
   ready.
3. The card panel shows the identified printing, its price and treatment. The finish
   (Regular / Foil / Surge foil) is pre-selected with the reason, e.g.
   *"Foil: ★ next to the set code"* or *"only printed in foil"*.
4. Adjust quantity or condition if needed and click **Add to inventory**, or **Skip**.

Auto scanning is made for dropping cards onto a pile in the box. Each card is captured once:
after a capture the status shows **Captured - drop the next card**, and the next capture happens
when a new card has been dropped on top and has settled. The drop is recognized by the motion
(the card briefly vanishing or jumping, a hand) and by where the new card lands, so two
identical copies in a row are both captured. In normal mode it also waits until you've added or
skipped the current card; a card dropped before you click Add is captured right after.

With **Fast Scan mode** (Settings) cards are identified in the background and added
automatically, so you can keep feeding cards; the *Processing* counter in the top bar shows
how many are still being identified. Only cards whose exact printing is confirmed (set code +
number, or name + number) are added automatically - anything less certain pauses auto
scanning so you can review it.

### Searching manually

Type a name in **Search**, optionally with the **Set** code and **Number** from the card's
bottom-left corner (e.g. `HOB` and `14`) - together they go straight to the exact printing -
and a **Treatment** (e.g. Borderless). If more than one printing matches, choose yours from
the thumbnail grid. After a capture, the fields are filled with what the AI read, so you can
correct a misread and search again. Press Enter in any field to search.

### Inventory

Open it from the inventory count in the top bar. You can filter, edit quantity / condition /
finish (changing the finish of part of a stack splits it), delete, clear, import a CSV, and
export to **CSV** or **Moxfield**. Adding a card that is already in the inventory with the same
condition and finish increases its quantity instead of creating a duplicate.

### Tips for reliable scans

- **Contrast**: a light, plain background (e.g. a white box) makes the card's dark border
  easy to find. White-bordered cards on a white background have no visible outline - use
  `detection.method: auto` (YOLO fallback) or capture them with auto-detection turned off.
- **Keep the whole card in view** with a small margin. If an edge is cut off, the card isn't
  detected - and the foil marker in the bottom-left corner can't be read.
- **Light** evenly from above; the **Anti-glare** option helps with reflective foils.
- Cards printed before the ★/• convention (roughly before 2020) have no foil marker; for
  those, set the finish yourself when both versions exist.

## Configuration

Settings live in `config.yaml`. The most useful ones:

| Setting | Default | Description |
|---|---|---|
| `camera.type` | `auto` | `auto`, `usb` or `picamera` |
| `camera.usb_index` | | `/dev/videoN` number of the USB camera |
| `camera.resolution` / `fps` | `[2560, 1440]` / `20` | Capture resolution and frame rate |
| `detection.method` | `contour` | `contour` (outline only), `auto` (outline, then YOLO), `yolo` |
| `detection.allow_landscape` | `false` | Accept cards lying sideways (a card's art box can look like a sideways card) |
| `auto_capture.delay` | `1.0` | Minimum seconds between automatic captures |
| `auto_capture.stability_frames` | `5` | Still, in-focus frames required before capturing |
| `auto_capture.min_sharpness` | `250` | Minimum sharpness for auto-capture; lower it if cards stay on *Focusing* |
| `fast_scan.stability_frames` | `4` | Same, in Fast Scan mode (~0.15 s, lets a dropped card stop sliding) |
| `anti_glare.enabled` | `false` | Default for the anti-glare toggle |
| `vision_ai.provider` | `gemini` | Default AI provider (see above) |
| `vision_ai.detect_foil` | `true` | Read the ★/• foil marker |
| `vision_ai.local.endpoint` | | Local AI server (the model is chosen in Settings) |
| `flask.host` / `port` | `0.0.0.0` / `5000` | Web server address |
| `cleanup.enabled` / `days` | `true` / `7` | Delete scanned images older than N days on startup |

Environment variables `VISION_AI_PROVIDER` and `LOCAL_AI_ENDPOINT` override the matching
settings.

## Maintenance

| Task | How |
|---|---|
| Update card data and prices | **Settings → Update card database**, or `python3 setup_database.py` |
| Scanned image statistics | `python3 cleanup.py --stats` |
| Delete old scanned images | `python3 cleanup.py --days 30` (add `--dry-run` to preview, `--all` for everything) |
| Back up database, inventory, settings, images and `.env` | `scripts/backup.sh` (set `SCANNER_DIR` at the top first) |

Your inventory lives in the same SQLite file as the card data (`data/cards_database.db`,
table `inventory`); updating the card database does not touch it.

Logs are written to `data/logs/`:

| File | Contents |
|---|---|
| `app.log` | Web app, searches, inventory actions |
| `ai.log` | AI requests and responses |
| `scanner.log` | Camera, detection, captures |
| `database.log` | Database queries |
| `scanned_cards.log` | One CSV line per identified card |

## Troubleshooting

**No camera found / black video** - check the device number with `v4l2-ctl --list-devices`
and set `camera.usb_index`. Only one program can use the camera at a time.

**Auto scanning never captures** - the status tells you why: *Focusing* (image not sharp
enough - check focus, or lower `auto_capture.min_sharpness`), *Stabilizing* (card still
moving), or *Captured - drop the next card* (it's waiting for a new card to land). The log
shows *New card detected* with the measured jump and image change for each drop.

**Card not detected** - make sure the whole card is visible with some margin and the
background contrasts with the border (see *Tips*). For cards without a clear outline, set
`detection.method: auto` to fall back to YOLO.

**"Vision AI disabled"** - no API key was found for the selected provider. Check `.env`, or
switch provider in Settings.

**Local AI returns 404** - Ollama answers 404 when the requested model isn't installed. Pick
one of the models listed in Settings (they come from your server) or `ollama pull` it.

**Wrong printing** - check the set code and number the AI read (shown in the Search bar after
a capture); correct them there and search again, or pick the printing from the grid.

**Installation fails on a very new Python** - create the virtual environment with Python
3.12 (e.g. `uv venv --python 3.12 venv`).

## Project structure

```
app.py               Flask + Socket.IO web app, routes and event handlers
scanner.py           Camera capture thread, detection, stability, auto-capture
object_detector.py   Card outline detection + perspective correction (YOLO fallback)
card_identifier.py   Vision AI providers, card identification, foil marker check
database.py          Scryfall card database: download, schema, search, printings
card_search.py       Search helpers used by the web app
inventory.py         Inventory storage, stats, import/export
anti_glare.py        Glare reduction for reflective cards
cleanup.py           Scanned image cleanup (also a CLI)
setup_database.py    Downloads and builds the card database
config.yaml          Settings (loaded by config.py / config_loader.py)
settings.py          UI preferences saved in data/settings.json
templates/, static/  Web interface
scripts/             start.sh, backup.sh
data/                Card database, settings, logs (created at runtime)
scanned_cards/       Captured card images (created at runtime)
```

`CLAUDE.md` has more detailed notes on the architecture, database schema and search logic.

## HTTP API

The web interface uses these endpoints, which you can also call directly:

| Endpoint | Description |
|---|---|
| `GET /video_feed` | MJPEG stream of the annotated camera view |
| `GET /api/stats` | Database and inventory statistics |
| `GET /api/detection_status` | Whether a card is detected and how stable it is |
| `GET /api/inventory` | Full inventory |
| `POST /api/inventory/update/<index>` | Update quantity, condition or finish (JSON body) |
| `POST /api/inventory/delete/<index>` | Delete an inventory entry |
| `POST /api/import_inventory` | Import a CSV (multipart `file`) |
| `POST /api/clear_inventory` | Delete all inventory entries |
| `GET /api/export_inventory` | Download the inventory as CSV |
| `GET /api/export_inventory_moxfield` | Download a Moxfield import CSV |
| `GET /api/ai_provider` | Current AI provider and model |
| `GET /api/ai_models` | Built-in model lists for each provider |
| `GET /api/local_ai_models` | Models installed on the local AI server |

Scanning, searching and settings go through Socket.IO events (`capture_card`, `search_card`,
`select_printing`, `add_to_inventory`, `toggle_auto_capture`, ...); see `app.py`.

## Acknowledgments

- Card data, prices and images: [Scryfall](https://scryfall.com)
- Computer vision: [OpenCV](https://opencv.org); optional detection fallback:
  [Ultralytics YOLO](https://github.com/ultralytics/ultralytics)
- Web: [Flask](https://flask.palletsprojects.com) and
  [Flask-SocketIO](https://flask-socketio.readthedocs.io)

This is an unofficial fan project, not affiliated with or endorsed by Wizards of the Coast.
Magic: The Gathering is a trademark of Wizards of the Coast LLC.
