#!/usr/bin/env python3
"""
Card Scanner Web Application
Main Flask application with SocketIO - COMPLETE VERSION
"""

from flask import Flask, render_template, Response, jsonify, request, send_file, send_from_directory
from flask_socketio import SocketIO, emit
import cv2
from datetime import datetime
from pathlib import Path
import sys
import time
import logging
import threading
import queue
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

# Load API keys etc. from .env before config is imported (config reads env vars)
load_dotenv(Path(__file__).parent / '.env')

# Keys entered in the web interface (data/api_keys.env) override .env
from api_keys import load_saved_keys, credential_status, save_credential  # noqa: E402
import prompts  # noqa: E402
load_saved_keys()

# ============================================================================
# Logging Configuration
# ============================================================================

def setup_logging():
    """Configure logging with separate log files for different components"""
    # Create logs directory
    log_dir = Path(__file__).parent / 'data' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_formatter = logging.Formatter(
        '%(levelname)s: %(message)s'
    )

    # Console handler - WARNING and above only
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(console_formatter)

    # Configure root logger (catches everything not specifically handled)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    # Silence Werkzeug (Flask development server) logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Silence SocketIO/EngineIO logging
    logging.getLogger('socketio').setLevel(logging.WARNING)
    logging.getLogger('engineio').setLevel(logging.WARNING)

    # ========================================================================
    # APP LOGGER - Flask application, routes, general events
    # ========================================================================
    app_logger = logging.getLogger('card_scanner')
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False  # Don't send to root logger

    app_file_handler = RotatingFileHandler(
        log_dir / 'app.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    app_file_handler.setLevel(logging.INFO)
    app_file_handler.setFormatter(detailed_formatter)
    app_logger.addHandler(app_file_handler)
    app_logger.addHandler(console_handler)

    # ========================================================================
    # AI LOGGER - Vision AI identification, model changes, API calls
    # ========================================================================
    ai_logger = logging.getLogger('ai')
    ai_logger.setLevel(logging.INFO)
    ai_logger.propagate = False

    ai_file_handler = RotatingFileHandler(
        log_dir / 'ai.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    ai_file_handler.setLevel(logging.INFO)
    ai_file_handler.setFormatter(detailed_formatter)
    ai_logger.addHandler(ai_file_handler)
    ai_logger.addHandler(console_handler)

    # ========================================================================
    # SCANNER LOGGER - Camera/scanner operations, detection events
    # ========================================================================
    scanner_logger = logging.getLogger('scanner')
    scanner_logger.setLevel(logging.INFO)
    scanner_logger.propagate = False

    scanner_file_handler = RotatingFileHandler(
        log_dir / 'scanner.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    scanner_file_handler.setLevel(logging.INFO)
    scanner_file_handler.setFormatter(detailed_formatter)
    scanner_logger.addHandler(scanner_file_handler)
    scanner_logger.addHandler(console_handler)

    # ========================================================================
    # DATABASE LOGGER - Database queries, inventory operations
    # ========================================================================
    db_logger = logging.getLogger('database')
    db_logger.setLevel(logging.INFO)
    db_logger.propagate = False

    db_file_handler = RotatingFileHandler(
        log_dir / 'database.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    db_file_handler.setLevel(logging.INFO)
    db_file_handler.setFormatter(detailed_formatter)
    db_logger.addHandler(db_file_handler)
    db_logger.addHandler(console_handler)

    # Scanned Cards Logger (CSV-like format for easy parsing)
    scanned_cards_logger = logging.getLogger('scanned_cards')
    scanned_cards_logger.setLevel(logging.INFO)
    scanned_cards_logger.propagate = False

    # Custom format for scanned cards log (CSV-like)
    scanned_cards_formatter = logging.Formatter('%(asctime)s,%(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    scanned_cards_file_handler = RotatingFileHandler(
        log_dir / 'scanned_cards.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    scanned_cards_file_handler.setLevel(logging.INFO)
    scanned_cards_file_handler.setFormatter(scanned_cards_formatter)
    scanned_cards_logger.addHandler(scanned_cards_file_handler)

    # Write CSV header if file is new/empty
    scanned_cards_log_path = log_dir / 'scanned_cards.log'
    if not scanned_cards_log_path.exists() or scanned_cards_log_path.stat().st_size == 0:
        with open(scanned_cards_log_path, 'w') as f:
            f.write('# Scanned Cards Log - CSV Format\n')
            f.write('# Columns: Timestamp,Card Name,Collector Number,AI Model,DB Found,Added To Inventory,Processing Time\n')
            f.write('Timestamp,Card Name,Collector Number,AI Model,DB Found,Added To Inventory,Processing Time\n')

    # Log initialization
    app_logger.info("="*80)
    app_logger.info("Logging initialized - Multi-file configuration")
    app_logger.info(f"Log directory: {log_dir}")
    app_logger.info("Log files: app.log, ai.log, scanner.log, database.log, scanned_cards.log")
    app_logger.info("="*80)

    return app_logger

# Initialize logging
logger = setup_logging()

# Get scanned cards logger for tracking all scanned cards
scanned_cards_logger = logging.getLogger('scanned_cards')

from config import Config
from database import CardDatabase, search_key
from inventory import InventoryManager
from decks import DeckManager
from recommendations import Recommendations, Unavailable
from review import REVIEW_DIR, ReviewQueue
import games
from cleanup import cleanup_old_images, get_images_stats

# Import scanner
from scanner import CardScanner
from card_ocr import CardOcr

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = Config.SECRET_KEY

# Initialize SocketIO with proper configuration
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25
)

# Cards added by the scanner page, until "Add to collection" (same tables as the collection's)
SCAN_INVENTORY_FILE = Config.DATA_DIR / 'scan_inventory.db'

# Global instances
scanner = None
database = None
inventory = None           # the collection (table inventory in the card database file)
scan_inventory = None      # what the scanner page adds to, until it is moved to the collection
current_card_info = None
current_review_id = None  # review queue item open on the page
review_sid = None         # Socket.IO session of the page reviewing it (a reload / disconnect closes it)
review = None             # review.ReviewQueue
deck_store = None         # decks.DeckManager
recommend = None          # recommendations.Recommendations
# The capture being reviewed (image path): kept with the card added from it - also when the
# card is found by a manual search after the AI couldn't identify it
pending_capture = None
auto_capture_counter = 1
processing_queue_count = 0  # Track number of cards being processed by AI

# AI Processing Queue (for async Fast Scan Mode)
ai_processing_queue = queue.Queue()
ai_worker_thread = None
ai_worker_running = False


def log_to_client(message, level="info"):
    """Send log message to web client and log file"""
    timestamp = datetime.now().strftime('%H:%M:%S')

    # Log to file with appropriate level
    if level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    elif level == "success":
        logger.info(f"SUCCESS: {message}")
    elif level == "debug":
        logger.debug(message)
    else:
        logger.info(message)

    # Send to web client via SocketIO
    socketio.emit('log', {
        'timestamp': timestamp,
        'level': level,
        'message': message
    })


def get_ai_model_info():
    """Get current AI model info as a string for logging"""
    if scanner and hasattr(scanner, 'card_identifier') and scanner.card_identifier:
        provider = getattr(scanner.card_identifier, 'provider', 'unknown')
        model = getattr(scanner.card_identifier, 'model', 'unknown')
        return f"{provider}/{model}"
    return None


def log_scanned_card(card_name, collector_number, ai_model, db_found, added_to_inventory, processing_time=None):
    """
    Log scanned card to scanned_cards.log in CSV format
    Format: timestamp,card_name,collector_number,ai_model,db_found,added_to_inventory,processing_time
    """
    # Clean values for CSV (escape commas and quotes)
    card_name_clean = (card_name or '').replace(',', ';').replace('"', "'")
    collector_number_clean = (collector_number or '').replace(',', ';')
    ai_model_clean = (ai_model or 'unknown').replace(',', ';')
    db_found_str = 'YES' if db_found else 'NO'
    added_str = 'YES' if added_to_inventory else 'NO'
    time_str = f"{processing_time:.2f}s" if processing_time else 'N/A'

    # CSV format: card_name,collector_number,ai_model,db_found,added_to_inventory,processing_time
    log_entry = f'"{card_name_clean}","{collector_number_clean}","{ai_model_clean}",{db_found_str},{added_str},{time_str}'
    scanned_cards_logger.info(log_entry)


def set_pending_capture(image_path):
    global pending_capture
    pending_capture = str(image_path) if image_path else None


def queue_for_review(game, image_path, name='', number='', set_code='', foil='unknown', card=None, why=None):
    """A capture that wasn't added automatically goes to the review queue; scanning goes on"""
    review.add(game.id, image_path, name, number, set_code, foil, card)
    if card:
        what = f"{card['name']} ({why or 'printing not confirmed'})"
    else:
        what = f"'{name}' (not found)" if name else "a card the AI couldn't read"
    log_to_client(f"Queued for review: {what}", level="warning")
    # queued: a card was just added to the queue (the page plays the queue alert)
    socketio.emit('review_queue_update', {'count': review.count(game.id), 'queued': True}, namespace='/')
    # Nothing waits on the page for this card (normal mode blocks auto-capture until Add / Skip)
    if scanner:
        scanner.card_under_review = False


def route_identified(image_path, card_name, collector_number, set_code, processing_time=None, foil='unknown',
                     fast=False, reader=None):
    """
    After the AI (or OCR - reader says which read the card): look the card up and add it automatically, show it, or queue it for review.
    While a review is open, a card captured meanwhile (manual capture, auto scanning without
    automatic adds) waits in the queue instead of taking the reviewed card's place and capture.
    """
    reviewing = current_review_id is not None
    if not fast and not reviewing:
        set_pending_capture(image_path)
    if card_name and card_name.strip():
        search_and_emit_card(card_name, collector_number, processing_time, was_fast_scan_mode=fast,
                             set_code=set_code, image_path=image_path, foil=foil, reader=reader)
    elif fast or reviewing:
        queue_for_review(games.active(), image_path, foil=foil)


def added_payload(game, card, finish, quantity):
    """What an inventory_updated event says was added (the page shows it with an Undo)"""
    return {'name': card['name'], 'set': card['set'], 'number': card['number'],
            'finish': game.finishes[finish], 'quantity': quantity}


def scan_location():
    """Where cards being scanned are put (the inventory location new entries get; '' = none)"""
    return (scanner.settings.get('scan_location', '') if scanner else '') or ''


def add_automatically(game, card, image_path, foil):
    """
    A confirmed card goes straight into the inventory: one Near Mint copy in the likely finish.
    Done here rather than by the page, so it doesn't depend on a page being open (or running
    the current script - an old tab once sent every add without its card)
    """
    finish = game.suggested_finish(card, foil)
    row_id = scan_inventory.add_card(game.inventory_fields(card, finish), game.id, finish, 'Near Mint', 1,
                                     capture=image_path, location=scan_location())
    socketio.emit('inventory_updated', {'auto': True, 'stats': scan_inventory.get_stats(game.id),
                                        'added': added_payload(game, card, finish, 1)}, namespace='/')
    start_price_update(game, card, [row_id])


def search_and_emit_card(card_name, collector_number, processing_time=None, was_fast_scan_mode=False, set_code=None,
                         image_path=None, foil='unknown', reader=None):
    """
    Search for card in database and emit results to client

    Args:
        card_name: Card name from AI
        collector_number: Collector number from AI
        set_code: Set code from AI (e.g. "HOB")
        processing_time: AI processing time in seconds
        was_fast_scan_mode: Whether this was a fast scan auto-add
        reader: what read the card when it wasn't the vision AI ("light-ocr")

    Returns:
        tuple: (db_card_info, was_logged) - db result and whether it was logged
    """
    global current_card_info

    # Log search action
    if collector_number:
        log_to_client(f"Auto-searching database for: {card_name} #{collector_number}")
    else:
        log_to_client(f"Auto-searching database for: {card_name}")

    # Search database
    game = games.active()
    read_by = reader or get_ai_model_info()
    db_card_info = game.identify(card_name, collector_number, set_code, ai_model=read_by)

    # Adding automatically, only cards whose exact printing was confirmed are added
    # (Game.confirmed_matches, e.g. set + number); anything less certain goes to the review
    # queue and scanning goes on
    confirmed = game.is_confirmed(db_card_info)
    auto_add = was_fast_scan_mode and confirmed

    # Log scanned card
    log_scanned_card(
        card_name=card_name,
        collector_number=collector_number,
        ai_model=read_by,
        db_found=(db_card_info is not None),
        added_to_inventory=auto_add,
        processing_time=processing_time
    )

    if not auto_add and (was_fast_scan_mode or current_review_id is not None):
        queue_for_review(game, image_path, card_name, collector_number, set_code, foil, db_card_info,
                         why=None if was_fast_scan_mode else 'captured during a review')
    elif auto_add:
        # Added here, not through the current card: an open review keeps its card
        add_automatically(game, db_card_info, image_path, foil)
    elif db_card_info:
        # Waiting for Add / Skip anyway: show the current prices
        db_card_info = game.with_prices(db_card_info)
        if image_path:
            db_card_info['capture'] = str(image_path)
        current_card_info = db_card_info
        socketio.emit('card_found', {
            'card': game.card_payload(db_card_info),
            'auto_add': False
        }, namespace='/')
    else:
        # Try to find similar cards
        similar = game.similar(card_name, limit=5)
        if similar:
            socketio.emit('similar_cards', {'cards': similar}, namespace='/')
        else:
            socketio.emit('card_not_found', {'card_name': card_name}, namespace='/')

    return db_card_info, True


def ai_processing_worker():
    """
    Background worker thread that processes cards from the AI queue.
    This allows Fast Scan Mode to capture cards rapidly while AI processes them asynchronously.
    """
    global ai_worker_running, processing_queue_count, current_card_info, scanner

    logger.info("AI processing worker thread started")

    while ai_worker_running:
        try:
            # Get item from queue (blocks with timeout)
            try:
                item = ai_processing_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Unpack queue item
            card_number = item['card_number']
            card_image_rgb = item['card_image']
            image_path = item['image_path']
            was_fast_scan_mode = item['fast_scan_mode']

            logger.info(f"AI worker processing card #{card_number} (queue size: {ai_processing_queue.qsize()})")

            # Run AI identification (this is the slow part - 13-36 seconds)
            start_time = time.time()
            card_info = scanner.identify_card_from_image(card_image_rgb, item['foil_image'])
            processing_time = time.time() - start_time

            # Extract card info
            card_name = ""
            collector_number = ""
            set_code = ""
            foil_status = 'unknown'
            if card_info and isinstance(card_info, dict):
                card_name = card_info.get('name', '')
                collector_number = card_info.get('collector_number', '')
                set_code = card_info.get('set_code', '')
                foil_status = card_info.get('foil', 'unknown')
                if card_info.get('processing_time') is None:
                    card_info['processing_time'] = processing_time

            # Emit card captured event
            socketio.emit('card_captured', {
                'image_path': str(image_path),
                'card_name': card_name,
                'collector_number': collector_number,
                'set_code': set_code,
                'card_number': card_number,
                'processing_time': processing_time,
                'foil': foil_status
            }, namespace='/')

            route_identified(image_path, card_name, collector_number, set_code, processing_time, foil_status,
                             fast=was_fast_scan_mode, reader=(card_info or {}).get('reader'))

            # In Fast Scan Mode, scanner is already ready for next capture
            # In Normal Mode, card awaits user review
            if was_fast_scan_mode:
                logger.info(f"Fast Scan Mode: Card #{card_number} AI processing completed in {processing_time:.1f}s")
            else:
                logger.info(f"Normal Auto-Scan: Card #{card_number} awaiting review - next capture blocked until user adds/dismisses")

            # Decrement queue counter
            processing_queue_count -= 1
            socketio.emit('processing_queue_update', {
                'queue_count': processing_queue_count
            }, namespace='/')

            # Mark task as done
            ai_processing_queue.task_done()

        except Exception as e:
            logger.exception(f"Error in AI processing worker: {e}")
            log_to_client(f"AI processing error: {e}", level="error")
            processing_queue_count = max(0, processing_queue_count - 1)
            ai_processing_queue.task_done()

    logger.info("AI processing worker thread stopped")


def initialize_components():
    """Initialize all components"""
    global scanner, database, inventory, scan_inventory, review, deck_store, recommend

    logger.info("Initializing components...")

    # Create directories
    Config.create_directories()

    # Initialize database
    logger.info("Initializing database...")
    database = CardDatabase()

    # Initialize scanner (CPU-based YOLOv8)
    logger.info("Initializing scanner...")
    scanner = CardScanner(log_callback=log_to_client)
    set_auto_add(scanner.settings.get('auto_add', True))  # remembered in data/settings.json
    log_to_client("Scanner initialized with YOLOv8 detection", level="info")

    # Card games (Magic, ...) - each wraps its card data; the saved one is scanned
    games.init(database, scanner.settings, log_callback=log_to_client)

    # Note: get_ai_model_info() and log_scanned_card() are defined at module level
    # so they can be accessed by both Flask routes and the AI worker thread

    # Initialize inventory manager
    logger.info("Initializing inventory...")
    inventory = InventoryManager(log_callback=log_to_client)
    # Scanned cards wait in their own file: clearing them never touches the collection
    scan_inventory = InventoryManager(db_file=SCAN_INVENTORY_FILE, log_callback=log_to_client)
    review = ReviewQueue()
    deck_store = DeckManager()
    recommend = Recommendations()

    # Set up auto-capture callback
    def handle_auto_capture():
        """Handle auto-capture event - triggers card identification"""
        global auto_capture_counter, current_card_info, processing_queue_count
        logger.info(f"Auto-capture triggered #{auto_capture_counter}")

        def announce_capture(taken=True):
            """
            The capture beep - the signal to drop the next card: sent once the image is taken
            and a focus probe started by this capture (~1 s, every refocus_every cards) is done
            """
            if scanner.capture_pending and not taken:
                scanner.capture_pending = False
            elif scanner.capture_pending:
                deadline = time.time() + 2.5
                while scanner.focus_probe_running and time.time() < deadline:
                    time.sleep(0.05)
                scanner.capture_pending = False
                socketio.emit('auto_capture_triggered', {
                    'counter': current_capture_number,
                    'message': f'Auto-capture #{current_capture_number}'
                }, namespace='/')

        current_capture_number = auto_capture_counter
        auto_capture_counter += 1

        # Execute capture logic directly
        try:
            # Capture fast scan mode state at time of capture (not current state)
            was_fast_scan_mode = scanner.fast_scan_mode if scanner and hasattr(scanner, 'fast_scan_mode') else False

            # Increment queue counter (AI processing starting)
            processing_queue_count += 1
            socketio.emit('processing_queue_update', {
                'queue_count': processing_queue_count
            }, namespace='/')

            is_detected = scanner.is_card_detected()
            if not is_detected:
                logger.warning("Auto-capture triggered but no card detected")
                scanner.capture_pending = False
                processing_queue_count -= 1
                socketio.emit('processing_queue_update', {
                    'queue_count': processing_queue_count
                }, namespace='/')
                return

            # ========================================================================
            # FAST SCAN MODE: Async capture + queue AI processing for rapid scanning
            # ========================================================================
            if was_fast_scan_mode:
                # Capture image ONLY (no AI processing) - fast!
                image_path, card_image_rgb, foil_image = scanner.capture_card_image_only(current_capture_number, settle=0)
                announce_capture(taken=bool(image_path))

                if not image_path:
                    logger.error("Fast Scan: Failed to capture image")
                    processing_queue_count -= 1
                    socketio.emit('processing_queue_update', {
                        'queue_count': processing_queue_count
                    }, namespace='/')
                    return

                # Queue the image for AI processing in background worker
                ai_processing_queue.put({
                    'card_number': current_capture_number,
                    'card_image': card_image_rgb,
                    'image_path': image_path,
                    'foil_image': foil_image,
                    'fast_scan_mode': True
                })

                # Immediately clear the review flag to allow next capture after cooldown
                scanner.card_under_review = False

                logger.info(f"Fast Scan Mode: Card #{current_capture_number} captured and queued for AI (queue: {ai_processing_queue.qsize()}) - ready for next capture in {Config.AUTO_CAPTURE_DELAY}s")

            # ========================================================================
            # NORMAL MODE: Synchronous capture + AI (original behavior)
            # ========================================================================
            else:
                # Synchronous capture with AI processing (blocks until AI completes)
                image_path, card_image_rgb, foil_image = scanner.capture_card_image_only(current_capture_number, settle=0)
                announce_capture(taken=bool(image_path))
                vision_ai_result = scanner.identify_card_from_image(card_image_rgb, foil_image) if image_path else None

                if not image_path:
                    logger.error("Normal Mode: Failed to capture image")
                    processing_queue_count -= 1
                    socketio.emit('processing_queue_update', {
                        'queue_count': processing_queue_count
                    }, namespace='/')
                    return

                # Extract card name from vision AI result
                card_name = ""
                collector_number = ""
                set_code = ""
                processing_time = None
                foil_status = 'unknown'
                if vision_ai_result and isinstance(vision_ai_result, dict):
                    card_name = vision_ai_result.get('name', '')
                    collector_number = vision_ai_result.get('collector_number', '')
                    set_code = vision_ai_result.get('set_code', '')
                    processing_time = vision_ai_result.get('processing_time')
                    foil_status = vision_ai_result.get('foil', 'unknown')

                # Emit card captured event
                socketio.emit('card_captured', {
                    'image_path': str(image_path),
                    'card_name': card_name,
                    'collector_number': collector_number,
                    'set_code': set_code,
                    'card_number': current_capture_number,
                    'processing_time': processing_time,
                    'foil': foil_status
                }, namespace='/')

                route_identified(image_path, card_name, collector_number, set_code, processing_time, foil_status,
                                 reader=(vision_ai_result or {}).get('reader'))

                # Normal mode: card awaits user review (card_under_review stays True)
                logger.info(f"Normal Auto-Scan: Card #{current_capture_number} awaiting review - next capture blocked until user adds/dismisses")

                # Decrement queue counter (AI processing completed synchronously)
                processing_queue_count -= 1
                socketio.emit('processing_queue_update', {
                    'queue_count': processing_queue_count
                }, namespace='/')

        except Exception as e:
            logger.exception(f"Error in auto-capture: {e}")
            log_to_client(f"Auto-capture error: {e}", level="error")
            # Clear flags on error to prevent getting stuck (in both modes)
            if scanner:
                scanner.card_under_review = False
                scanner.capture_pending = False
            # Decrement queue counter on error too
            processing_queue_count -= 1
            socketio.emit('processing_queue_update', {
                'queue_count': processing_queue_count
            }, namespace='/')

    scanner.auto_capture_callback = handle_auto_capture
    logger.info("Auto-capture callback registered")

    # Start AI processing worker thread
    global ai_worker_thread, ai_worker_running
    ai_worker_running = True
    ai_worker_thread = threading.Thread(target=ai_processing_worker, daemon=True, name="AI-Worker")
    ai_worker_thread.start()
    logger.info("AI processing worker thread started")

    logger.info("All components initialized successfully!")
    log_to_client("All components initialized successfully", level="success")


# ============================================================================
# Flask Routes
# ============================================================================

@app.route('/')
def index():
    """Main page"""
    return render_template('scanner.html')


@app.route('/collection')
def collection():
    """Inventory management and deck building"""
    return render_template('collection.html')


def generate_frames():
    """MJPEG stream of the annotated live view (each new frame once; encoding is shared)"""
    last_id = -1
    while True:
        frame_id, jpeg = scanner.get_stream_jpeg() if scanner else (-1, None)
        if jpeg is None or frame_id == last_id:
            time.sleep(0.01)
            continue
        last_id = frame_id
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n'


@app.route('/captures/<path:name>')
def capture_thumbnail(name):
    """Thumbnail of a capture kept with an inventory entry"""
    from inventory import CAPTURES_DIR
    return send_from_directory(CAPTURES_DIR, name, max_age=86400)


@app.route('/video_feed')
def video_feed():
    """Video streaming route"""
    return Response(
        generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/api/stats')
def get_stats():
    """Get database and inventory statistics"""
    if database and inventory:
        game = games.active()
        return jsonify({
            'database': {'total_cards': game.card_count(), 'update': data_update_notices.get(game.id),
                         'updating': game.id in data_updates_running},
            'review': review.count(game.id) if review else 0,
            # The scanner page's counters: what was scanned and not moved to the collection yet
            'inventory': scan_inventory.get_stats(game.id),
            'collection': inventory.get_stats(game.id)
        })

    return jsonify({'error': 'Components not initialized'}), 500


def inventory_area():
    """The inventory a request is about: the scanner's (?area=scan) or the collection"""
    return scan_inventory if request.args.get('area') == 'scan' else inventory


@app.route('/api/scan_inventory/to_collection', methods=['POST'])
def scan_to_collection():
    """Move every scanned card of the active game into the collection (merging with what is there)"""
    game = games.active()
    moved = inventory.take_from(scan_inventory, game.id)
    socketio.emit('inventory_updated', {'auto': False, 'stats': scan_inventory.get_stats(game.id)}, namespace='/')
    return jsonify({'success': True, **moved, 'stats': scan_inventory.get_stats(game.id)})


@app.route('/api/inventory')
def get_inventory():
    """Get full inventory list"""
    if inventory:
        try:
            game = games.active()
            cards = inventory_area().get_all_cards(game.id)
            # What the collection page shows and filters with beyond the stored columns
            details = game.card_details([card['card_id'] for card in cards])
            # The decks that use each card (by name; not for the scanned cards)
            in_decks = deck_store.needed_by_name(game.id, commander_formats(game)) \
                if game.deck_formats and inventory_area() is inventory else {}
            for card in cards:
                card['details'] = details.get(card['card_id'], {})
                card['decks'] = [used['deck'] for used in in_decks.get(search_key(card['name']), [])]

            return jsonify({
                'success': True,
                'cards': cards,
                'total': len(cards)
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'error': 'Inventory not initialized'}), 500


@app.route('/api/inventory/delete/<int:row_id>', methods=['DELETE', 'POST'])
def delete_inventory_card(row_id):
    """Delete an inventory entry by its id"""
    if inventory:
        success = inventory_area().delete_card(row_id)

        if success:
            return jsonify({
                'success': True,
                'message': f'Card deleted from inventory'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to delete card'}), 400

    return jsonify({'error': 'Inventory not initialized'}), 500


@app.route('/api/inventory/update/<int:row_id>', methods=['PUT', 'POST'])
def update_inventory_card(row_id):
    """Update an inventory entry by its id (quantity, condition, finish)"""
    if inventory:
        try:
            # Get data from request
            data = request.get_json() if request.is_json else {}
            quantity = data.get('quantity')
            condition = data.get('condition')
            finish = data.get('finish')
            split_quantity = data.get('split_quantity')
            location = data.get('location')
            tags = data.get('tags')
            if finish is not None and finish not in games.active().finishes:
                return jsonify({'success': False, 'split': False, 'error': f'Unknown finish: {finish}'}), 400

            # A new finish takes the printing's price in that finish (foil / holo / reverse)
            finish_price = None
            entry = inventory_area().get_entry(row_id) if finish else None
            if entry and finish != entry['finish'] and entry['card_id']:
                game = games.get(entry['game']) or games.active()
                card = game.get_card(entry['card_id'])
                if card:
                    finish_price = game.inventory_fields(card, finish)['price']

            # Changing the finish of several copies splits the entry
            result = inventory_area().update_card(row_id, quantity=quantity, condition=condition,
                                           finish=finish, split_quantity=split_quantity,
                                           finish_price=finish_price,
                                           location=str(location)[:60] if location is not None else None,
                                           tags=tags)

            if result['success']:
                return jsonify(result)
            else:
                return jsonify(result), 400

        except Exception as e:
            logger.error(f"Error updating card: {e}")
            return jsonify({'success': False, 'split': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'split': False, 'error': 'Inventory not initialized'}), 500


@app.route('/api/inventory/bulk', methods=['POST'])
def bulk_update_inventory():
    """One change to several entries (JSON: ids, action, value) - see InventoryManager.bulk_update"""
    if not inventory:
        return jsonify({'success': False, 'error': 'Inventory not initialized'}), 500
    data = request.get_json(silent=True) or {}
    try:
        ids = [int(row_id) for row_id in data.get('ids') or []]
        changed = inventory_area().bulk_update(ids, data.get('action'), str(data.get('value') or '')[:60])
    except (ValueError, TypeError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'changed': changed})


@app.route('/api/inventory/remove_batch', methods=['POST'])
def remove_inventory_batch():
    """Take back the cards that were added at one time (JSON: added_at, as in the entries)"""
    added_at = str((request.get_json(silent=True) or {}).get('added_at') or '')
    if not added_at:
        return jsonify({'success': False, 'error': 'added_at is missing'}), 400
    return jsonify({'success': True, **inventory_area().remove_batch(games.active().id, added_at)})


@app.route('/api/export_inventory/<fmt>')
def export_inventory(fmt):
    """Download the active game's inventory in one of its export formats (Game.export_formats)"""
    if not inventory:
        return jsonify({'error': 'Inventory not initialized'}), 500
    game = games.active()
    formats = game.export_formats()
    if fmt not in formats:
        return jsonify({'error': f'Unknown export format: {fmt}'}), 404
    try:
        _label, prefix, writer = formats[fmt]
        export_path = inventory_area().export(game.id, writer, prefix)
        return send_file(str(export_path), mimetype='text/csv', as_attachment=True,
                         download_name=export_path.name)
    except Exception as e:
        logger.exception(f"Export failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/import_inventory', methods=['POST'])
def import_inventory():
    """Import inventory from CSV file upload"""
    if not inventory:
        return jsonify({'error': 'Inventory not initialized'}), 500

    # Check if file was uploaded
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    # Check if filename is empty
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Check file extension
    if not file.filename.endswith('.csv'):
        return jsonify({'error': 'Only CSV files are supported'}), 400

    try:
        # Get replace_existing flag from form data (default: False)
        replace_existing = request.form.get('replace_existing', 'false').lower() == 'true'

        # Save uploaded file temporarily
        upload_dir = Config.DATA_DIR / 'uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        temp_file_path = upload_dir / f'import_{timestamp}_{file.filename}'

        file.save(str(temp_file_path))
        logger.info(f"CSV file uploaded: {temp_file_path}")

        # Import the CSV
        game = games.active()
        stats = inventory_area().import_csv(temp_file_path, game.id, list(game.finishes),
                                     replace_existing=replace_existing)

        # Clean up temporary file
        temp_file_path.unlink()

        if stats['success']:
            inv_stats = inventory_area().get_stats(game.id)

            return jsonify({
                'success': True,
                'stats': stats,
                'inventory_stats': inv_stats,
                'message': f"Import complete: {stats['added']} added, {stats['updated']} updated"
            })
        else:
            return jsonify({
                'success': False,
                'error': stats.get('error', 'Import failed')
            }), 400

    except Exception as e:
        logger.exception(f"Import failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/clear_inventory', methods=['POST', 'DELETE'])
def clear_inventory():
    """Clear all cards from inventory"""
    if not inventory:
        return jsonify({'error': 'Inventory not initialized'}), 500

    try:
        result = inventory_area().clear_inventory(games.active().id)

        if result['success']:
            return jsonify({
                'success': True,
                'deleted': result['deleted'],
                'message': f"Inventory cleared: {result['deleted']} entries removed"
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Failed to clear inventory')
            }), 400

    except Exception as e:
        logger.exception(f"Clear inventory failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Decks (collection page) - lists of cards; what is owned comes from the inventory
# ============================================================================

def commander_formats(game):
    return [key for key, rules in game.deck_formats.items() if rules.get('commander')]


def deck_format_for(game, site_format, entries):
    """Deck format for a deck from another site, whose format name may be unknown here"""
    if site_format in game.deck_formats:
        return site_format
    if any(entry['board'] == 'commander' for entry in entries):
        return commander_formats(game)[0]
    return 'legacy' if 'legacy' in game.deck_formats else next(iter(game.deck_formats))


def resolve_entries(game, entries, deck_format):
    """
    Entries from a pasted list or another site, as stored in a deck: the card data's spelling
    of each name and a printing to show. Returns (entries, names not in the card data).
    """
    has_commander = deck_format in commander_formats(game)
    by_name = game.cards_by_names([entry['name'] for entry in entries])
    # The printing the source names (a precon's own cards, a Moxfield deck) is the one shown
    printings = game.card_details([entry.get('scryfall_id') for entry in entries])
    resolved, unknown = [], []
    for entry in entries:
        board = entry.get('board') or 'main'
        if board == 'maybe':  # cards the other site lists outside the deck
            if not has_commander:
                continue
            board = 'side'
        if board == 'commander' and not has_commander:
            board = 'main'
        card = by_name.get(search_key(entry['name']))
        if not card:
            unknown.append(entry['name'])
        printing = entry.get('scryfall_id') if card and entry.get('scryfall_id') in printings else None
        resolved.append({'name': card['name'] if card else entry['name'],
                         'card_id': printing or (card['id'] if card else None),
                         'quantity': entry.get('quantity') or 1, 'board': board})
    return resolved, unknown


def deck_payload(deck):
    """A deck as the page shows it: card data, copies owned, other decks wanting them, issues, totals"""
    game = games.get(deck['game'])
    considering = deck['format'] in commander_formats(game)  # its 'side' board isn't played
    by_name = game.cards_by_names([card['name'] for card in deck['cards']])
    owned = inventory.owned_by_name(game.id)
    needed = deck_store.needed_by_name(game.id, commander_formats(game))
    # The image is the printing the deck entry was added with (a precon's own printing, the
    # search result clicked); the rest is the card's data, the same for every printing
    printings = game.card_details([entry['card_id'] for entry in deck['cards']])
    cards, checked = [], []
    for entry in deck['cards']:
        key = search_key(entry['name'])
        card = by_name.get(key)
        payload = game.deck_card_payload(card) if card else None
        printing = printings.get(entry['card_id']) or {}
        if payload and printing.get('image_uri'):
            payload['image_uri'] = printing['image_uri']
        cards.append({
            'name': entry['name'], 'quantity': entry['quantity'], 'board': entry['board'],
            'card': payload,
            # The printing shown (the picker changes it)
            'printing': {'id': entry['card_id'], 'set_code': printing.get('set_code') or '',
                         'number': printing.get('number') or ''},
            'owned': owned.get(key, 0),
            'elsewhere': [other for other in needed.get(key, []) if other['deck_id'] != deck['id']],
        })
        checked.append({'name': entry['name'], 'quantity': entry['quantity'], 'board': entry['board'], 'card': card})
    played = [card for card in cards if not (considering and card['board'] == 'side')]
    wanted = {}
    for card in played:
        wanted[card['name']] = wanted.get(card['name'], 0) + card['quantity']
    price = lambda card: (card['card'] or {}).get('price') or 0.0
    first = {card['name']: card for card in played}
    missing = {name: max(0, count - first[name]['owned']) for name, count in wanted.items()}
    return {
        'id': deck['id'], 'name': deck['name'], 'format': deck['format'], 'notes': deck['notes'],
        'updated_at': deck['updated_at'], 'cards': cards,
        'issues': game.check_deck(deck['format'], checked),
        'totals': {
            'cards': sum(card['quantity'] for card in played if card['board'] != 'side'),
            'side': sum(card['quantity'] for card in cards if card['board'] == 'side'),
            'price': round(sum(price(first[name]) * count for name, count in wanted.items()), 2),
            'missing': sum(missing.values()),
            'missing_price': round(sum(price(first[name]) * count for name, count in missing.items()), 2),
        },
    }


def get_deck_or_404(deck_id):
    deck = deck_store.get(deck_id) if deck_store else None
    if not deck:
        return None, (jsonify({'success': False, 'error': 'Deck not found'}), 404)
    return deck, None


def entries_from_source(game, data, deck_format):
    """
    Cards for a deck from what the page sent: 'text' (a pasted list), 'url' (Moxfield /
    Archidekt), 'precon' (MTGJSON file name) or 'average' (EDHREC's average deck for these
    commander names). Returns (entries or None, name from the source, its format name).
    """
    if data.get('text'):
        return game.parse_decklist(data['text'], deck_format), None, None
    if data.get('url'):
        found = recommend.deck_from_url(data['url'])
        return found['entries'], found['name'], found['format']
    if data.get('precon'):
        entries = recommend.precon(data['precon'])
        if entries is None:
            raise Unavailable('MTGJSON does not have this deck')
        return entries, None, None
    if data.get('average'):
        entries = recommend.average_deck(data['average'])
        if entries is None:
            raise Unavailable('EDHREC has no average deck for this commander')
        return entries, None, None
    return None, None, None


@app.route('/api/decks', methods=['GET', 'POST'])
def decks_collection():
    """List the active game's decks, or create one (JSON: name, format, and optionally a source
    of cards - see entries_from_source - or 'commander': a card name to start with)"""
    game = games.active()
    if not game.deck_formats:
        return jsonify({'success': False, 'error': f'No deck builder for {game.label}'}), 400
    if request.method == 'GET':
        owned = inventory.owned_by_name(game.id)
        considering = commander_formats(game)
        listed = []
        for deck in deck_store.list_decks(game.id):
            wanted = {}
            for card in deck_store.get(deck['id'])['cards']:
                if not (card['board'] == 'side' and deck['format'] in considering):
                    wanted[card['name']] = wanted.get(card['name'], 0) + card['quantity']
            have = sum(min(count, owned.get(search_key(name), 0)) for name, count in wanted.items())
            total = sum(wanted.values())
            listed.append({**deck, 'owned': have, 'owned_percent': round(100 * have / total) if total else 0})
        return jsonify({'success': True, 'decks': listed, 'has_deck_data': bool(game.has_deck_data())})

    data = request.get_json(silent=True) or {}
    deck_format = data.get('format')
    try:
        entries, source_name, source_format = entries_from_source(game, data, deck_format)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if entries is not None and deck_format not in game.deck_formats:
        deck_format = deck_format_for(game, source_format, entries)
    if deck_format not in game.deck_formats:
        return jsonify({'success': False, 'error': 'Choose a format'}), 400
    if entries is None and data.get('commander'):
        entries = [{'name': data['commander'], 'quantity': 1, 'board': 'commander'}]
    unknown = []
    deck_id = deck_store.create(game.id, str(data.get('name') or source_name or 'New deck')[:80], deck_format)
    if entries:
        resolved, unknown = resolve_entries(game, entries, deck_format)
        deck_store.import_cards(deck_id, resolved)
    logger.info(f"Deck created: {deck_id} ({deck_format})")
    return jsonify({'success': True, 'deck': deck_payload(deck_store.get(deck_id)), 'unknown': unknown})


@app.route('/api/decks/<int:deck_id>', methods=['GET', 'PUT', 'DELETE'])
def deck_item(deck_id):
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    if request.method == 'DELETE':
        deck_store.delete(deck_id)
        return jsonify({'success': True})
    if request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        deck_format = data.get('format')
        if deck_format is not None and deck_format not in games.get(deck['game']).deck_formats:
            return jsonify({'success': False, 'error': f'Unknown format: {deck_format}'}), 400
        name = str(data['name'])[:80] if data.get('name') is not None else None
        notes = str(data['notes'])[:4000] if data.get('notes') is not None else None
        deck_store.update(deck_id, name=name, deck_format=deck_format, notes=notes)
        deck = deck_store.get(deck_id)
    return jsonify({'success': True, 'deck': deck_payload(deck)})


@app.route('/api/decks/<int:deck_id>/duplicate', methods=['POST'])
def deck_duplicate(deck_id):
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    return jsonify({'success': True, 'deck': deck_payload(deck_store.get(deck_store.duplicate(deck_id)))})


@app.route('/api/decks/<int:deck_id>/cards', methods=['POST'])
def deck_cards(deck_id):
    """
    Change a deck's cards. JSON: 'cards': [{name, board, card_id, and one of 'change' (+1 / -1),
    'quantity' (set; 0 removes), 'move_to' (another board) or 'printing' (the printing id to
    show the entry in)}]. Returns the deck.
    """
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        for item in data.get('cards') or []:
            name, board = str(item['name']), item.get('board') or 'main'
            if item.get('printing'):
                if not games.get(deck['game']).card_details([item['printing']]):
                    raise ValueError('Unknown printing')
                deck_store.set_printing(deck_id, name, board, item['printing'])
            elif item.get('move_to'):
                deck_store.move_card(deck_id, name, board, item['move_to'])
            elif 'quantity' in item:
                deck_store.set_card(deck_id, name, board, int(item['quantity']), item.get('card_id'))
            else:
                deck_store.add_card(deck_id, name, board, int(item.get('change', 1)), item.get('card_id'))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'deck': deck_payload(deck_store.get(deck_id))})


@app.route('/api/decks/<int:deck_id>/import', methods=['POST'])
def deck_import(deck_id):
    """Add cards from a source (see entries_from_source) to a deck; 'replace': true empties it first"""
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    game = games.get(deck['game'])
    data = request.get_json(silent=True) or {}
    try:
        entries, _name, _format = entries_from_source(game, data, deck['format'])
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if not entries:
        return jsonify({'success': False, 'error': 'No cards found'}), 400
    resolved, unknown = resolve_entries(game, entries, deck['format'])
    added = deck_store.import_cards(deck_id, resolved, replace=bool(data.get('replace')))
    return jsonify({'success': True, 'added': added, 'unknown': unknown,
                    'deck': deck_payload(deck_store.get(deck_id))})


@app.route('/api/decks/<int:deck_id>/export/<kind>')
def deck_export(deck_id, kind):
    """Download a deck as a text list ('text'), or only the copies not owned ('buylist')"""
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    game = games.get(deck['game'])
    entries = deck['cards']
    if kind == 'buylist':
        owned = inventory.owned_by_name(game.id)
        wanted = {}
        for entry in entries:
            if not (entry['board'] == 'side' and deck['format'] in commander_formats(game)):
                wanted[entry['name']] = wanted.get(entry['name'], 0) + entry['quantity']
        lines = [f"{count - owned.get(search_key(name), 0)} {name}" for name, count in sorted(wanted.items())
                 if count > owned.get(search_key(name), 0)]
        text = '\n'.join(lines) + '\n'
    elif kind == 'text':
        text = game.format_decklist(entries, deck['format'])
    else:
        return jsonify({'success': False, 'error': f'Unknown export: {kind}'}), 404
    file_name = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in deck['name']).strip() or 'deck'
    return Response(text, mimetype='text/plain', headers={
        'Content-Disposition': f'attachment; filename="{file_name}{"_buylist" if kind == "buylist" else ""}.txt"'})


@app.route('/api/cards/search')
def search_cards():
    """Deck builder card search: one result per card name, with the copies owned"""
    game = games.active()
    if not game.deck_formats:
        return jsonify({'success': False, 'error': f'No deck builder for {game.label}'}), 400
    args = request.args
    owned = inventory.owned_by_name(game.id)
    # free=1: leave out the cards other decks use (deck_id: the deck being built, which doesn't count)
    taken = None
    if args.get('free'):
        current = int(args['deck_id']) if (args.get('deck_id') or '').isdigit() else None
        taken = [key for key, used in deck_store.needed_by_name(game.id, commander_formats(game)).items()
                 if any(deck['deck_id'] != current for deck in used)]
    try:
        cmc = float(args['cmc']) if args.get('cmc') not in (None, '') else None
        offset = max(0, int(args.get('offset') or 0))
        cards, more = game.search_cards(
            text=args.get('q'), type_text=args.get('type'), oracle_text=args.get('text'),
            identity=args.get('identity'), colors=args.get('colors'), cmc=cmc, rarity=args.get('rarity'),
            legal_in=args.get('format') or None, names=list(owned) if args.get('owned') else None,
            exclude_names=taken, offset=offset)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'more': more, 'cards': [
        {**game.deck_card_payload(card), 'owned': owned.get(search_key(card['name']), 0)} for card in cards]})


@app.route('/api/cards/printings')
def card_printings():
    """Every printing of a card (name), newest first, with the copies of each that are owned"""
    game = games.active()
    if not game.deck_formats:
        return jsonify({'success': False, 'error': f'No deck builder for {game.label}'}), 400
    name = request.args.get('name') or ''
    owned = inventory.owned_by_printing(game.id, name)
    return jsonify({'success': True, 'printings': [
        {**printing, 'owned': owned.get(printing['id'], 0)} for printing in game.printings(name)]})


# -- Recommendations (recommendations.py: EDHREC, MTGJSON, Archidekt, Moxfield) --

@app.route('/api/decks/<int:deck_id>/suggestions')
def deck_suggestions(deck_id):
    """Cards played with the deck's commander (EDHREC), with the copies owned; the deck's own cards left out"""
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    game = games.get(deck['game'])
    commanders = [card['name'] for card in deck['cards'] if card['board'] == 'commander']
    if not commanders:
        return jsonify({'success': True, 'categories': [], 'message': 'Choose a commander to get suggestions'})
    try:
        found = recommend.commander_cards(commanders)
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if not found:
        return jsonify({'success': True, 'categories': [], 'message': 'EDHREC has no data for this commander'})
    in_deck = {search_key(card['name']) for card in deck['cards']}
    owned = inventory.owned_by_name(game.id)
    elsewhere = {key for key, used in deck_store.needed_by_name(game.id, commander_formats(game)).items()
                 if any(other['deck_id'] != deck['id'] for other in used)}
    by_name = game.cards_by_names([card['name'] for category in found['categories'] for card in category['cards']])
    categories = []
    for category in found['categories']:
        cards = []
        for suggestion in category['cards']:
            card = by_name.get(search_key(suggestion['name']))
            if not card or search_key(card['name']) in in_deck:
                continue
            cards.append({**game.deck_card_payload(card), 'inclusion': suggestion['inclusion'],
                          'synergy': suggestion['synergy'], 'owned': owned.get(search_key(card['name']), 0),
                          'elsewhere': search_key(card['name']) in elsewhere})
        if cards:
            categories.append({'title': category['title'], 'cards': cards})
    return jsonify({'success': True, 'decks': found['decks'], 'url': found['url'], 'categories': categories})


@app.route('/api/decks/popular')
def popular_decks():
    """Public decks on other sites: with a deck's commander (deck_id), or of a format"""
    game = games.active()
    commander, deck_format = None, request.args.get('format')
    if request.args.get('deck_id'):
        deck, error = get_deck_or_404(int(request.args['deck_id']))
        if error:
            return error
        deck_format = deck['format']
        commander = next((card['name'] for card in deck['cards'] if card['board'] == 'commander'), None)
    if deck_format not in game.deck_formats:
        return jsonify({'success': False, 'error': 'Unknown format'}), 400
    found, problems = recommend.popular_decks(commander=commander, deck_format=deck_format)
    return jsonify({'success': True, 'decks': found, 'problems': problems, 'commander': commander})


@app.route('/api/precons')
def precon_list():
    """Preconstructed decks (MTGJSON), newest first - to browse, open as a deck, or add as owned"""
    try:
        return jsonify({'success': True, 'precons': recommend.precon_list()})
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502


@app.route('/api/precons/<file_name>/own', methods=['POST'])
def precon_own(file_name):
    """
    "I own this precon": its cards go into the inventory - the printings and finishes that are
    in the box, Near Mint, at 'location' - and it becomes a deck to change from there.
    JSON: name (of the deck), location.
    """
    game = games.active()
    data = request.get_json(silent=True) or {}
    try:
        precon = next((deck for deck in recommend.precon_list() if deck['file'] == file_name), None)
        entries = recommend.precon(file_name) if precon else None
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if not entries:
        return jsonify({'success': False, 'error': 'MTGJSON does not have this deck'}), 404
    name = str(data.get('name') or precon['name'])[:80]
    location = str(data.get('location') or '').strip()[:60]

    # The exact printing: by its Scryfall id, else set + number, else any printing of the name
    by_id = database.cards_by_ids([entry['scryfall_id'] for entry in entries])
    by_name = game.cards_by_names([entry['name'] for entry in entries])
    deck_entries, unknown, added = [], [], 0
    for entry in entries:
        card = by_id.get(entry['scryfall_id']) \
            or (database.get_card_by_set_number(entry['set'], entry['number']) if entry['set'] and entry['number'] else None) \
            or by_name.get(search_key(entry['name']))
        if not card:
            unknown.append(entry['name'])
            continue
        finish = game.suggested_finish(card, 'foil' if entry['foil'] else 'non-foil')
        inventory.add_card(game.inventory_fields(card, finish), game.id, finish, 'Near Mint', entry['quantity'],
                           location=location, quiet=True)
        added += entry['quantity']
        deck_entries.append({'name': card['name'], 'card_id': card['id'], 'quantity': entry['quantity'],
                             'board': entry['board']})
    inventory.last_added = None  # Undo takes back one add, not a whole deck
    deck_id = deck_store.create(game.id, name, precon['format'])
    deck_store.import_cards(deck_id, deck_entries)
    log_to_client(f"Added to inventory: {added} cards of {precon['name']}"
                  + (f" ({location})" if location else ''), level="success")
    # (the event's stats are the scanner page's counters: the scanned cards)
    socketio.emit('inventory_updated', {'auto': False, 'stats': scan_inventory.get_stats(game.id)}, namespace='/')
    return jsonify({'success': True, 'added': added, 'unknown': unknown,
                    'deck': deck_payload(deck_store.get(deck_id))})


# "What can I build?": rankings that need a request per commander / precon, so they run in
# the background and the page asks for their state. kind -> state
deck_ideas = {}
deck_ideas_lock = threading.Lock()


def run_deck_ideas(kind, game):
    state = deck_ideas[kind]
    try:
        owned = inventory.owned_by_name(game.id)
        if kind == 'commanders':
            # Legendary creatures in the inventory, by how much of what is played with them is owned
            legends = sorted({row['name'] for row in inventory.get_all_cards(game.id)
                              if 'Legendary' in row['type_line'] and 'Creature' in row['type_line']})
            cards = game.cards_by_names(legends)
            legends = [name for name in legends if cards.get(search_key(name))
                       and game.can_be_commander(cards[search_key(name)])]
            state['total'] = len(legends)
            for name in legends:
                found = recommend.commander_cards([name])
                if found:
                    played = {card['name']: card['inclusion'] for category in found['categories']
                              for card in category['cards']}
                    have = [card for card in played if owned.get(search_key(card))]
                    weight = sum(played.values())
                    card = cards[search_key(name)]
                    state['items'].append({
                        'name': card['name'], 'image_uri': card['image_uri'], 'identity': card['color_identity'],
                        'decks': found['decks'], 'owned': len(have), 'total': len(played),
                        'fit': round(100 * sum(played[card_name] for card_name in have) / weight) if weight else 0,
                    })
                state['done'] += 1
        else:
            precons = recommend.precon_list()
            state['total'] = len(precons)
            for precon in precons:
                entries = recommend.precon(precon['file'])
                if entries:
                    total = sum(entry['quantity'] for entry in entries)
                    have = sum(min(entry['quantity'], owned.get(search_key(entry['name']), 0)) for entry in entries)
                    state['items'].append({**precon, 'owned': have, 'total': total,
                                           'percent': round(100 * have / total) if total else 0})
                state['done'] += 1
    except Unavailable as e:
        state['error'] = str(e)
    except Exception as e:
        logger.exception(f"Deck ideas ({kind}) failed: {e}")
        state['error'] = 'Something went wrong - see data/logs/app.log'
    finally:
        state['running'] = False


@app.route('/api/decks/ideas/<kind>', methods=['GET', 'POST'])
def deck_ideas_state(kind):
    """
    "What can I build?" rankings: kind 'commanders' (owned legendary creatures, by the share of
    their EDHREC cards that is owned) or 'precons' (preconstructed decks, by the share owned).
    POST starts a run (answers are cached, so a second run is quick); GET returns its state.
    """
    game = games.active()
    if kind not in ('commanders', 'precons') or not game.deck_formats:
        return jsonify({'success': False, 'error': 'Unknown ranking'}), 404
    with deck_ideas_lock:
        state = deck_ideas.get(kind)
        if request.method == 'POST' and not (state and state['running']):
            state = deck_ideas[kind] = {'running': True, 'done': 0, 'total': 0, 'items': [], 'error': None}
            threading.Thread(target=run_deck_ideas, args=(kind, game), daemon=True).start()
    if not state:
        return jsonify({'success': True, 'started': False, 'running': False, 'items': []})
    order = 'fit' if kind == 'commanders' else 'percent'
    return jsonify({'success': True, 'started': True, 'running': state['running'], 'done': state['done'],
                    'total': state['total'], 'error': state['error'],
                    'items': sorted(list(state['items']), key=lambda item: item[order], reverse=True)})



@app.route('/api/detection_status')
def get_detection_status():
    """Get current card detection status with detailed state information"""
    global scanner

    if scanner:
        status = scanner.get_detection_status()
        return jsonify(status)

    return jsonify({
        'detected': False,
        'stable_frames': 0,
        'required_frames': 5,
        'is_stable': False,
        'focus_locked': False
    })


@app.route('/api/ai_provider')
def get_ai_provider():
    """Get current AI provider and model for card identification"""
    global scanner

    if scanner and scanner.card_identifier:
        return jsonify({
            'provider': scanner.card_identifier.provider,
            'model': scanner.card_identifier.model,
            'enabled': True
        })

    return jsonify({
        'provider': Config.VISION_AI_PROVIDER,
        'model': None,
        'enabled': Config.VISION_AI_ENABLED
    })


@app.route('/api/ai_credentials')
def get_ai_credentials():
    """Per provider: whether an API key is set (masked, never the full key) / the local endpoint"""
    return jsonify(credential_status())


@app.route('/api/ai_models')
def get_ai_models():
    """Get available models for all providers"""
    from card_identifier import CardIdentifier
    return jsonify({
        'models': CardIdentifier.AVAILABLE_MODELS
    })


@app.route('/api/ai_models/<provider>')
def get_provider_models(provider):
    """Get available models for a specific provider"""
    from card_identifier import CardIdentifier
    provider = provider.lower()

    if provider in CardIdentifier.AVAILABLE_MODELS:
        return jsonify({
            'provider': provider,
            'models': CardIdentifier.AVAILABLE_MODELS[provider]
        })
    else:
        return jsonify({
            'error': f'Unknown provider: {provider}'
        }), 404


@app.route('/api/local_ai_models')
def get_local_ai_models():
    """Fetch live models from local Ollama server"""
    import requests

    ollama_base = Config.LOCAL_AI_ENDPOINT.replace('/v1/chat/completions', '')
    try:
        ollama_api = f"{ollama_base}/api/tags"

        logger.info(f"Fetching models from Ollama: {ollama_api}")

        # Query Ollama for available models
        response = requests.get(ollama_api, timeout=5)
        response.raise_for_status()

        data = response.json()

        # Extract model names from Ollama response
        # Ollama returns: {"models": [{"name": "llava:7b", ...}, ...]}
        models = []
        if 'models' in data:
            for model in data['models']:
                if 'name' in model:
                    models.append(model['name'])

        logger.info(f"Found {len(models)} local models: {models}")

        return jsonify({
            'success': True,
            'models': models,
            'endpoint': ollama_base
        })

    except requests.exceptions.ConnectionError:
        logger.warning(f"Could not connect to local AI server at {ollama_base}")
        return jsonify({
            'success': False,
            'error': 'Could not connect to local AI server',
            'models': []
        })
    except requests.exceptions.Timeout:
        logger.warning("Local AI server request timed out")
        return jsonify({
            'success': False,
            'error': 'Request timed out',
            'models': []
        })
    except Exception as e:
        logger.error(f"Error fetching local models: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'models': []
        })


# ============================================================================
# SocketIO Events
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info("Client connected to SocketIO")
    emit('log', {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'level': 'info',
        'message': 'Connected to card scanner'
    })


@socketio.on('capture_card')
def handle_capture(data):
    """Handle card capture request"""
    global scanner, current_card_info

    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return

    card_number = data.get('card_number', 1)

    try:
        # Manual capture works regardless of detection state
        # If no card detected, captures full frame
        image_path, vision_ai_result = scanner.capture_card_image(card_number)

        if not image_path:
            emit('error', {'message': 'Failed to capture image'})
            return

        # Extract card name from vision AI result (if identified)
        card_name = ""
        collector_number = ""
        set_code = ""
        processing_time = None
        foil_status = 'unknown'
        if vision_ai_result and isinstance(vision_ai_result, dict):
            card_name = vision_ai_result.get('name', '')
            collector_number = vision_ai_result.get('collector_number', '')
            set_code = vision_ai_result.get('set_code', '')
            processing_time = vision_ai_result.get('processing_time')
            foil_status = vision_ai_result.get('foil', 'unknown')

        result = {
            'image_path': str(image_path),
            'card_name': card_name,
            'collector_number': collector_number,
            'set_code': set_code,
            'card_number': card_number,
            'processing_time': processing_time,
            'foil': foil_status
        }
        emit('card_captured', result)

        route_identified(image_path, card_name, collector_number, set_code, processing_time, foil_status,
                         reader=(vision_ai_result or {}).get('reader'))

    except Exception as e:
        logger.exception(f"Exception in handle_capture: {e}")
        log_to_client(f"Capture error: {e}", level="error")
        emit('error', {'message': str(e)})


@socketio.on('search_card')
def handle_search(data):
    """Handle manual card search - lists printings so the user can pick the exact one"""
    global current_card_info

    logger.info(f"Search card request received: {data}")

    if not database:
        emit('error', {'message': 'Card database not initialized'})
        return
    game = games.active()

    card_name = (data.get('card_name') or '').strip()
    collector_number = (data.get('collector_number') or '').strip() or None
    set_code = (data.get('set_code') or '').strip() or None
    treatment = (data.get('treatment') or '').strip() or None

    # Without a name: set code + number, or a number with the set total ("199/165", Pokémon)
    if not card_name and not (collector_number and (set_code or '/' in collector_number)):
        emit('error', {'message': 'Enter a card name, or the set code and number'})
        return

    try:
        resolved_name, printings = game.find_printings(card_name, collector_number, treatment, set_code)

        if len(printings) == 1:
            current_card_info = printings[0]
            emit('card_found', {'card': game.card_payload(current_card_info)})
        elif printings:
            # Several printings - let the user pick the one in hand
            current_card_info = None
            emit('card_printings', {
                'name': resolved_name,
                'treatment': treatment,
                'cards': [game.card_payload(card) for card in printings]
            })
        elif resolved_name:
            # Card exists, but no printing has the requested treatment
            emit('card_not_found', {
                'card_name': resolved_name,
                'message': f'No printings of "{resolved_name}" match the selected treatment.'
            })
        else:
            logger.info(f"No match for '{card_name}', searching for similar cards...")
            similar = game.similar(card_name, limit=5) if card_name else []
            if similar:
                emit('similar_cards', {'cards': similar})
            else:
                emit('card_not_found', {'card_name': card_name or f"{(set_code or '').upper()} #{collector_number}".strip()})

    except Exception as e:
        logger.exception(f"Exception in handle_search: {e}")
        log_to_client(f"Search error: {e}", level="error")
        emit('error', {'message': str(e)})


@socketio.on('select_printing')
def handle_select_printing(data):
    """User picked a specific printing from the printing list"""
    global current_card_info

    game = games.active()
    card = game.get_card(data.get('id')) if database else None
    if not card:
        emit('error', {'message': 'Printing not found'})
        return

    current_card_info = card
    logger.info(f"Printing selected: {card['name']} ({card['set']} #{card['number']})")
    emit('card_found', {'card': game.card_payload(card)})


@socketio.on('add_to_inventory')
def handle_add_inventory(data):
    """Handle add to inventory request"""
    global inventory, current_card_info, scanner, current_review_id

    card = current_card_info
    if not scan_inventory or not card:
        logger.warning("Add to inventory requested but no card selected")
        emit('error', {'message': 'No card selected'})
        return

    try:
        game = games.active()
        condition = data.get('condition', 'Near Mint')
        # Several finishes of the card come in one event ("items") - separate events would
        # be handled in parallel threads, in any order
        items = data.get('items') or [{'finish': data.get('finish'), 'quantity': data.get('quantity', 1)}]
        for item in items:
            if (item.get('finish') or game.default_finish) not in game.finishes:
                emit('error', {'message': f"Unknown finish: {item.get('finish')}"})
                return

        # The card's own capture, else the one under review; it goes with the first finish added
        capture = card.pop('capture', None) or pending_capture
        if capture == pending_capture:
            set_pending_capture(None)
        added_rows = []

        for item in items:
            finish = item.get('finish') or game.default_finish
            try:
                quantity = max(1, int(item.get('quantity', 1)))
            except (ValueError, TypeError):
                quantity = 1
            logger.info(f"Adding card to inventory: {quantity}x {card['name']} ({condition}, {finish})")
            added_rows.append(scan_inventory.add_card(game.inventory_fields(card, finish), game.id, finish,
                                                      condition, quantity, capture=capture,
                                                      location=scan_location()))
            capture = None

            # Send updated stats and what was added (the page offers an Undo)
            emit('inventory_updated', {'auto': False, 'stats': scan_inventory.get_stats(game.id),
                                       'added': added_payload(game, card, finish, quantity)})

        logger.info("Card added to inventory successfully")
        start_price_update(game, card, added_rows)
        current_card_info = None
        if current_review_id is not None:
            # Reviewed: resolve the item and open the next one
            review.remove(current_review_id)
            current_review_id = None
            emit('review_queue_update', {'count': review.count(game.id)}, broadcast=True)
            handle_review_open()

        # Clear the review flag to allow next auto-capture
        if scanner:
            scanner.card_under_review = False
            logger.info("Card review completed - auto-capture re-enabled")

    except Exception as e:
        logger.exception(f"Exception in handle_add_inventory: {e}")
        log_to_client(f"Add to inventory error: {e}", level="error")
        emit('error', {'message': str(e)})
        # Clear flag even on error to prevent getting stuck
        if scanner:
            scanner.card_under_review = False


def start_price_update(game, card, row_ids):
    """Update the prices of entries just added, in the background (games that fetch prices)"""
    if game.fetches_prices:
        threading.Thread(target=update_added_prices, args=(game, card, row_ids), daemon=True, name='prices').start()


def update_added_prices(game, card, row_ids):
    """
    After an add: fetch the card's current prices (games that fetch them per card) and update
    the entries - scanning never waits for the price request
    """
    try:
        card = game.with_prices(dict(card))
        changed = False
        for row_id in row_ids:
            entry = scan_inventory.get_entry(row_id)
            if not entry or entry['card_id'] != card['id']:
                continue  # deleted or merged meanwhile
            price = game.inventory_fields(card, entry['finish'])['price']
            if price != entry['price_usd']:
                scan_inventory.set_price(row_id, price)
                changed = True
        if changed:
            socketio.emit('inventory_prices_updated', {'stats': scan_inventory.get_stats(game.id)}, namespace='/')
    except Exception as e:
        logger.warning(f"Price update for {card.get('name')} failed: {e}")


@socketio.on('review_open')
def handle_review_open(data=None):
    """Open the oldest item of the review queue (or say it is empty)"""
    global current_card_info, current_review_id, review_sid
    game = games.active()
    item, total = review.first(game.id)
    current_review_id = item['id'] if item else None
    review_sid = request.sid if item else None
    if not item:
        current_card_info = None
        set_pending_capture(None)
        emit('review_item', {'id': None, 'total': 0})
        return
    card = game.get_card(item['card_id']) if item['card_id'] else None
    if card:
        card['match'] = item['match']
    current_card_info = card
    set_pending_capture(review.image_path(item))  # goes with whatever card is added for this item
    emit('review_item', {
        'id': item['id'],
        'total': total,
        'image_url': f"/review_images/{item['file']}" if item['file'] else None,
        'ai': {'name': item['ai_name'], 'number': item['ai_number'], 'set': item['ai_set'], 'foil': item['foil']},
        'captured_at': item['created_at'],
        'card': game.card_payload(card) if card else None,
    })


@socketio.on('review_skip')
def handle_review_skip(data=None):
    """Drop the open review item without adding anything"""
    global current_card_info, current_review_id, review_sid
    if current_review_id is not None:
        review.remove(current_review_id)
    current_review_id = current_card_info = review_sid = None
    set_pending_capture(None)
    emit('review_queue_update', {'count': review.count(games.active_id())}, broadcast=True)
    handle_review_open()  # the next one


@socketio.on('review_close')
def handle_review_close(data=None):
    """Leave the review; the open item stays in the queue"""
    global current_card_info, current_review_id, review_sid
    current_review_id = current_card_info = review_sid = None
    set_pending_capture(None)


@socketio.on('disconnect')
def handle_disconnect(*_args):
    """The reviewing page went away (reload, closed, asleep): the review is closed, so later
    captures aren't sent to the queue and an Add can't resolve the item unseen (the page
    opens it again when it reconnects)"""
    if review_sid is not None and request.sid == review_sid:
        logger.info("Reviewing page disconnected - review closed")
        handle_review_close()


@app.route('/review_images/<path:name>')
def review_image(name):
    """Capture of a review queue item"""
    return send_from_directory(REVIEW_DIR, name)


@socketio.on('undo_last_add')
def handle_undo_last_add():
    """Take back the most recent add to the inventory"""
    if not inventory:
        emit('error', {'message': 'Inventory not initialized'})
        return

    undone = scan_inventory.undo_last_add()
    if undone:
        emit('inventory_undone', {'name': undone, 'stats': scan_inventory.get_stats(games.active().id)})
    else:
        emit('error', {'message': 'Nothing to undo'})


@socketio.on('dismiss_card')
def handle_dismiss_card(data=None):
    """Handle card dismissal - user cancels current card review"""
    global scanner, current_card_info

    logger.info("Card dismissed by user")
    current_card_info = None
    # "Not found" dismisses itself to let auto scanning go on; the capture stays for a
    # manual search. Skip drops it
    if not (data or {}).get('keep_capture'):
        set_pending_capture(None)

    # Clear the review flag to allow next auto-capture
    if scanner:
        scanner.card_under_review = False
        logger.info("Card review dismissed - auto-capture re-enabled")

    emit('card_dismissed', {'message': 'Card dismissed'})


@socketio.on('toggle_detection')
def handle_toggle_detection(data):
    """Toggle card detection on/off"""
    global scanner

    if not scanner:
        logger.error("Toggle detection requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = data.get('enabled', True)
    scanner.set_detection_enabled(enabled)
    emit('detection_toggled', {'enabled': enabled})
    logger.info(f"Detection toggled: {enabled}")
    log_to_client(f"Card detection {('enabled' if enabled else 'disabled')}", level="info")


def game_info(game):
    """What the page needs to know about a game (lists keep their order; jsonify sorts dicts)"""
    return {
        'id': game.id,
        'label': game.label,
        'source': game.source,
        'has_treatments': game.has_treatments,
        'set_example': game.set_example,
        'number_example': game.number_example,
        'finishes': [[key, label] for key, label in game.finishes.items()],
        'exports': [[key, label] for key, (label, _prefix, _writer) in game.export_formats().items()],
        # Deck builder formats: [[id, label, has a commander]] (none: the game has no deck builder)
        'deck_formats': [[key, rules['label'], bool(rules.get('commander'))]
                         for key, rules in game.deck_formats.items()],
    }


@app.route('/api/games')
def get_games():
    """Supported card games and the one being scanned"""
    return jsonify({'active': games.active_id(), 'games': [game_info(game) for game in games.all_games()]})


@socketio.on('set_game')
def handle_set_game(data):
    """Switch the game being scanned (stops auto scanning; the current card is dropped)"""
    global current_card_info, current_review_id, review_sid
    game_id = data.get('game')
    try:
        games.set_active(game_id)
    except ValueError as e:
        emit('error', {'message': str(e)})
        return
    current_card_info = current_review_id = review_sid = None
    set_pending_capture(None)
    if scanner and scanner.auto_capture_enabled:
        scanner.auto_capture_enabled = False
        scanner.card_under_review = False
        socketio.emit('auto_capture_toggled', {'enabled': False})
    game = games.active()
    logger.info(f"Game switched to {game.label}")
    card_count = game.card_count()
    socketio.emit('game_changed', {**game_info(game), 'card_count': card_count})
    if not card_count:
        # First time: fetch its card data right away
        log_to_client(f"Downloading the {game.label} card data from {game.source}...")
        start_card_data_update(game)


@socketio.on('toggle_auto_capture')
def handle_toggle_auto_capture(data):
    """Toggle auto-capture on/off"""
    global scanner

    if not scanner:
        logger.error("Toggle auto-capture requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = data.get('enabled', True)
    scanner.auto_capture_enabled = enabled

    # Load a local AI model now, so the first auto-captured card doesn't wait for it
    if enabled and scanner.card_identifier:
        scanner.card_identifier.warm_up()

    # When disabling, also clear the card_under_review flag to reset state
    if not enabled:
        scanner.card_under_review = False
        scanner.stable_frames = 0  # Reset stability counter
        logger.info("Auto-capture disabled - resetting scanner state")

    emit('auto_capture_toggled', {'enabled': enabled})
    logger.info(f"Auto-capture toggled: {enabled}")
    log_to_client(f"Auto-capture {('enabled' if enabled else 'disabled')}", level="info")


def set_auto_add(enabled):
    """
    Auto-add ("fast scan") mode: auto-captured cards are identified in the background and
    confirmed ones are added to the inventory without review; otherwise each card waits
    for Add / Skip.
    """
    scanner.fast_scan_mode = enabled
    scanner.required_stable_frames = Config.FAST_SCAN_STABILITY_FRAMES if enabled else Config.AUTO_CAPTURE_STABILITY_FRAMES
    scanner.auto_capture_delay = Config.AUTO_CAPTURE_DELAY


@app.route('/api/scan_settings')
def get_scan_settings():
    """Scanning preferences the page needs on load"""
    return jsonify({
        'auto_add': bool(scanner.fast_scan_mode) if scanner else True,
        'ocr_first': bool(scanner.ocr_enabled) if scanner else True,
        'ocr_installed': CardOcr.installed(),
        'autofocus': scanner.focus_locked_value is None if scanner else True,
        'fixed_area_enabled': bool(scanner.fixed_area_enabled) if scanner else False,
        'fixed_area': scanner.fixed_area if scanner else None,
        'camera_rotation': scanner.rotation if scanner else 0,
        'scan_location': scan_location(),
        'locations': inventory.locations(games.active_id()) if inventory else [],
    })


@app.route('/api/scan_location', methods=['POST'])
def set_scan_location():
    """Set the inventory location scanned cards are added to ('' = none)"""
    location = str((request.get_json(silent=True) or {}).get('location') or '').strip()[:60]
    scanner.settings.set('scan_location', location)
    log_to_client(f"Scanned cards go to: {location}" if location else "Scanned cards get no location")
    return jsonify({'success': True, 'scan_location': location})


@socketio.on('toggle_fast_scan')
def handle_toggle_fast_scan(data):
    """Toggle adding auto-scanned cards to the inventory automatically (remembered)"""
    global scanner

    if not scanner:
        logger.error("Toggle auto-add requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = bool(data.get('enabled', False))
    set_auto_add(enabled)
    scanner.settings.set('auto_add', enabled)
    emit('fast_scan_toggled', {'enabled': enabled})
    logger.info(f"Auto-add toggled: {enabled} (required frames: {scanner.required_stable_frames})")
    log_to_client(f"Add cards automatically: {'on' if enabled else 'off - review each card'}", level="info")


@socketio.on('toggle_ocr')
def handle_toggle_ocr(data):
    """Read cards with light-ocr first, the vision AI only when needed (remembered)"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    scanner.set_ocr_enabled(bool(data.get('enabled', False)))
    emit('ocr_toggled', {'enabled': scanner.ocr_enabled, 'installed': CardOcr.installed()})


@socketio.on('toggle_anti_glare')
def handle_toggle_anti_glare(data):
    """Toggle anti-glare preprocessing on/off"""
    global scanner

    if not scanner:
        logger.error("Toggle anti-glare requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = data.get('enabled', False)
    scanner.set_anti_glare_enabled(enabled)
    emit('anti_glare_toggled', {'enabled': enabled})
    logger.info(f"Anti-glare toggled: {enabled}")
    # Log message sent by scanner.set_anti_glare_enabled()


@socketio.on('toggle_debug_trace')
def handle_toggle_debug_trace(data):
    """Toggle debug trace logging on/off"""
    global scanner

    if not scanner:
        logger.error("Toggle debug trace requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = data.get('enabled', False)
    scanner.debug_trace_enabled = enabled
    emit('debug_trace_toggled', {'enabled': enabled})
    logger.info(f"Debug trace toggled: {enabled}")


@socketio.on('reset_focus')
def handle_reset_focus():
    """Reset camera focus"""
    global scanner

    if not scanner:
        logger.error("Reset focus requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    logger.info("Focus reset requested by user")
    if scanner.reset_focus():
        # The sweep runs in the background; the result arrives as a log message
        emit('focus_reset', {'message': 'Focusing...'})
    elif scanner.focus_sweep_running:
        emit('focus_reset', {'message': 'Already focusing...'})
    else:
        emit('error', {'message': 'This camera has no manual focus control'})


@socketio.on('set_autofocus')
def handle_set_autofocus(data):
    """Continuous autofocus on, or off = find the sharpest focus and lock it"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    if not scanner.set_continuous_autofocus(bool(data.get('enabled'))):
        emit('error', {'message': 'This camera has no manual focus control'})


@socketio.on('set_camera_rotation')
def handle_set_camera_rotation(data):
    """Rotate the camera image (0/90/180/270 degrees clockwise) - e.g. a camera mounted sideways"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    try:
        fixed_area_off = scanner.set_rotation(data.get('rotation', 0))
    except (TypeError, ValueError) as e:
        emit('error', {'message': str(e)})
        return
    socketio.emit('camera_rotation_updated', {'rotation': scanner.rotation, 'fixed_area_off': fixed_area_off})
    if fixed_area_off:
        socketio.emit('fixed_area_updated', {'enabled': False, 'area': scanner.fixed_area})


@socketio.on('set_fixed_area')
def handle_set_fixed_area(data):
    """
    Fixed capture area (sleeved cards): {'enabled': bool} turns it on/off, {'area': [x1, y1, x2, y2]}
    (fractions of the frame, drawn on the video) sets it, {'use_detected': true} takes the
    detected card plus a margin. Sent back to every page as fixed_area_updated.
    """
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    area = data.get('area')
    if data.get('use_detected'):
        area = scanner.detected_area()
        if area is None:
            emit('error', {'message': 'No card detected - put a card in the box, or draw the area'})
            return
    if area is not None:
        try:
            x1, y1, x2, y2 = (float(v) for v in area)
        except (TypeError, ValueError):
            emit('error', {'message': 'Invalid area'})
            return
        x1, y1, x2, y2 = max(0.0, x1), max(0.0, y1), min(1.0, x2), min(1.0, y2)
        if x2 - x1 < 0.05 or y2 - y1 < 0.05:
            emit('error', {'message': 'The area is too small - draw it around the card'})
            return
        area = [x1, y1, x2, y2]
    enabled = data.get('enabled')
    if enabled and area is None and scanner.fixed_area is None:
        emit('error', {'message': 'Draw the capture area first'})
        return
    state = scanner.set_fixed_area(enabled=enabled, area=area)
    logger.info(f"Fixed area: {'on' if state['enabled'] else 'off'} {state['area']}")
    socketio.emit('fixed_area_updated', state)


@socketio.on('set_ai_provider')
def handle_set_ai_provider(data):
    """Set AI provider and/or model for card identification"""
    global scanner

    if not scanner:
        logger.error("Set AI provider requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    provider = data.get('provider', 'gemini')
    model = data.get('model', None)  # Optional model parameter

    if model:
        logger.info(f"AI provider/model change requested: {provider} / {model}")
    else:
        logger.info(f"AI provider change requested: {provider}")

    # Call the scanner's set_ai_provider method
    result = scanner.set_ai_provider(provider, model)

    if result['success']:
        emit('ai_provider_set', {
            'provider': provider,
            'model': result.get('model'),
            'message': result['message']
        })
        logger.info(f"AI provider set to: {provider} ({result.get('model')})")
    else:
        emit('error', {'message': result['message']})
        logger.error(f"Failed to set AI provider: {result['message']}")


@socketio.on('save_ai_credential')
def handle_save_ai_credential(data):
    """Save a provider's API key (or the local endpoint) entered in Settings"""
    provider = (data.get('provider') or '').lower()
    try:
        save_credential(provider, data.get('value', ''))
    except (ValueError, OSError) as e:
        emit('error', {'message': f'Could not save: {e}'})
        return
    logger.info(f"Credential for {provider} {'saved' if data.get('value') else 'removed'} from the web interface")
    emit('ai_credential_saved', {'provider': provider, 'status': credential_status()[provider]})


def active_ai():
    """(provider, model) the scanner identifies cards with - model is None without an identifier"""
    if scanner and scanner.card_identifier:
        return scanner.card_identifier.provider, scanner.card_identifier.model
    return Config.VISION_AI_PROVIDER, None


def prompts_payload():
    provider, model = active_ai()
    status = prompts.status(provider, model, games.active_id())
    # 'order' keeps the editor's tabs in order (jsonify sorts the keys)
    return {'provider': provider, 'model': model, 'prompts': status, 'order': list(status)}


@app.route('/api/prompts')
def get_prompts():
    """Prompt instructions in effect for the active AI model, and where they come from"""
    return jsonify(prompts_payload())


@socketio.on('save_prompt')
def handle_save_prompt(data):
    """Save edited prompt instructions for the active model (scope 'model') or all models"""
    provider, model = active_ai()
    kind = data.get('kind')
    for_model = data.get('scope') == 'model'
    if for_model and not model:
        emit('error', {'message': 'No AI model is active - save the prompt for all models'})
        return
    try:
        prompts.save(kind, data.get('text'), provider if for_model else None, model if for_model else None,
                     games.active_id())
    except ValueError as e:
        emit('error', {'message': str(e)})
        return
    target = f'{provider} / {model}' if for_model else 'all models'
    emit('prompts_updated', {**prompts_payload(), 'message': f'Prompt saved for {target}'})


@socketio.on('reset_prompt')
def handle_reset_prompt(data):
    """Remove the saved prompt in effect (this model's, else the all-models one)"""
    provider, model = active_ai()
    try:
        removed = prompts.reset(data.get('kind'), provider, model, games.active_id())
    except ValueError as e:
        emit('error', {'message': str(e)})
        return
    message = {'model': f'Prompt for {provider} / {model} removed',
               'all': 'Prompt for all models removed'}.get(removed, 'Already using the built-in prompt')
    emit('prompts_updated', {**prompts_payload(), 'message': message})


@socketio.on('test_prompt')
def handle_test_prompt(data):
    """
    Run the AI on the last captured card with the prompt text from the editor (not saved)
    and report the raw answer and how it would be matched.
    """
    kind = data.get('kind')
    text = (data.get('text') or '').strip()
    sid = request.sid

    def reply(**result):
        socketio.emit('prompt_test_result', {'kind': kind, **result}, to=sid)

    if not scanner or not scanner.card_identifier:
        reply(error='Vision AI is not configured')
        return
    if not scanner.last_capture:
        reply(error='No card captured yet - capture a card first')
        return
    if not text:
        reply(error='The prompt is empty')
        return
    image, foil_image = scanner.last_capture
    if kind == 'foil' and foil_image is None:
        reply(error='The last capture has no detected card outline, so the foil corner cannot be located')
        return

    def run():
        identifier = scanner.card_identifier
        start = time.time()
        try:
            if kind == 'foil':
                raw, foil = identifier.read_foil_symbol_verbose(foil_image, text)
                reply(raw=raw, result={'foil': foil}, seconds=round(time.time() - start, 2))
                return
            raw, card = identifier.identify_card_verbose(image, text)
            seconds = round(time.time() - start, 2)
            match = None
            if card:
                game = games.active()
                found = game.identify(card['name'], card.get('collector_number'), card.get('set_code'))
                if found:
                    match = {'name': found['name'], 'set': (found.get('set_code') or '').upper(),
                             'set_name': found.get('set'), 'number': found.get('number'), 'match': found.get('match'),
                             'confirmed': game.is_confirmed(found)}
                card = {k: card.get(k, '') for k in ('name', 'collector_number', 'set_code')}
            reply(raw=raw, result=card, match=match, seconds=seconds)
        except Exception as e:
            logger.exception(f"Prompt test failed: {e}")
            reply(error=str(e))

    socketio.start_background_task(run)


# Newer card data found by the update check: game id -> message (shown on the page)
data_update_notices = {}
data_updates_running = set()


def start_card_data_update(game):
    """Download a game's card data in the background (progress/complete/error events)"""
    if game.id in data_updates_running:
        return False
    data_updates_running.add(game.id)

    def progress_callback(message):
        socketio.emit('database_update_progress', {'message': message})
        logger.info(f"Database update: {message}")

    def update_task():
        try:
            logger.info(f"Updating {game.label} card data from {game.source}")
            game.download(progress_callback)
            total = game.card_count()
            data_update_notices.pop(game.id, None)
            socketio.emit('database_update_complete', {'game': game.id, 'total_cards': total})
            logger.info(f"{game.label} card data updated: {total} cards")
        except Exception as e:
            logger.exception(f"Database update failed: {e}")
            socketio.emit('database_update_error', {'game': game.id, 'message': str(e)})
        finally:
            data_updates_running.discard(game.id)

    threading.Thread(target=update_task, daemon=True).start()
    return True


def check_card_data_updates():
    """Ask each game's source whether newer card data exists (games with data only)"""
    for game in games.all_games():
        try:
            if not game.card_count() or game.id in data_updates_running:
                continue
            message = game.check_for_update()
        except Exception as e:
            logger.warning(f"{game.label} update check failed: {e}")
            continue
        if message:
            data_update_notices[game.id] = message
            logger.info(f"{game.label} card data update available: {message}")
            socketio.emit('database_update_available', {'game': game.id, 'label': game.label, 'message': message})
        else:
            data_update_notices.pop(game.id, None)


def run_update_checks():
    """Check for newer card data shortly after startup, then once a day"""
    def loop():
        time.sleep(10)
        while True:
            check_card_data_updates()
            time.sleep(24 * 3600)
    threading.Thread(target=loop, daemon=True, name='update-check').start()


@socketio.on('update_database')
def handle_update_database():
    """Download the active game's card data again"""
    logger.info("Database update request received")
    if not database:
        logger.error("Database update requested but database not initialized")
        emit('error', {'message': 'Database not initialized'})
        return
    if not start_card_data_update(games.active()):
        emit('error', {'message': 'An update of this card data is already running'})


@socketio.on('rebuild_database')
def handle_rebuild_database():
    """Handle database schema rebuild request"""
    global database

    logger.info("Database rebuild request received")

    if not database:
        logger.error("Database rebuild requested but database not initialized")
        emit('error', {'message': 'Database not initialized'})
        return

    def progress_callback(message):
        """Send progress updates to the client"""
        socketio.emit('database_rebuild_progress', {'message': message})
        logger.info(f"Database rebuild: {message}")

    def rebuild_task():
        """Run database rebuild in background thread"""
        try:
            logger.info("Starting database rebuild in background thread")

            # Rebuild the database schema
            result = database.rebuild_database_schema(progress_callback)

            if result['success']:
                # Send completion event
                socketio.emit('database_rebuild_complete', {
                    'cards_imported': result['cards_imported'],
                    'schema_type': result['schema_type']
                })

                logger.info(f"Database rebuild complete: {result['cards_imported']} cards, schema type: {result['schema_type']}")
            else:
                socketio.emit('database_rebuild_error', {
                    'message': result.get('error', 'Unknown error')
                })
                logger.error(f"Database rebuild failed: {result.get('error')}")

        except Exception as e:
            logger.exception(f"Database rebuild failed: {e}")
            socketio.emit('database_rebuild_error', {
                'message': str(e)
            })

    # Run rebuild in background thread
    rebuild_thread = threading.Thread(target=rebuild_task, daemon=True)
    rebuild_thread.start()

    logger.info("Database rebuild thread started")


# ============================================================================
# Main
# ============================================================================

def run_cleanup_background():
    """Run cleanup in a background thread"""
    if not Config.CLEANUP_ENABLED:
        logger.info("Background cleanup disabled in configuration")
        return

    def cleanup_task():
        try:
            # Wait a few seconds after startup before cleaning
            time.sleep(5)

            logger.info(f"Starting background cleanup of scanned images (keeping last {Config.CLEANUP_DAYS} days)...")

            # Get stats before cleanup
            stats_before = get_images_stats()
            if stats_before['count'] > 0:
                logger.info(f"Images before cleanup: {stats_before['count']} files, {stats_before['total_size_mb']:.2f} MB, oldest: {stats_before['oldest_days']:.1f} days")
            else:
                logger.info("No images found to clean up")
                return

            # Clean up images older than configured days
            files_deleted, space_freed = cleanup_old_images(days_to_keep=Config.CLEANUP_DAYS)

            if files_deleted > 0:
                logger.info(f"Cleanup complete: Deleted {files_deleted} old images, freed {space_freed:.2f} MB")
            else:
                logger.info("Cleanup complete: No old images to remove")

        except Exception as e:
            logger.error(f"Error during background cleanup: {e}")

    # Start cleanup in background thread
    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()
    logger.info("Background cleanup thread started")


def main():
    """Start the web server"""
    # Use print for important startup messages that should always be visible
    print("="*60)
    print("Card Scanner Web Interface")
    print("="*60)

    # Check if database exists
    if not Config.DATABASE_FILE.exists() or Config.DATABASE_FILE.stat().st_size == 0:
        print("\n⚠ WARNING: No card database found!")
        print("Please run 'python3 setup_database.py' first")
        print("to download the card database.")
        logger.warning("Card database not found - cannot start application")
        return

    # Initialize components
    logger.info("Starting scanner initialization...")
    initialize_components()

    # Start background cleanup
    run_cleanup_background()
    run_update_checks()

    game = games.active()
    logger.info(f"Scanning {game.label}: {game.card_count():,} cards in the database")
    print(f"✓ {game.label}: {game.card_count():,} cards")

    # Check Vision AI status
    if scanner.card_identifier:
        ai = f"{scanner.card_identifier.provider} / {scanner.card_identifier.model}"
        logger.info(f"Vision AI enabled using {ai}")
        print(f"✓ Vision AI enabled ({ai})")
    else:
        logger.warning("Vision AI disabled - no API key configured")
        print(f"⚠ Vision AI disabled")
        print("  To enable: enter an API key in Settings -> Vision AI (or pick a local model)")

    print("\n" + "="*60)
    print("Web Interface Starting...")
    print("="*60)
    print(f"\n✓ Access the scanner at:")
    print(f"  • Local:   http://localhost:{Config.PORT}")
    print(f"  • Network: http://<your-pi-ip>:{Config.PORT}")
    print(f"\n✓ Logs are being written to: data/logs/app.log")
    print("\nPress Ctrl+C to stop")
    print("="*60 + "\n")

    logger.info(f"Starting Flask server on {Config.HOST}:{Config.PORT}")

    try:
        # Start Flask app with SocketIO
        socketio.run(
            app,
            host=Config.HOST,
            port=Config.PORT,
            debug=Config.DEBUG,
            allow_unsafe_werkzeug=True  # Allow development server for local use
        )
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        logger.info("Shutdown requested by user")
    finally:
        if scanner:
            scanner.cleanup()
        if database:
            database.close()
        logger.info("Scanner stopped successfully")
        print("Scanner stopped.")


if __name__ == "__main__":
    main()
