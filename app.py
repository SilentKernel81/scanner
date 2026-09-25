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
from database import CardDatabase
from inventory import InventoryManager
import games
from cleanup import cleanup_old_images, get_images_stats

# Import scanner
from scanner import CardScanner

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

# Global instances
scanner = None
database = None
inventory = None
current_card_info = None
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


def search_and_emit_card(card_name, collector_number, processing_time=None, was_fast_scan_mode=False, set_code=None,
                         image_path=None):
    """
    Search for card in database and emit results to client

    Args:
        card_name: Card name from AI
        collector_number: Collector number from AI
        set_code: Set code from AI (e.g. "HOB")
        processing_time: AI processing time in seconds
        was_fast_scan_mode: Whether this was a fast scan auto-add

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
    db_card_info = game.identify(card_name, collector_number, set_code, ai_model=get_ai_model_info())

    # Fast Scan only adds cards whose exact printing was confirmed (Game.confirmed_matches,
    # e.g. set + number); anything less certain pauses auto scanning for a review
    confirmed = game.is_confirmed(db_card_info)
    auto_add = was_fast_scan_mode and confirmed
    if was_fast_scan_mode and not confirmed and scanner:
        scanner.card_under_review = True
        what = f"the exact printing of {db_card_info['name']}" if db_card_info else f"'{card_name}'"
        log_to_client(f"Couldn't confirm {what} - please review (auto scanning paused)", level="warning")

    # Log scanned card
    log_scanned_card(
        card_name=card_name,
        collector_number=collector_number,
        ai_model=get_ai_model_info(),
        db_found=(db_card_info is not None),
        added_to_inventory=auto_add,
        processing_time=processing_time
    )

    if db_card_info:
        if image_path:
            db_card_info['capture'] = str(image_path)
        current_card_info = db_card_info
        socketio.emit('card_found', {
            'card': game.card_payload(db_card_info),
            'auto_add': auto_add
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

            # Auto-search database if Vision AI identified the card
            set_pending_capture(image_path)
            if card_name and card_name.strip():
                search_and_emit_card(card_name, collector_number, processing_time, was_fast_scan_mode=was_fast_scan_mode,
                                     set_code=set_code, image_path=image_path)

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
    global scanner, database, inventory

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

                # Auto-search database if Vision AI identified the card
                set_pending_capture(image_path)
                if card_name and card_name.strip():
                    search_and_emit_card(card_name, collector_number, processing_time, was_fast_scan_mode=False,
                                         set_code=set_code, image_path=image_path)

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
            'inventory': inventory.get_stats(game.id)
        })

    return jsonify({'error': 'Components not initialized'}), 500


@app.route('/api/inventory')
def get_inventory():
    """Get full inventory list"""
    global inventory

    if inventory:
        try:
            cards = inventory.get_all_cards(games.active().id)

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
        success = inventory.delete_card(row_id)

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
            if finish is not None and finish not in games.active().finishes:
                return jsonify({'success': False, 'split': False, 'error': f'Unknown finish: {finish}'}), 400

            # A new finish takes the printing's price in that finish (foil / holo / reverse)
            finish_price = None
            entry = inventory.get_entry(row_id) if finish else None
            if entry and finish != entry['finish'] and entry['card_id']:
                game = games.get(entry['game']) or games.active()
                card = game.get_card(entry['card_id'])
                if card:
                    finish_price = game.inventory_fields(card, finish)['price']

            # Changing the finish of several copies splits the entry
            result = inventory.update_card(row_id, quantity=quantity, condition=condition,
                                           finish=finish, split_quantity=split_quantity,
                                           finish_price=finish_price)

            if result['success']:
                return jsonify(result)
            else:
                return jsonify(result), 400

        except Exception as e:
            logger.error(f"Error updating card: {e}")
            return jsonify({'success': False, 'split': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'split': False, 'error': 'Inventory not initialized'}), 500


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
        export_path = inventory.export(game.id, writer, prefix)
        return send_file(str(export_path), mimetype='text/csv', as_attachment=True,
                         download_name=export_path.name)
    except Exception as e:
        logger.exception(f"Export failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/import_inventory', methods=['POST'])
def import_inventory():
    """Import inventory from CSV file upload"""
    global inventory

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
        stats = inventory.import_csv(temp_file_path, game.id, list(game.finishes),
                                     replace_existing=replace_existing)

        # Clean up temporary file
        temp_file_path.unlink()

        if stats['success']:
            inv_stats = inventory.get_stats(game.id)

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
    global inventory

    if not inventory:
        return jsonify({'error': 'Inventory not initialized'}), 500

    try:
        result = inventory.clear_inventory(games.active().id)

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

        # Automatically search database if Vision AI identified the card
        set_pending_capture(image_path)
        if card_name and card_name.strip():
            search_and_emit_card(card_name, collector_number, processing_time, was_fast_scan_mode=False,
                                 set_code=set_code, image_path=image_path)

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

    if not card_name:
        logger.warning("No card name provided in search request")
        emit('error', {'message': 'No card name provided'})
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
            similar = game.similar(card_name, limit=5)
            if similar:
                emit('similar_cards', {'cards': similar})
            else:
                emit('card_not_found', {'card_name': card_name})

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
    global inventory, current_card_info, scanner

    if not inventory or not current_card_info:
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

        # The card's own capture (automatic adds), else the one under review; it goes with
        # the first finish added
        capture = current_card_info.pop('capture', None) or pending_capture
        if capture == pending_capture:
            set_pending_capture(None)

        for item in items:
            finish = item.get('finish') or game.default_finish
            try:
                quantity = max(1, int(item.get('quantity', 1)))
            except (ValueError, TypeError):
                quantity = 1
            logger.info(f"Adding card to inventory: {quantity}x {current_card_info['name']} ({condition}, {finish})")
            inventory.add_card(game.inventory_fields(current_card_info, finish), game.id, finish, condition, quantity,
                               capture=capture)
            capture = None

            # Send updated stats and what was added (the page offers an Undo)
            emit('inventory_updated', {
                'stats': inventory.get_stats(game.id),
                'added': {
                    'name': current_card_info['name'],
                    'set': current_card_info['set'],
                    'number': current_card_info['number'],
                    'finish': game.finishes[finish],
                    'quantity': quantity
                }
            })

        logger.info("Card added to inventory successfully")
        current_card_info = None

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


@socketio.on('undo_last_add')
def handle_undo_last_add():
    """Take back the most recent add to the inventory"""
    if not inventory:
        emit('error', {'message': 'Inventory not initialized'})
        return

    undone = inventory.undo_last_add()
    if undone:
        emit('inventory_undone', {'name': undone, 'stats': inventory.get_stats(games.active().id)})
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
    }


@app.route('/api/games')
def get_games():
    """Supported card games and the one being scanned"""
    return jsonify({'active': games.active_id(), 'games': [game_info(game) for game in games.all_games()]})


@socketio.on('set_game')
def handle_set_game(data):
    """Switch the game being scanned (stops auto scanning; the current card is dropped)"""
    global current_card_info
    game_id = data.get('game')
    try:
        games.set_active(game_id)
    except ValueError as e:
        emit('error', {'message': str(e)})
        return
    current_card_info = None
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
        'autofocus': scanner.focus_locked_value is None if scanner else True,
        'fixed_area_enabled': bool(scanner.fixed_area_enabled) if scanner else False,
        'fixed_area': scanner.fixed_area if scanner else None,
        'camera_rotation': scanner.rotation if scanner else 0
    })


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
