# ============================================================================
# FILE: server/desks.py
# Desks: one per camera - whose card, review and events something is.
#
# The handlers look single-camera, but `scanner` is a proxy for desk().scanner, and the card on a
# page, its open review and its capture belong to desk(). Code that runs outside a page's
# request (threads, callbacks) must be inside `with in_desk(...)`.
# ============================================================================
import contextlib
from datetime import datetime
from flask import has_request_context, request
import threading
from werkzeug.local import LocalProxy

from card_scanner.config import Config
from card_scanner.core.remote_scanner import RemoteScanner
from card_scanner.core.review import ANY
from card_scanner.core.stations import StationSettings

from card_scanner.server.state import app_settings, camera_hub, services, socketio, stations
from card_scanner.server.logs import logger


class Desk:
    """
    One camera's side of the app: its scanner, the card on its page, its open review, its
    captures being read. Every station has one (desk_for); default_desk is what a request that
    names no station gets - a desk whose camera never connects.
    The code below reaches the current one through desk() and the `scanner` proxy.
    """

    def __init__(self, station_id=None, scanner=None, review_filter=ANY):
        self.id = station_id                # stations.py id; None: default_desk
        self.scanner = scanner              # remote_scanner.RemoteScanner: the station's camera, as seen from here
        self.review_filter = review_filter  # whose review items its page shows (review.py)
        self.current_card_info = None       # the card on its page, waiting for Add / Skip
        self.current_review_id = None       # review queue item open on its page
        self.review_sid = None              # Socket.IO session of the page reviewing it (a reload / disconnect closes it)
        # The capture being reviewed (image path): kept with the card added from it - also
        # when the card is found by a manual search after the AI couldn't identify it
        self.pending_capture = None
        # The capture_id of each of the station's captures shown for Add / Skip (station_client's mode 'review': auto-scanning with auto-add
        # off, or a manual capture), by the capture's OWN picture (str path) - the key the shown card carries (card['capture'], pending_capture).
        # Its card dropper releases the next card only once THAT capture is added, skipped or sent to the review queue (review_done). Never one
        # shared field: a later capture arriving while an earlier one is still shown must not take its place (a Codex review of 2026-10-22)
        self.capture_ids = {}
        self.processing = 0                 # captures being identified (shown on its page)
        self.last_capture = None            # file name (in scanned_cards/) of the picture a camera-less station sent last
        # The printings the review screen showed for the open item (printing_check.candidates' ids, nearest first): the reading log notes which of them the
        # person picked (scan_events.add_selected_card) - the label that judges the picture's guards
        self.review_candidates = []

    @property
    def room(self):
        """Socket.IO room of the pages showing this desk (None: every page)"""
        return f"station:{self.id}" if self.id else None

    @property
    def station(self):
        return stations.get(self.id) if self.id and stations else None
services.default_desk = Desk()
desks = {}            # station id -> Desk
desks_lock = threading.RLock()
GENERAL_ROOM = 'general'  # pages that show no station (collection, decks, settings): log lines no station owns
page_stations = {}    # Socket.IO session of a scanner page -> the station it shows
current = threading.local()  # .desk: set by in_desk for work outside a page's request


def desk_for(station_id):
    """A station's desk (made when first asked for), or None for an unknown station"""
    with desks_lock:
        found = desks.get(station_id)
        if found is None and stations and station_id and stations.get(station_id) is not None:
            found = desks[station_id] = Desk(station_id, review_filter=station_id)
            found.scanner = camera_hub.scanner(station_id)
        return found


def desk():
    """
    The desk this code is working for: the one set with in_desk (worker threads, a station's
    upload), else the station of the scanner page that sent the Socket.IO event, else the
    station named in the request (?station=, /api/stations/<id>/...), else default_desk.
    """
    forced = getattr(current, 'desk', None)
    if forced is not None:
        return forced
    if has_request_context():
        sid = getattr(request, 'sid', None)
        station_id = (page_stations.get(sid) if sid else
                      request.args.get('station') or (request.view_args or {}).get('station_id'))
        found = desk_for(station_id) if station_id else None
        if found is not None:
            return found
    return services.default_desk


