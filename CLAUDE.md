# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python-based real-time card scanner for Magic: The Gathering cards. Uses Flask + SocketIO for web interface, YOLOv8 for object detection, and Scryfall API for card database. Runs on Raspberry Pi with either PiCamera2 or USB webcam.

## Common Commands

### Setup and Installation
```bash
# Create venv (Python 3.12 - torch/opencv wheels may lag the newest Python)
uv venv --python 3.12 venv
uv pip install --python venv/bin/python torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python venv/bin/python -r requirements.txt

# Download card database from Scryfall (gzipped JSONL, a few minutes)
venv/bin/python setup_database.py

# API keys go in .env (loaded by app.py via python-dotenv)
cp .env.example .env
```

### Running the Application
```bash
# Start the web server (default: http://0.0.0.0:5000)
python3 app.py
```

### Cleanup
```bash
# View scanned images statistics
python3 cleanup.py --stats

# Manually clean up images older than 7 days (default)
python3 cleanup.py

# Clean up images older than 30 days
python3 cleanup.py --days 30

# Preview what would be deleted without actually deleting
python3 cleanup.py --dry-run

# Delete all scanned images
python3 cleanup.py --all
```

## Architecture

### Component Flow
1. **Scanner (scanner.py)** - Captures video frames in background thread, runs YOLOv8 detection, crops detected cards
2. **Object Detection (object_detector.py)** - YOLO wrapper for rectangular card detection (uses pre-trained model, ignores class names)
3. **Card Identification (card_identifier.py)** - Vision AI (Gemini/GPT-4/Claude) identifies specific Magic card from cropped image
4. **Database (database.py)** - SQLite wrapper for Scryfall card data with fuzzy matching
5. **Search (card_search.py)** - Card lookup and similarity matching
6. **Inventory (inventory.py)** - SQLite database-based inventory tracking with automatic duplicate detection
7. **Web App (app.py)** - Flask + SocketIO orchestration

### Threading Model
- Main thread: Flask/SocketIO event loop
- Background thread: Continuous frame capture in `scanner.py` (`_capture_frames()`)
- Frame access protected by `threading.Lock` (`frame_lock`)

### Camera Abstraction
The scanner auto-detects camera type:
- **USB Camera**: Uses OpenCV VideoCapture with index 0
- **PiCamera**: Uses PiCamera2 library
- Detection order: USB first, then PiCamera (configurable via `Config.CAMERA_TYPE`)

### Database Schema
**Cards Table:** id, name, flavor_name, set_code, set_name, collector_number, rarity, price_usd, price_usd_foil, image_uri, oracle_text, type_line, colors, mana_cost, plus printing-treatment fields from Scryfall: border_color, frame, frame_effects (JSON), full_art, promo_types (JSON), finishes (JSON), released_at. Columns are defined once in `database.py:CARD_COLUMNS`; missing columns are added automatically on startup (run "Update Card Database" to fill them). Rows are read by column name (`sqlite3.Row`).

**Performance Indexes:**
- `idx_card_name`: Single-column index on name (case-insensitive)
- `idx_card_flavor_name`: Single-column index on flavor_name (case-insensitive)
- `idx_card_set_number`: Composite index on (set_code, collector_number) for exact version lookups
- `idx_card_rarity`: Single-column index on rarity for filtering
- `idx_card_type`: Single-column index on type_line for type-based searches

**Flavor Names:** Special printings (like Universes Beyond) may have alternate names. For example, "Bucklebury Ferry" (Lord of the Rings) is stored with Oracle name "Oboro, Palace in the Clouds" but includes flavor_name "Bucklebury Ferry" for searchability. All search functions check both name and flavor_name fields.

**Database Optimization:** The "Rebuild Database Schema" button copies the cards table (by column name) into the canonical column order and rebuilds its indexes. The inventory table is not touched.

**Inventory Table:** id, card_name, set_name, card_number, rarity, type_line, mana_cost, colors, color_identity, price_usd, quantity, condition, foil, surge, timestamp. UNIQUE constraint on (card_name, set_name, card_number, condition, foil, surge) for duplicate detection. Indexed on `card_name COLLATE NOCASE` and `set_name`.

### Configuration
All settings centralized in `config.py`:
- Camera resolution: 2560x1440 (configurable)
- Camera FPS: 30
- **Autofocus: Enabled** (continuous autofocus always active)
- **Frame stabilization: 5 frames required** before "Ready" state
- **Auto-capture: Controlled via "Start Auto Scanning" button** (disabled by default)
- Database location: `data/cards_database.db`
- **Automatic cleanup: Enabled** (runs on app startup, deletes images older than 7 days)

## Key Implementation Details

### Card Detection & Identification Workflow
**Detection (Real-time):**
1. YOLOv8 model (`yolov8n.pt`) detects rectangular objects in each frame
2. Pre-trained COCO model used - class name ignored (all objects labeled "Card")
3. Bounding box drawn on annotated frame
4. Card region cropped and stored in `scanner.detected_card`

**Identification (On Capture):**
1. User triggers capture (or auto-capture)
2. Cropped card image sent to Vision AI (Gemini/GPT-4/Claude)
3. AI identifies BOTH:
   - Card name (from top of card)
   - Collector number (from bottom left corner, e.g., "123/456")
4. Database search uses BOTH fields to find exact card version
5. Falls back to name-only search if exact match not found
6. User confirms and adds to inventory

**Why Collector Number Matters:**
Cards with same name can have different printings (sets, art, rarities, prices). Using collector number ensures we identify the EXACT version of the card being scanned.

