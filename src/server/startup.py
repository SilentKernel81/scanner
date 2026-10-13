# ============================================================================
# FILE: server/startup.py
# Start-up: every shared object is created here (initialize_components), then the web server runs.
# ============================================================================
import sys
import threading
import time

from cleanup import cleanup_old_images, get_images_stats
from config import Config
from database import CardDatabase
from decks import DeckManager
import games
from identification import Identification
from inventory import InventoryManager
from pending import PendingCaptures
from recommendations import Recommendations
from remote_scanner import CameraHub, RemoteScanner
from review import ReviewQueue
from settings import Settings
from stations import Stations
from trades import Trades

from server.state import (
    SCAN_INVENTORY_FILE, app, database, identification, maintenance_barrier, services, socketio,
)
from server.logs import enable_request_log, logger
from server.desks import Desk, log_to_client, make_remote_scanner, scanner
from server.captures import resume_pending
from server.card_data import run_update_checks
from server.backups_api import run_backup_schedule, scheduled_backup
from server.settings_events import saved_debug_mode

# Routes and Socket.IO events exist once their module has been imported. Every module that
# defines some is named here - also those this file uses nothing else of. A module missing
# from this list is a set of routes that silently does not exist (tests/test_app_surface.py
# notices).
from server import (  # noqa: F401
    pages, decks_api, deck_ideas_api, inventory_api, trades_api, backups_api, stations_api,
    scan_events, settings_events, ai_events, card_data,
)


def initialize_components():
    """Initialize all components"""

    logger.info("Initializing components...")

    # Create directories
    Config.create_directories()

    # Initialize database
    logger.info("Initializing database...")
    services.database = CardDatabase()

    # Cameras are at the stations, each reached through the hub. The server has none of its own
    # (it could open one itself until 2026-10-12; a machine that is server and camera in one
    # now runs station_client.py beside this program)
    logger.info("Initializing scanner...")
    services.app_settings = Settings()
    services.stations = Stations()
    services.camera_hub = CameraHub(socketio, services.stations, make_remote_scanner)
    # A request that names no station has no camera: this one never connects
    services.default_desk = Desk(scanner=RemoteScanner(socketio, services.app_settings))
    log_to_client("Scanner initialized", level="info")

    # Card games (Magic, ...) - each wraps its card data; the saved one is scanned
    games.init(services.database, services.app_settings, log_callback=log_to_client)

    # OCR and the vision AI, with a queue in front of each (shared by every camera)
    services.identification = Identification(services.app_settings, log_callback=log_to_client)
    services.identification.start()

    # Note: get_ai_model_info() and log_scanned_card() are defined at module level
    # so they can be accessed by both Flask routes and the AI worker thread

    # Initialize inventory manager
    logger.info("Initializing inventory...")
    services.inventory = InventoryManager(log_callback=log_to_client)
    # Scanned cards wait in their own file: clearing them never touches the collection
    services.scan_inventory = InventoryManager(db_file=SCAN_INVENTORY_FILE, log_callback=log_to_client)
    services.inventory.finish_interrupted_moves(services.scan_inventory)  # an "Add to collection" cut short by a crash
    services.trade_store = Trades(services.inventory)
    services.review = ReviewQueue()
    services.pending = PendingCaptures()
    services.deck_store = DeckManager()
    services.recommend = Recommendations()

    # The automatic backup of the collection, scanned cards and decks, if one is due
    # (run_backup_schedule asks again while the app runs)
    try:
        made = scheduled_backup('Application start')
        services.daily_backup_status = (
            f"✓ Automatic backup made: {made['cards']} cards, {made['scanned']} scanned, {made['decks']} decks" if made
            else "- Automatic backup: none due (switched off, made recently, or nothing changed)")
        logger.info(services.daily_backup_status[2:])
    except Exception as e:
        services.daily_backup_status = f"⚠ The automatic backup failed: {e}"
        logger.error(f"The automatic backup failed: {e}", exc_info=True)

    resume_pending()

    logger.info("All components initialized successfully!")
    log_to_client("All components initialized successfully", level="success")


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
            with maintenance_barrier:  # not while a full backup is collecting the pictures
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
    run_backup_schedule()

    game = games.active()
    logger.info(f"Scanning {game.label}: {game.card_count():,} cards in the database")
    print(f"✓ {game.label}: {game.card_count():,} cards")

    # Check Vision AI status
    if identification.card_identifier:
        ai = f"{identification.card_identifier.provider} / {identification.card_identifier.model}"
        logger.info(f"Vision AI enabled using {ai}")
        print(f"✓ Vision AI enabled ({ai})")
    else:
        logger.warning("Vision AI disabled - no API key configured")
        print(f"⚠ Vision AI disabled")
        print("  To enable: enter an API key in Settings -> Vision AI (or pick a local model)")

    print("✓ Cameras: at the stations - each connects by itself (station_client.py, the Android app)")
    print(services.daily_backup_status)

    services.debug_mode_running = saved_debug_mode()
    if services.debug_mode_running:
        enable_request_log()
    logger.info(f"Debug trace: {'on' if scanner.debug_trace_enabled else 'off'}, debug mode: {'on' if services.debug_mode_running else 'off'} (Settings)")
    print(f"{'✓' if scanner.debug_trace_enabled else '-'} Debug trace: {'on - frames go to data/debug_frames' if scanner.debug_trace_enabled else 'off'}")
    print(f"{'✓' if services.debug_mode_running else '-'} Debug mode: {'on - requests go to data/logs/requests.log' if services.debug_mode_running else 'off'}")

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

    # Flask's own two start lines (" * Serving Flask app", " * Debug mode") repeat what the
    # banner above says
    import flask.cli
    flask.cli.show_server_banner = lambda *args, **kwargs: None
    sys.stdout.flush()  # those lines used to flush the banner when the output is not a terminal (the service's journal)

    try:
        # Start Flask app with SocketIO
        socketio.run(
            app,
            host=Config.HOST,
            port=Config.PORT,
            debug=services.debug_mode_running,
            # Never the reloader: it starts the program a second time, and only one process
            # can open the camera
            use_reloader=False,
            allow_unsafe_werkzeug=True  # Allow development server for local use
        )
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        logger.info("Shutdown requested by user")
    finally:
        if identification:
            identification.stop()
        if scanner:
            scanner.cleanup()
        if database:
            database.close()
        logger.info("Scanner stopped successfully")
        print("Scanner stopped.")