@contextlib.contextmanager
def in_desk(target):
    previous = getattr(current, 'desk', None)
    current.desk = target or services.default_desk
    try:
        yield current.desk
    finally:
        current.desk = previous


def at_station(station_id, function, *args):
    """Call function as the station's desk (callbacks of its RemoteScanner)"""
    with in_desk(desk_for(station_id)):
        return function(*args)


def emit_desk(event, data):
    """To the pages of the current desk (every page for default_desk)"""
    socketio.emit(event, data, namespace='/', to=desk().room)


# The current desk's scanner: `scanner.x` and `if scanner:` work as on the object itself
scanner = LocalProxy(lambda: desk().scanner)


def make_remote_scanner(station_id):
    """CameraHub's factory: the stand-in for a station's camera, with the station's own settings"""
    remote = RemoteScanner(
        socketio, StationSettings(stations, station_id, app_settings),
        log_callback=lambda message, level="info": at_station(station_id, log_to_client, message, level),
        on_captured=lambda number: at_station(station_id, emit_desk, 'auto_capture_triggered', {
            'counter': number, 'message': f'Auto-capture #{number}'}),
        on_auto_capture_changed=lambda enabled: at_station(station_id, emit_desk, 'auto_capture_toggled',
                                                           {'enabled': enabled}))
    # What set_auto_add would do for a camera here: the station's saved choice, and the number of
    # still frames that goes with it. Sent to the client when it connects (RemoteScanner.hello)
    auto_add = bool(remote.settings.get('auto_add', True))
    remote.fast_scan_mode = auto_add
    remote.required_stable_frames = Config.FAST_SCAN_STABILITY_FRAMES if auto_add else Config.AUTO_CAPTURE_STABILITY_FRAMES
    return remote


CAPTURE_IDS_KEPT = 50     # captures shown and never settled (the page closed, the server busy elsewhere) are let go after this many


def remember_capture_id(image_path, capture_id):
    """A station's capture that will be shown for Add / Skip arrived: its id, under its own picture (stations_api.station_capture)"""
    if not capture_id:
        return
    ids = desk().capture_ids
    ids[str(image_path)] = capture_id
    while len(ids) > CAPTURE_IDS_KEPT:
        ids.pop(next(iter(ids)))        # the oldest: dicts keep their order


def forget_capture_id(image_path):
    """Reading the capture failed: nothing to add or skip, and no card released for it"""
    if image_path:
        desk().capture_ids.pop(str(image_path), None)


def review_done(image_path):
    """
    The capture whose picture is image_path was settled by the person - added or skipped while it was SHOWN - or went to the review
    queue by itself: its station may release the next card (station_client: on_review_done; only a card dropper that owes a card for
    exactly that capture does). image_path is the shown card's own picture, never "the latest capture", so settling one card cannot
    release another's next card. Never called when something failed or scanning stopped: a card dropper must not drop a card onto
    one the person has not dealt with (clearing card_under_review, which errors also do, used to count as Add / Skip).
    """
    capture_id = desk().capture_ids.pop(str(image_path), None) if image_path else None
    if capture_id and scanner and hasattr(scanner, 'review_done'):
        scanner.review_done(capture_id)


def queue_changed(change):
    """A capture went to be identified (+1) or is done (-1): tell the desk's pages how many are being read"""
    target = desk()
    with desks_lock:
        target.processing = max(0, target.processing + change)
        count = target.processing
    emit_desk('processing_queue_update', {'queue_count': count})


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

    # Send to web client via SocketIO. A station's page gets only its own lines: what no station
    # owns (backups, card data, the collection, startup) goes to the pages that show no station,
    # not to every station's Activity tab
    socketio.emit('log', {
        'timestamp': timestamp,
        'level': level,
        'message': message
    }, namespace='/', to=desk().room or GENERAL_ROOM)
