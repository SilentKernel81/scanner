# Magic: The Gathering Card Scanner

A real-time card scanner for Magic: The Gathering cards using computer vision and AI. Built for Raspberry Pi with support for both PiCamera2 and USB webcams.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.11-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Overview

This project provides an automated solution for scanning and cataloging Magic: The Gathering cards. It uses YOLOv8 for real-time card detection, Vision AI (Gemini/GPT-4/Claude) for card identification, and Scryfall's database for card information lookup.

### Key Capabilities

- **Real-time Detection**: Continuously detects cards in camera feed using YOLOv8
- **AI Identification**: Identifies specific cards using Vision AI (reads card name and collector number)
- **Database Integration**: Pulls card data from Scryfall's comprehensive database
- **Inventory Management**: Track your collection with CSV exports (standard and Moxfield formats)
- **Web Interface**: User-friendly interface with real-time video feed and controls
- **Smart Focus**: Automatic focus adjustment with stability detection

## Features

### Core Features

- ✅ **Real-time card detection** with YOLOv8 object detection
- ✅ **Vision AI identification** supporting multiple providers:
  - Google Gemini (default)
  - OpenAI GPT-4 Vision
  - Anthropic Claude Vision
- ✅ **Scryfall database integration** (~150MB, 90,000+ cards)
- ✅ **Automatic focus adjustment** with smart lock mechanism
- ✅ **Frame stabilization** (5-frame buffer for stability)
- ✅ **Database inventory tracking** with automatic duplicate detection and quantity management
- ✅ **Multiple export formats**:
  - Standard CSV with full card details
  - Moxfield-compatible CSV for easy import
- ✅ **Web interface** with live video feed
- ✅ **Dual camera support** (USB webcam or Raspberry Pi Camera)

### Advanced Features

- 📊 **Inventory statistics** (color breakdown, rarity analysis, total value)
- 🔍 **Fuzzy search** for card lookups
- 🎯 **Collector number matching** for exact version identification
- 📸 **Image capture** and storage
- 🎨 **Color identity** classification
- 💰 **Price tracking** (USD, foil/non-foil)
- 🏷️ **Condition tracking** (Near Mint, Lightly Played, etc.)
- ✨ **Foil variant support** (Regular foil and surge foil tracking)

## System Requirements

### Hardware

- **Raspberry Pi 4** (recommended) or Raspberry Pi 5
- **Camera**: USB Webcam or Raspberry Pi Camera Module v2/v3
- **RAM**: 4GB minimum (8GB recommended)
- **Storage**: 8GB+ SD card (16GB recommended)
- **Optional**: Hailo AI accelerator (experimental support)

### Software

- **OS**: Raspberry Pi OS (Bullseye or later)
- **Python**: 3.11+
- **Internet**: Required for initial setup and Vision AI calls

## Installation

### Method 1: Automatic Installation (Recommended)

```bash
# Clone the repository
git clone <repository-url>
cd scanner

# Run the installation script
chmod +x install.sh
./install.sh
```

### Method 2: Manual Installation

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Download Scryfall database (~150MB, 5-10 minutes)
python3 setup_database.py

# Test your system
python3 test_system.py
```

### Vision AI Setup

You'll need an API key from one of these providers:

**Option 1: Google Gemini (Free Tier Available)**
```bash
export GEMINI_API_KEY="your_api_key_here"
```
Get your key at: https://makersuite.google.com/app/apikey

**Option 2: OpenAI GPT-4**
```bash
export OPENAI_API_KEY="your_api_key_here"
```
Get your key at: https://platform.openai.com/api-keys

**Option 3: Anthropic Claude**
```bash
export ANTHROPIC_API_KEY="your_api_key_here"
```
Get your key at: https://console.anthropic.com/

Configure provider in `config.py`:
```python
VISION_AI_PROVIDER = 'gemini'  # or 'openai' or 'anthropic'
```

## Quick Start

### 1. Start the Scanner

```bash
# Activate virtual environment
source venv/bin/activate

# Start the web server
python3 app.py
```

The server starts at `http://0.0.0.0:5000`

### 2. Access the Web Interface

Open your browser and navigate to:
- Local: `http://localhost:5000`
- Network: `http://<raspberry-pi-ip>:5000`

### 3. Scan a Card

1. Place a card in front of the camera
2. Wait for the frame to turn **green** with "Ready" status
3. Click **"Capture Card"** or wait for auto-capture
4. Review the identified card
5. Confirm to add to inventory

### 4. Export Your Inventory

**Command Line:**
```bash
python3 export_inventory.py
```

