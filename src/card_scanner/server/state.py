# ============================================================================
# FILE: server/state.py
# What every part of the server shares: the Flask app, Socket.IO, and the
# objects that are created once at start-up (the card database, the two
# inventories, the review queue, ...).
#
# The shared objects do not exist yet when the modules are imported:
# startup.initialize_components() creates them, later. So they are kept as
# attributes of one holder, `services`, and each is also offered under its own
# name as a *proxy* - a stand-in that looks the real object up every time it is
# used:
#
#     from server.state import inventory        # a proxy, valid before start-up
#     inventory.get_all_cards(...)              # reaches services.inventory, now
#
# That is why the rest of the server can write `inventory`, `database`,
# `review` as plain names. Three things to know about a proxy:
#   - `if inventory:` works, `inventory is None` does NOT (the proxy itself is
#     never None) - the same rule as for `scanner` (server/desks.py);
#   - to set one, assign the holder's attribute: `services.inventory = ...`
#     (only start-up does);
#   - `a is b` compares the proxies, which is fine between two of these names
#     (`inventory_area() is scan_inventory`), but a proxy is never `is` the
#     real object taken from `services`.
# Plain values that change (a flag, a line of text) have no proxy: they are read
# and written as `services.debug_mode_running`, always.
# ============================================================================
import collections
import threading

from flask import Flask
from flask_socketio import SocketIO
from werkzeug.local import LocalProxy

from card_scanner.config import Config
from card_scanner.paths import CODE
from card_scanner.server import access

# Initialize Flask app. Its name stays 'app', as when this was app.py: endpoint names and
# url_for depend on nothing else. Its folder is server/, where templates/ and static/ are -
# said outright (a Flask app looks for both beside the module it is named after, and there
# is no module called 'app' in here). The address of a static file stays /static/...
app = Flask('app', root_path=str(CODE / 'server'))
app.config['SECRET_KEY'] = access.secret_key()

# Initialize SocketIO with proper configuration
socketio = SocketIO(
    app,
    cors_allowed_origins=access.socket_origin_allowed,
    async_mode='threading',
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25
)

# After SocketIO: its middleware must be inside the Host check (server/access.py: protect)
access.protect(app)

# Cards added by the scanner page, until "Add to collection" (same tables as the collection's)
SCAN_INVENTORY_FILE = Config.DATA_DIR / 'scan_inventory.db'
# The digital collection: what the user has in MTG Arena, as the last import said (same
# tables again; core/arena.py). A file of its own: an import replaces all of it, and that
# must never be able to reach the cards on the shelf
ARENA_INVENTORY_FILE = Config.DATA_DIR / 'arena_inventory.db'


class Services:
    """The objects created at start-up (startup.initialize_components), and a few values that change"""
    app_settings = None       # settings.Settings: data/settings.json
    camera_hub = None         # remote_scanner.CameraHub: the stations' cameras
    identification = None     # identification.Identification: OCR, vision AI and their queues
    stations = None           # stations.Stations: cameras elsewhere that send their captures here
    pending = None            # pending.PendingCaptures: captures in the queues, on disk until settled
    database = None
    inventory = None           # the collection (table inventory in the card database file)
    scan_inventory = None      # what the scanner page adds to, until it is moved to the collection
    arena_inventory = None     # the digital collection (MTG Arena): filled by imports only, never by a camera
    trade_store = None        # trades.Trades: the collection's cards set aside for a trade
    review = None             # review.ReviewQueue
    deck_store = None         # decks.DeckManager
    recommend = None          # recommendations.Recommendations
    default_desk = None       # desks.Desk of the camera on this machine (set in server/desks.py)
    debug_mode_running = Config.DEBUG  # Flask's debug mode as this process was started (main)
    app_mode = False           # started as a desktop program (--app; server/program.py)
    restart_requested = False  # main starts the program again after shutting down
    network_access_running = False  # whether other devices can reach this process (main)
    station_process = None     # the camera station started beside the server (program.start_station)
    daily_backup_status = ''    # the startup banner's line about the day's backup


services = Services()

# The proxies (see the top of this file)
app_settings = LocalProxy(lambda: services.app_settings)
camera_hub = LocalProxy(lambda: services.camera_hub)
identification = LocalProxy(lambda: services.identification)
stations = LocalProxy(lambda: services.stations)
pending = LocalProxy(lambda: services.pending)
database = LocalProxy(lambda: services.database)
inventory = LocalProxy(lambda: services.inventory)
scan_inventory = LocalProxy(lambda: services.scan_inventory)
arena_inventory = LocalProxy(lambda: services.arena_inventory)
trade_store = LocalProxy(lambda: services.trade_store)
review = LocalProxy(lambda: services.review)
deck_store = LocalProxy(lambda: services.deck_store)
recommend = LocalProxy(lambda: services.recommend)

# What became of the stations' last few hundred captures: {(station id, capture number): outcome}
capture_outcomes = collections.OrderedDict()
# Captures by the id their sender gave them, so one sent twice (no answer the first time) is
# one capture: {capture id: (station id, capture number)}
seen_captures = collections.OrderedDict()
# Held while a capture's result is applied and while old pictures are cleaned away - and by a
# full backup (backups.create_full), which must see neither half done
maintenance_barrier = threading.RLock()
capture_accept_lock = threading.Lock()  # station_capture: is this capture known? - no: it is now
capture_outcomes_lock = threading.Lock()
