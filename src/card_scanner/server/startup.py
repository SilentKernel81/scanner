# ============================================================================
# FILE: server/startup.py
# Start-up: every shared object is created here (initialize_components), then the web server runs.
# ============================================================================
import signal
import socket
import sys
import threading
import time
import webbrowser

try:
    import fcntl
except ImportError:  # not on Linux or macOS: no lock, as before
    fcntl = None

from card_scanner.core import arena, foil_marker, prices
from card_scanner.core.cleanup import get_images_stats
from card_scanner.config import Config
from card_scanner.core.database import CardDatabase
from card_scanner.core.decks import DeckManager
from card_scanner import games
from card_scanner.core.identification import Identification
from card_scanner.core.inventory import InventoryManager
from card_scanner.core.pending import PendingCaptures
from card_scanner.core.printing_check import PrintingCheck, StripReader
from card_scanner.core.reading.record import ReadingLog
from card_scanner.core.recommendations import Recommendations
from card_scanner.core.remote_scanner import CameraHub, RemoteScanner
from card_scanner.core.review import ReviewQueue
from card_scanner.settings import Settings
from card_scanner.core.stations import Stations
from card_scanner.core.trades import Trades
from card_scanner.core.others import Others

from card_scanner.server.state import (
    ARENA_INVENTORY_FILE, SCAN_INVENTORY_FILE, app, database, identification, maintenance_barrier, printing_check, services, socketio,
)
from card_scanner.server.logs import enable_request_log, logger
from card_scanner.server.desks import Desk, log_to_client, make_remote_scanner, scanner
from card_scanner.server.captures import resume_pending
from card_scanner.server.card_data import run_update_schedule, start_card_data_update
from card_scanner.server.backups_api import run_backup_schedule, scheduled_backup
from card_scanner.server.inventory_api import finish_interrupted_sends
from card_scanner.server.settings_events import cleanup_days, run_image_cleanup, saved_debug_mode