**Web Interface:**
- Click the inventory icon (📦)
- Choose export format:
  - **Export CSV** - Full format with all fields
  - **Export to Moxfield** - Import-ready for Moxfield

## Architecture

### System Overview

```
┌─────────────────┐
│  Web Browser    │
│  (User)         │
└────────┬────────┘
         │ HTTP/WebSocket
         ▼
┌─────────────────┐
│  Flask + SocketIO│
│  (app.py)       │
└────────┬────────┘
         │
    ┌────┴────┬──────────┬────────────┬──────────┐
    ▼         ▼          ▼            ▼          ▼
┌─────────┐ ┌──────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐
│Scanner  │ │Object│ │Card ID  │ │Database │ │Inventory │
│         │ │Detect│ │(AI)     │ │(Scryfall│ │(SQLite)  │
└─────────┘ └──────┘ └─────────┘ └─────────┘ └──────────┘
    │           │         │            │            │
    ▼           ▼         ▼            ▼            ▼
┌─────────┐ ┌──────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐
│Camera   │ │YOLO  │ │Gemini/  │ │SQLite   │ │SQLite    │
│(USB/Pi) │ │v8    │ │GPT-4/   │ │(Cards)  │ │(Inventory│
│         │ │      │ │Claude   │ │         │ │ Table)   │
└─────────┘ └──────┘ └─────────┘ └─────────┘ └──────────┘
```

### Component Details

#### 1. **scanner.py** - Frame Capture & Detection
- Background thread for continuous frame capture
- Thread-safe frame access with locks
- YOLOv8 integration for card detection
- Frame stabilization logic (5-frame buffer)
- Smart autofocus with lock mechanism

#### 2. **object_detector.py** - YOLO Wrapper
- YOLOv8 model initialization
- Rectangular object detection
- Bounding box extraction
- Pre-trained COCO model (class names ignored)

#### 3. **card_identifier.py** - Vision AI Integration
- Multi-provider support (Gemini/GPT-4/Claude)
- Card name extraction
- Collector number extraction
- Image preprocessing and enhancement

#### 4. **database.py** - Scryfall Database
- SQLite database wrapper
- Card information storage
- Indexed searches
- Fuzzy matching support

#### 5. **card_search.py** - Search Engine
- Exact name matching
- Fuzzy name matching (0.6 threshold)
- Collector number filtering
- Partial name matching

#### 6. **inventory.py** - Inventory Management
- SQLite database storage with `inventory` table
- Automatic duplicate detection (increments quantity for existing cards)
- Card addition with enriched data
- Statistics calculation (color breakdown, rarity analysis)
- Multiple export formats (standard CSV, Moxfield CSV)
- Quantity tracking with auto-increment
- Condition tracking and foil variant support (regular foil and surge foil)
- UNIQUE constraint on (card_name, set_name, card_number, condition, foil, surge)

#### 7. **app.py** - Web Application
- Flask web server
- SocketIO real-time communication
- REST API endpoints
- Event handlers
- MJPEG video streaming

### Data Flow

**Card Detection Flow:**
```
Camera → Frame Capture → YOLO Detection → Bounding Box → Crop → Store
   ↓                                                               ↓
   └──────────────────→ Annotated Frame Display ←─────────────────┘
```

**Card Identification Flow:**
```
User Capture → Wait 0.3s → Enhance Image → Vision AI → Extract Name+Number
                                               ↓
                                          Database Search
                                               ↓
                                          Exact Match? → Return Card
                                               ↓ No
                                          Fuzzy Match? → Return Best Match
                                               ↓ No
                                          Partial Match → Return Suggestions
```

**Inventory Flow:**
```
Confirmed Card → Enrich with Database Data → Check for Duplicate
                                                    ↓
                                            Exists? → Increment Quantity
                                                    ↓ No
                                            Insert New → Update Stats
       ↓
   Save Image → Store in scanned_cards/
```

## Configuration

All configuration is centralized in `config.py`:

### Camera Settings

```python
CAMERA_TYPE = 'auto'  # 'auto', 'usb', or 'picamera'
CAMERA_RESOLUTION = (2560, 1440)
CAMERA_FPS = 30
CAMERA_AUTOFOCUS = True
CAMERA_FOCUS_LOCK_ENABLED = True
CAMERA_FOCUS_LOCK_DELAY = 1.0  # seconds
```

### Detection Settings

```python
DETECTION_ENABLED = True
DETECTION_METHOD = 'yolo'
YOLO_MODEL = 'yolov8n.pt'
YOLO_CONFIDENCE = 0.5
YOLO_IMG_SIZE = 640
```

### Stability Settings