### Frame Processing Pipeline
1. Capture frame (RGB)
2. Run YOLO detection (if enabled)
3. **Track frame stability** (increment counter if card detected, reset if not)
4. Draw bounding box (orange if stabilizing, green if ready)
5. Crop detected card region
6. Store both annotated and raw frames with lock protection
7. On capture: Wait 0.3s for settling, apply enhanced preprocessing, send to Vision AI

**Enhanced Preprocessing (`_enhance_for_ai`):**
1. Sharpen image (edge enhancement)
2. CLAHE contrast adjustment (better text visibility)
3. Noise reduction (cleaner image for AI)

**Stability Indicator:**
- Orange box + "Stabilizing X/5" = Card detected, autofocus active, not yet stable
- Green box + "Ready" = Stable, ready for capture

### Vision AI Configuration
Set environment variables for your chosen provider:
- **Gemini**: `export GEMINI_API_KEY=your_key_here` (default)
- **OpenAI**: `export OPENAI_API_KEY=your_key_here`
- **Anthropic**: `export ANTHROPIC_API_KEY=your_key_here`

Change provider in `config.py`: `VISION_AI_PROVIDER = 'gemini'|'openai'|'anthropic'`
Disable AI: `VISION_AI_ENABLED = False`

### SocketIO Events
- `connect`: Initial client connection
- `capture_card`: Manual card capture trigger
- `search_card`: Manual search by name + optional collector number + optional treatment (`database.py:TREATMENT_FILTERS`). One match → `card_found`; several → `card_printings` (client shows a printing picker)
- `select_printing`: User picked a printing (by Scryfall id) from the picker → `card_found`
- `add_to_inventory`: Add card to database inventory (supports surge foil, auto-increments quantity for duplicates)
- `dismiss_card`: Cancel current card review and clear selection
- `toggle_detection`: Enable/disable YOLOv8 detection
- `toggle_auto_capture`: Enable/disable automatic card capture
- `toggle_anti_glare`: Enable/disable anti-glare filter
- `reset_focus`: Reset camera autofocus
- `set_ai_provider`: Change Vision AI provider (gemini/openai/anthropic)
- `update_database`: Update card database from Scryfall (runs in background thread, ~5-10 minutes)
- `rebuild_database`: Rebuild database schema with optimized structure and indexes (runs in background, ~30 seconds)

### Search Strategy
All search methods check both `name` and `flavor_name` fields:
1. Exact match: Case-insensitive SQL query (e.g., "Bucklebury Ferry" finds "Oboro, Palace in the Clouds")
2. Fuzzy match: Python `difflib.get_close_matches()` with 0.6 cutoff
3. Partial match: SQL LIKE query for similar cards

### Important Notes
- **Python version**: Use Python 3.12 venv (system Python may be too new for torch wheels)
- **Virtual environment**: Project uses Python venv (see `pyvenv.cfg`)
- **Camera initialization**: 2-second warm-up after camera setup
- **Autofocus**: Enabled for USB cameras via `cv2.CAP_PROP_AUTOFOCUS`
- **Model file**: YOLOv8 model (`yolov8n.pt`) must be present in project root
- **Database requirement**: App checks for database existence before starting
- **Code refactoring**: AI prompts consolidated in `card_identifier.py:CARD_IDENTIFICATION_PROMPT`, duplicate search logic extracted to `app.py:search_and_emit_card()`, shared utilities in `utils.py`

### Web Interface
- Single-page app: `templates/scanner.html`
- Video stream: MJPEG via `/video_feed` route
- Real-time updates: SocketIO for logs, card detection, search results
- Static assets: CSS/JS in `static/` directory
- **Database Management Buttons** (Settings panel):
  - **Update Card Database**: Downloads latest card data from Scryfall (~150MB, 5-10 min)
  - **Rebuild Database Schema**: Optimizes database structure with proper column ordering and performance indexes (~30 sec)

### Inventory Management
Cards stored in SQLite database (`inventory` table) with automatic duplicate detection:
- Card details: Name, Set, Collector Number, Rarity, Type
- **Mana information**: Mana Cost, Colors (W/U/B/R/G), Color Identity (White/Blue/Black/Red/Green/Multicolor/Colorless)
- Pricing: USD price (foil/non-foil)
- **Condition & metadata**: Condition, Foil status, **Surge Foil status**, Quantity, Timestamp

**Foil Support:**
- **Regular Foil**: Standard foil treatment
- **Surge Foil**: Special surge foil variant (introduced in recent Magic sets)
- Surge and regular foil are tracked separately for accurate pricing and collection management

**Duplicate Detection:**
When adding a card that already exists (same name + set + card number + condition + foil + surge), the system automatically increments the quantity instead of creating a duplicate entry. This ensures clean inventory management and accurate counts.

**Color Identity Logic:**
- Single color → "White", "Blue", "Black", "Red", "Green"
- Multiple colors → "Multicolor"
- No colors → "Colorless"

**Export:**
- Standard CSV: `/api/export_inventory` - Full inventory with all fields
- Moxfield CSV: `/api/export_inventory_moxfield` - Moxfield-compatible format for importing to Moxfield.com
- Both exports available via web interface buttons
- Files saved to: `data/card_inventory_export_YYYYMMDD_HHMMSS.csv` and `data/moxfield_export_YYYYMMDD_HHMMSS.csv`

## Development Workflow

When adding features:
1. Check `config.py` for relevant configuration options
2. Scanner modifications require understanding thread safety (use `frame_lock`)
3. Database changes should update both schema in `database.py` and `setup_database.py`
4. New SocketIO events need handlers in both `app.py` (server) and client JS
5. No automated tests - verify by running `app.py` and checking startup logs

When debugging:
- Check console output - extensive logging to stdout
- SocketIO events logged with `print()` statements
- Camera issues: Verify camera type detection in logs
- Database issues: Run `setup_database.py` to verify