# Routes and Socket.IO events exist once their module has been imported. Every module that
# defines some is named here - also those this file uses nothing else of. A module missing
# from this list is a set of routes that silently does not exist (tests/test_app_surface.py
# notices).
from card_scanner.server import (  # noqa: F401
    lock, pages, decks_api, deck_ideas_api, inventory_api, arena_api, trades_api, others_api, backups_api, stations_api,
    scan_events, settings_events, ai_events, card_data, program,
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
    # Whose prices are shown and counted (Settings > Card data > Prices): copied into the card data's shown columns -
    # again at every start, about a second: the file may have been filled elsewhere since (scripts/setup_database.py)
    services.database.set_price_source(services.app_settings.get('price_source', prices.DEFAULT_SOURCE))
    # How large the large copy of each capture is (the person's choice on the scanner page)
    from card_scanner.core import inventory as inventory_module
    inventory_module.set_full_capture_size(services.app_settings.get('capture_copy_size', inventory_module.FULL_CAPTURE_DEFAULT))
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
    # The digital collection (MTG Arena) in its own file: empty until an export is imported.
    # No log_callback: nothing a camera does reaches it, and its imports report themselves
    services.arena_inventory = InventoryManager(db_file=ARENA_INVENTORY_FILE)
    arena.prepare(services.arena_inventory)   # the table for the wildcards and the import's date
    services.trade_store = Trades(services.inventory)
    services.others = Others()   # other people's collections, in a file of their own (data/others.db)
    services.review = ReviewQueue()
    finish_interrupted_sends()  # cards being sent back to review when the program stopped
    services.pending = PendingCaptures()
    services.deck_store = DeckManager()
    services.recommend = Recommendations()
    # Does a capture look like the printing the server resolved for it? Shadow mode: writes down where it
    # does not, changes nothing (core/printing_check.py). Without an image index (scripts/build_image_index.py) it does nothing.
    # The lookup reads the strip of a capture with a light-ocr reader OF ITS OWN (StripReader: a second process, started on first use, stopped when
    # idle) - never the live one, which a slow or failed shadow read would stall and disable - and only while OCR is on (the person's choice)
    services.printing_check = PrintingCheck(
        services.database, Config.DATA_DIR / 'image_index', Config.DATA_DIR / 'logs' / 'printing_check.jsonl', Config.PRINTING_CHECK_MODE,
        Config.PRINTING_CHECK_LOOKUP, strip_reader=StripReader(lambda: services.identification.ocr_enabled, foil_net=foil_marker.load(Config.OCR_FOIL_MARKER)),
        finish_for=lambda card, foil: games.active().suggested_finish(card, foil), game_id=games.active_id)
    # reading.mode 'evidence' reads a card by its picture and strip with the printing check's image index (the live reading's counterpart of its lookup)
    services.identification.evidence = services.printing_check
    # The reading log: one line per capture of what each step of the reading saw and did, in either mode (core/reading/record.py; the report:
    # scripts/reading_report.py) - the live evidence the picture's thresholds and guards are to be judged on
    services.identification.reading_log = ReadingLog(Config.DATA_DIR / 'logs' / 'reading.jsonl')

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
    """Clean away old received pictures: a few seconds after start, then once a day (a server
    that stays up for weeks would otherwise only clean at its next restart). The days kept are
    the person's choice (Settings > Storage), read at every run"""
    if not Config.CLEANUP_ENABLED:
        logger.info("Background cleanup disabled in configuration")
        return

    def cleanup_task():
        time.sleep(5)  # a few seconds after startup
        while True:
            try:
                days = cleanup_days()
                logger.info(f"Starting background cleanup of scanned images (keeping last {days} days)...")
                stats_before = get_images_stats()
                if stats_before['count'] > 0:
                    logger.info(f"Images before cleanup: {stats_before['count']} files, {stats_before['total_size_mb']:.2f} MB, oldest: {stats_before['oldest_days']:.1f} days")
                    files_deleted, space_freed = run_image_cleanup()
                    if files_deleted > 0:
                        logger.info(f"Cleanup complete: Deleted {files_deleted} old images, freed {space_freed:.2f} MB")
                    else:
                        logger.info("Cleanup complete: No old images to remove")
                else:
                    logger.info("No images found to clean up")
            except Exception as e:
                logger.error(f"Error during background cleanup: {e}")
            time.sleep(24 * 60 * 60)

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()
    logger.info("Background cleanup thread started")


instance_lock = None  # the open data/app.lock, held while this copy runs


def lock_instance():
    """
    False when another copy runs on this data folder - also one that is still starting and
    does not answer on its port yet. Two servers on one folder would write the same databases
    and settings; the lock goes with the process, so a crash leaves nothing to clean up.
    """
    global instance_lock
    if fcntl is None:
        return True
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(Config.DATA_DIR / 'app.lock', 'w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    instance_lock = handle  # kept open: the lock is gone when this process is
    return True


def port_answers(port, host='localhost'):
    """Whether a program on this computer already listens on the port (on that address)"""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def any_answers(port):
    """Whether something answers on the port, on loopback or on the address flask.host names"""
    return any(port_answers(port, url.split('//')[1].rsplit(':', 1)[0].strip('[]'))
               for url in program.candidate_urls(port))


def open_browser_when_ready(url):
    def wait():
        for _ in range(120):
            if any_answers(Config.PORT):
                webbrowser.open(url)
                return
            time.sleep(0.5)
    threading.Thread(target=wait, daemon=True, name='open-browser').start()


def quit_running_program(urls):
    import requests
    for url in urls:
        try:
            if requests.post(f"{url}/api/quit", timeout=5).ok:
                print("Card Scanner stopped")
                return
        except requests.RequestException:
            continue
    print(f"No Card Scanner to stop at {' or '.join(urls)}")


def main():
    """Start the web server"""
    args = program.parse_arguments()
    services.app_mode = args.app
    url = program.candidate_urls(Config.PORT)[0]  # until the saved settings are read (local_url below)

    if args.quit:
        quit_running_program(program.candidate_urls(Config.PORT))
        return

    # A second copy could not use the databases or the port: show the one that runs instead
    if not lock_instance():
        print(f"Card Scanner is running already on this data folder ({Config.shown()}): {url}")
        if services.app_mode and not args.no_browser:
            for _ in range(120):  # it may still be starting
                if any_answers(Config.PORT):
                    break
                time.sleep(0.5)
            webbrowser.open(url)
        return
    if any_answers(Config.PORT):
        print(f"Port {Config.PORT} is in use by another program (or a Card Scanner with another data folder): {url}")
        if services.app_mode and not args.no_browser:
            webbrowser.open(url)
        return

    # Use print for important startup messages that should always be visible
    print("="*60)
    print("Card Scanner Web Interface")
    print("="*60)

    if Config.USER_DIR != Config.BASE_DIR:
        print(f"✓ Your data: {Config.USER_DIR}")

    # Initialize components
    logger.info("Starting scanner initialization...")
    initialize_components()

    # Start background cleanup
    run_cleanup_background()
    run_update_schedule()
    run_backup_schedule()

    game = games.active()
    if game.card_count():
        logger.info(f"Scanning {game.label}: {game.card_count():,} cards in the database")
        print(f"✓ {game.label}: {game.card_count():,} cards")
    else:
        # First start: the pages work meanwhile and show the progress
        logger.info(f"No {game.label} card data yet - downloading it from {game.source}")
        print(f"⚠ {game.label}: no card data yet - downloading it from {game.source} (a few minutes)")
        start_card_data_update(game, retry=True)  # no data to fall back on: a failure is tried again

    # Check Vision AI status
    if identification.card_identifier:
        ai = f"{identification.card_identifier.provider} / {identification.card_identifier.model}"
        logger.info(f"Vision AI enabled using {ai}")
        print(f"✓ Vision AI enabled ({ai})")
    else:
        logger.warning("Vision AI disabled - no API key configured")
        print("⚠ Vision AI disabled")
        print("  To enable: enter an API key in Settings -> Card recognition -> Vision AI (or pick a local model)")

    print("✓ Cameras: at the stations - each connects by itself (station_client.py, the Android app)")
    print(services.daily_backup_status)

    services.debug_mode_running = saved_debug_mode()
    services.network_access_running = program.network_access()
    host = program.listen_host()
    # Debug mode = the request log, and - only where nobody else can reach the server - the
    # interactive debugger (program.debugger_allowed)
    services.debugger_running = bool(services.debug_mode_running and program.debugger_allowed(host))
    url = program.local_url(Config.PORT)  # where this computer reaches it: also for an address of its own
    if services.debug_mode_running:
        enable_request_log()
    logger.info(f"Debug trace: {'on' if scanner.debug_trace_enabled else 'off'}, debug mode: {'on' if services.debug_mode_running else 'off'} (Settings)")
    # (the messages apart: Python 3.10 and 3.11 cannot nest f-strings with the same quotes)
    trace_message = "on - frames go to " + Config.shown('debug_frames')
    requests_message = "on - requests go to " + Config.shown('logs', 'requests.log')
    print(f"{'✓' if scanner.debug_trace_enabled else '-'} Debug trace: {trace_message if scanner.debug_trace_enabled else 'off'}")
    print(f"{'✓' if services.debug_mode_running else '-'} Debug mode: {requests_message if services.debug_mode_running else 'off'}")
    if services.debug_mode_running:
        print("  Interactive debugger: " + ("on" if services.debugger_running else
              "off - the server is reachable from the network (SCANNER_LAN_DEBUGGER=1 turns it on anyway)"))

    print("\n" + "="*60)
    print("Web Interface Starting...")
    print("="*60)
    print("\n✓ Access the scanner at:")
    print(f"  • Local:   {url}")
    if services.network_access_running:
        print(f"  • Network: http://<this-computer's-address>:{Config.PORT}")
    else:
        print("  • This computer only (Settings -> Reachable from other devices)")
    print(f"\n✓ Logs are being written to: {Config.shown('logs', 'app.log')}")
    print("\nPress Ctrl+C to stop" + (" (or Settings -> Quit)" if services.app_mode else ""))
    print("="*60 + "\n")

    logger.info(f"Starting Flask server on {host}:{Config.PORT}")

    # Flask's own two start lines (" * Serving Flask app", " * Debug mode") repeat what the
    # banner above says
    import flask.cli
    flask.cli.show_server_banner = lambda *args, **kwargs: None
    sys.stdout.flush()  # those lines used to flush the banner when the output is not a terminal (the service's journal)

    signal.signal(signal.SIGTERM, program.stop_on_signal)  # systemctl stop, Quit, the session ending
    if services.app_mode:
        if not args.no_station:
            program.start_station(Config.PORT)  # this computer's camera, ended with the server
        if not args.no_browser:
            open_browser_when_ready(url)

    try:
        # Start Flask app with SocketIO
        socketio.run(
            app,
            host=host,
            port=Config.PORT,
            debug=services.debugger_running,
            # Never the reloader: it starts the program a second time, and only one process
            # can open the camera
            use_reloader=False,
            allow_unsafe_werkzeug=True  # Allow development server for local use
        )
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        logger.info("Shutdown requested by user")
    finally:
        program.stop_station()  # first: it lets go of the camera
        if printing_check:
            printing_check.close()  # the lookup's own OCR process, if it has one
        if identification:
            identification.stop()
        if scanner:
            scanner.cleanup()
        if database:
            database.close()
        logger.info("Scanner stopped successfully")
        print("Scanner stopped.")

    program.exec_again_if_requested()