```python
STABLE_FRAMES_REQUIRED = 5  # Frames needed for "Ready" state
```

### Vision AI Settings

```python
VISION_AI_ENABLED = True
VISION_AI_PROVIDER = 'gemini'  # 'gemini', 'openai', or 'anthropic'
GEMINI_MODEL = 'gemini-1.5-flash'
```

### Auto-Capture Settings

**Note:** Auto-capture is now controlled via the "Start Auto Scanning" button in the UI, not via config.

```python
AUTO_CAPTURE_DELAY = 2.0  # seconds after stability
AUTO_CAPTURE_WAIT_FOR_FOCUS = True  # Wait for focus lock before capturing
```

### File Paths

```python
DATA_DIR = Path('data')
DATABASE_FILE = DATA_DIR / 'cards_database.db'  # Scryfall card data + inventory
CARD_IMAGES_DIR = Path('scanned_cards')
```

## Usage

### Command-Line Tools

#### Setup Database
```bash
python3 setup_database.py
```
Downloads and processes Scryfall's card database.

#### Test System
```bash
python3 test_system.py
```
Verifies database, camera, and directory setup.

#### Test Camera
```bash
python3 test_camera.py
```
Opens camera feed for testing.

#### Test Vision AI
```bash
python3 test_vision_ai.py path/to/card/image.jpg
```
Tests AI identification on a specific image.

#### Export Inventory
```bash
python3 export_inventory.py
```
Interactive export with statistics and format options.

#### Search for Cards
```bash
python3 test_search.py "Lightning Bolt"
```
Search the database for specific cards.

#### Camera Diagnostics
```bash
python3 camera_diagnostics.py
```
Display camera capabilities and settings.

### Web Interface

#### Main Controls

- **🎬 Toggle Detection** - Enable/disable card detection
- **📸 Capture Card** - Manually capture current frame
- **📦 Inventory** - View and manage your collection
- **🔍 Search Cards** - Search Scryfall database

#### Inventory Management

- **🔄 Refresh** - Reload inventory data
- **📥 Export CSV** - Export full inventory
- **📥 Export to Moxfield** - Export Moxfield-compatible format
- **🔍 Filter** - Search by name, set, or rarity
- **✏️ Edit Quantity** - Click quantity to edit
- **🗑️ Delete** - Remove cards from inventory

#### Visual Indicators

- **Orange Box + "Stabilizing X/5"** - Card detected, not yet stable
- **Green Box + "Ready"** - Card stable, ready to capture
- **Bright Green Box + "LOCKED"** - Focus locked, optimal capture time

### REST API

#### GET Endpoints

```
GET /video_feed
    Returns: MJPEG video stream

GET /api/detection_status
    Returns: {enabled: boolean, method: string}

GET /api/inventory
    Returns: {cards: [...], stats: {...}}

GET /api/export_inventory
    Returns: CSV file (standard format)

GET /api/export_inventory_moxfield
    Returns: CSV file (Moxfield format)
```

#### SocketIO Events

**Client → Server:**
```javascript
// Capture card
socket.emit('capture_card');

// Toggle detection
socket.emit('toggle_detection');

// Search card
socket.emit('search_card', {query: 'Lightning Bolt'});

// Add to inventory
socket.emit('add_to_inventory', {
    card: {...},
    condition: 'Near Mint',
    is_foil: false,
    is_surge: false,  // Surge foil variant
    quantity: 1
});

// Export inventory
socket.emit('export_inventory');
socket.emit('export_inventory_moxfield');
```

**Server → Client:**
```javascript
// Log messages
socket.on('log', (data) => {
    // data: {timestamp, level, message}
});

// Card detected
socket.on('card_detected', (data) => {
    // data: {card_info}
});

// Card added
socket.on('card_added', (data) => {
    // data: {card_info, stats}
});

// Inventory updated
socket.on('inventory_updated', (data) => {
    // data: {cards, stats}
});

// Search results
socket.on('search_results', (data) => {
    // data: {query, results}
});
```

## Troubleshooting

### Common Issues

#### Camera Not Detected

```bash
# Check USB camera
ls /dev/video*

# Check PiCamera
libcamera-hello

# Run camera diagnostics
python3 camera_diagnostics.py
```

#### Database Errors

```bash
# Re-download database
python3 setup_database.py

# Verify database
python3 check_card_in_db.py "Lightning Bolt"
```

#### Vision AI Errors

```bash
# Test API key
python3 test_vision_ai.py test_image.jpg

# Check environment variable
echo $GEMINI_API_KEY

# Try different provider in config.py
```

#### NumPy Version Issues

The project requires NumPy 1.x for OpenCV compatibility:
```bash
pip install "numpy<2.0"
```

#### Focus Issues

```bash
# Test autofocus manually
python3 test_focus.py

# Adjust focus settings in config.py
CAMERA_FOCUS_LOCK_DELAY = 2.0  # Increase for slower focus
```

### Log Files

```bash
# View application logs
tail -f data/logs/card_scanner.log

# Check recent errors
grep ERROR data/logs/card_scanner.log
```

## Development

### Project Structure

```
scanner/
├── app.py                      # Main Flask application
├── scanner.py                  # Frame capture & detection
├── object_detector.py          # YOLOv8 wrapper
├── card_identifier.py          # Vision AI integration
├── database.py                 # Scryfall database
├── card_search.py              # Search engine
├── inventory.py                # Inventory management
├── ocr_processor.py            # OCR (legacy)
├── config.py                   # Configuration
├── setup_database.py           # Database setup
├── migrate_inventory.py        # CSV to DB migration tool
├── export_inventory.py         # Export tool
├── requirements.txt            # Dependencies
│
├── templates/
│   └── scanner.html           # Web interface
│
├── static/
│   ├── css/
│   │   └── scanner.css        # Styles
│   └── js/
│       └── scanner.js         # Client-side logic
│
├── data/
│   ├── cards_database.db      # SQLite DB (Scryfall + Inventory)
│   └── logs/                  # Application logs
│
├── scanned_cards/             # Captured card images
├── models/                    # ML models (YOLO)
└── docs/                      # Documentation
```

### Adding a New Vision AI Provider

1. Edit `card_identifier.py`:

```python
def identify_card(self, image):
    if self.provider == 'your_provider':
        return self._identify_with_your_provider(image)

def _identify_with_your_provider(self, image):
    # Implementation here
    pass
```

2. Update `config.py`:

```python
VISION_AI_PROVIDER = 'your_provider'
YOUR_PROVIDER_API_KEY = os.getenv('YOUR_PROVIDER_API_KEY')
```

3. Add to `requirements.txt`:

```
your-provider-sdk>=1.0.0
```

### Extending the Database

The database schema (`database.py`) has two main tables:

**Cards Table (Scryfall data):**
```sql
CREATE TABLE cards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    set_code TEXT,
    set_name TEXT,
    collector_number TEXT,
    rarity TEXT,
    price_usd REAL,
    price_usd_foil REAL,
    image_uri TEXT,
    oracle_text TEXT,
    type_line TEXT,
    colors TEXT,
    mana_cost TEXT
)
```

**Inventory Table (Your collection):**
```sql
CREATE TABLE inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name TEXT NOT NULL,
    set_name TEXT NOT NULL,
    card_number TEXT,
    rarity TEXT,
    type_line TEXT,
    mana_cost TEXT,
    colors TEXT,
    color_identity TEXT,
    price_usd REAL,
    quantity INTEGER NOT NULL DEFAULT 1,
    condition TEXT DEFAULT 'Near Mint',
    foil INTEGER DEFAULT 0,
    surge INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL,
    UNIQUE(card_name, set_name, card_number, condition, foil, surge)
)
```

To add fields:

1. Update schema in `database.py`
2. Modify `setup_database.py` to populate new fields (for cards table)
3. Update `inventory.py` to use new fields (for inventory table)
4. Re-run `python3 setup_database.py` (for cards) or update migration script (for inventory)

### Testing

```bash
# Run all tests
python3 test_system.py

# Test individual components
python3 test_camera.py
python3 test_detection.py
python3 test_vision_ai.py image.jpg
python3 test_search.py "Card Name"
```

## Contributing

Contributions are welcome! Areas for improvement:

- [ ] Multi-card detection (scan multiple cards simultaneously)
- [ ] Offline card identification (no Vision AI required)
- [ ] Mobile app integration
- [ ] Additional export formats (Archidekt, DeckBox, etc.)
- [ ] Barcode scanning support
- [ ] Price tracking over time
- [ ] Collection statistics dashboard
- [ ] Card condition grading assistance

### Development Workflow

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

MIT License - See LICENSE file for details

## Acknowledgments

- **Scryfall** - Card database API
- **Ultralytics** - YOLOv8 object detection
- **OpenCV** - Computer vision library
- **Flask** - Web framework
- **SocketIO** - Real-time communication

## Support

For issues, questions, or contributions:
- Open an issue on GitHub
- Check existing documentation
- Review troubleshooting section

---

**Version**: 1.0.0
**Last Updated**: November 2025
**Platform**: Raspberry Pi (Linux ARM)
