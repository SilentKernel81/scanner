# ============================================================================
# FILE: server/stations_api.py
# Stations: cameras elsewhere that capture cards and send them here (stations.py).
# ============================================================================
import cv2
from flask import jsonify, request
import hmac
import numpy as np
import threading
import time

from config import Config
import games
from stations import Stations

from server.state import (
    app, camera_hub, capture_accept_lock, capture_outcomes, capture_outcomes_lock, identification,
    pending, review, scan_inventory, seen_captures, socketio, stations,
)
from server.logs import logger
from server.desks import (
    desk_for, desks, desks_lock, emit_desk, in_desk, log_to_client, queue_changed, scanner,
)
from server.captures import (
    announce_and_route, foil_file, remember_outcome, scan_stats, station_prefix, submit_capture,
)
from server.inventory_api import clear_camera


STATION_CAPTURE_MAX_BYTES = 30 * 1024 * 1024  # a card image and its foil image
STATION_CAPTURE_WAIT = 30  # seconds a station waits for its card's outcome unless it says otherwise


def station_token_ok():
    """Whether a station's request carries the token (when one is set: stations.token)"""
    return not Config.STATION_TOKEN or hmac.compare_digest(request.headers.get('X-Station-Token', ''),
                                                           Config.STATION_TOKEN)


def save_upload(path, data, image):
    """Keep an uploaded picture as a JPEG: the station's own bytes when it sent one"""
    if data[:2] == b'\xff\xd8':
        path.write_bytes(data)
    else:
        cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))


def decode_upload(file):
    """(bytes, RGB image) of an uploaded picture, or (None, None)"""
    data = file.read() if file else b''
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
    if image is None:
        return None, None
    return data, cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


@app.route('/api/stations')
def station_list():
    """
    The stations, with whether each one's camera is connected and what it sees. 'scanned' is
    what a station has in the scanned cards now; 'captures' every picture it ever sent.
    """
    listed = []
    for station in stations.all():
        connected = camera_hub.connected(station['id'])
        status = camera_hub.scanner(station['id']).get_detection_status() if connected else {}
        listed.append({**station, 'connected': connected, 'camera_error': status.get('camera_error'),
                       'detected': bool(status.get('detected')),
                       'scanning': bool(connected and camera_hub.scanner(station['id']).auto_capture_enabled),
                       'scanned': scan_inventory.get_stats(games.active_id(), station['id'])['total_cards'],
                       'review': review.count(games.active_id(), station['id'])})
    return jsonify({'stations': listed, 'token_required': bool(Config.STATION_TOKEN)})


@app.route('/api/stations/<station_id>', methods=['PUT', 'DELETE'])
def station_item(station_id):
    """Rename a station, set the location its cards are put in, or forget it"""
    if request.method == 'DELETE':
        if stations.get(station_id) is None:
            return jsonify({'success': False, 'message': 'No such station'}), 404
        # Nothing of it is left behind where no page would show it: its scanned cards (not yet
        # in the collection) and its review items go first, in every game
        cards = reviews = 0
        for game in games.all_games():
            result, removed = clear_camera(game, station_id)
            if not result['success']:
                return jsonify({'success': False, 'message': result.get('error') or 'Could not clear its cards'}), 500
            cards, reviews = cards + result['deleted'], reviews + removed
        with desks_lock:
            desks.pop(station_id, None)
        camera_hub.forget(station_id)
        stations.remove(station_id)
        socketio.emit('inventory_updated', {'auto': False, 'cleared': True}, namespace='/')
        logger.info(f"Station {station_id} forgotten: {cards} scanned cards and {reviews} review items removed")
        return jsonify({'success': True, 'cards': cards, 'reviews': reviews})
    data = request.get_json(silent=True) or {}
    station = stations.update(station_id, name=data.get('name'), location=data.get('location'))
    if station is None:
        return jsonify({'success': False, 'message': 'No such station'}), 404
    return jsonify({'success': True, 'station': station})


@app.route('/api/stations/<station_id>/captures', methods=['POST'])
def station_capture(station_id):
    """
    A card captured by a station. Multipart form:
      image       the card as the station cut it out (JPEG / PNG)
      foil_image  optional: the perspective-corrected card, for the star/dot foil check
      name        optional: what the station calls itself
      wait        optional: seconds to wait for the outcome (default 30; 0: answer at once)
      capture_id  optional: the station's own id for this capture - sent again, it is not a new one
      foil_is_image  optional, '1': image is the perspective-corrected card (no foil_image needed)
      camera, mode   station_client.py: mode 'review' = show it on the station's page, don't add
    The card is read (OCR, then the vision AI), and - as when adding automatically - added to
    the scanned cards if its printing is confirmed, else queued for review.
    Answers {'capture': n, 'status': 'added' | 'review' | 'pending', ...}; 202 while pending.
    """
    if not Stations.valid_id(station_id):
        return jsonify({'success': False, 'message': 'Station ids are 1-40 letters, digits, - or _'}), 400
    if not station_token_ok():
        return jsonify({'success': False, 'message': 'Wrong or missing station token'}), 401
    if (request.content_length or 0) > STATION_CAPTURE_MAX_BYTES:
        return jsonify({'success': False, 'message': 'Capture too large'}), 413
    data, image = decode_upload(request.files.get('image'))
    if image is None:
        return jsonify({'success': False, 'message': "No readable picture in 'image'"}), 400
    foil_data, foil_image = decode_upload(request.files.get('foil_image'))
    if foil_image is None and request.form.get('foil_is_image') == '1':
        foil_image = image  # the card image is the perspective-corrected card
    try:
        wait = max(0.0, min(float(request.form.get('wait', STATION_CAPTURE_WAIT)), 120.0))
    except ValueError:
        wait = STATION_CAPTURE_WAIT

    # Sent before? (the station got no answer and tries again): the same capture, not a new one
    capture_id = (request.form.get('capture_id') or '')[:64] or None
    # Asked and claimed in one step: the same capture arriving twice at once (a retry crossing
    # its first upload) was two captures when the second asked before the first had claimed it
    with capture_accept_lock:
        with capture_outcomes_lock:
            known = seen_captures.get(capture_id) if capture_id else None
        settled = None if known else pending.settled(capture_id)
        if not known and not settled:
            station = stations.capture(station_id, request.form.get('name'))
            number = station['captures']
            key = (station_id, number)
            if capture_id:
                with capture_outcomes_lock:
                    seen_captures[capture_id] = key
                    while len(seen_captures) > 2000:
                        seen_captures.popitem(last=False)
    if known:
        return capture_outcome_response(*known)
    if settled:
        # Settled before a restart (the list above is this run's): the same answer, no second card
        outcome = settled[2]
        return jsonify({'success': outcome.get('status') != 'error', 'capture': settled[1], **outcome})
    image_path = Config.IMAGES_DIR / f"{station_id}_{number}_{int(time.time())}.jpg"
    save_upload(image_path, data, image)
    game_id = games.active_id()

    # From here on desk() is this station's (the URL names it, and it exists now).
    # station_client.py's mode 'review': the card is shown on the station's page, not added
    if request.form.get('camera') == '1' and request.form.get('mode') == 'review':
        # A manual capture, or auto scanning that waits for Add / Skip: read now and shown
        queue_changed(+1)
        try:
            card_info = identification.identify(image, foil_image)
            outcome = announce_and_route(image_path, card_info, number, game_id)
        except Exception as e:
            logger.exception(f"Capture {image_path.name} failed: {e}")
            log_to_client(f"Capture error: {e}", level="error")
            scanner.card_under_review = False  # don't hold the next capture back
            outcome = {'status': 'error', 'message': str(e)}
        finally:
            queue_changed(-1)
        remember_outcome(key, outcome)
        return capture_outcome_response(*key)

    foil_path = None
    if foil_image is not None and foil_image is not image:
        foil_path = foil_file(image_path)
        save_upload(foil_path, foil_data, foil_image)

    # The capture is in the queues now, whatever happens to this request. Wait for its outcome as
    # long as the station asked; what is known then is the answer (still being read: 202 pending)
    done = threading.Event()
    submit_capture(image_path, image, foil_image, number, game_id, station,
                   on_outcome=lambda _outcome: done.set(), foil_path=foil_path, outcome_key=key, capture_id=capture_id)
    done.wait(wait)
    return capture_outcome_response(*key)


def capture_outcome_response(station_id, number):
    """What became of a station's capture: its outcome, 202 while it is being read, or 404"""
    with capture_outcomes_lock:
        outcome = capture_outcomes.get((station_id, number))
    if outcome is not None:
        return jsonify({'success': outcome.get('status') != 'error', 'capture': number, **outcome})
    if pending.has(station_id, number):
        return jsonify({'success': True, 'capture': number, 'status': 'pending'}), 202
    outcome = pending.outcome(station_id, number)  # settled before a restart
    if outcome is not None:
        return jsonify({'success': outcome.get('status') != 'error', 'capture': number, **outcome})
    return jsonify({'success': False, 'message': 'No such capture (outcomes are kept for the last few hundred)'}), 404


@app.route('/api/stations/<station_id>/captures/<int:number>')
def station_capture_outcome(station_id, number):
    """What became of a capture that was still pending when its upload was answered"""
    if not station_token_ok():
        return jsonify({'success': False, 'message': 'Wrong or missing station token'}), 401
    return capture_outcome_response(station_id, number)


def undo_station_add(station):
    """Take back a station's most recent automatic add. Returns the card's name, or None"""
    name = scan_inventory.undo_last_add(source=station['id'])
    if name:
        with in_desk(desk_for(station['id'])):
            log_to_client(f"{station_prefix(station)}Removed {name} from the scanned cards (undo)", level="warning")
            emit_desk('inventory_updated', {'auto': True, 'stats': scan_stats(games.active_id()),
                                            'undone': name})
    return name


@app.route('/api/stations/<station_id>/undo', methods=['POST'])
def station_undo(station_id):
    """A station takes back its last card"""
    if not station_token_ok():
        return jsonify({'success': False, 'message': 'Wrong or missing station token'}), 401
    station = stations.get(station_id)
    if station is None:
        return jsonify({'success': False, 'message': 'No such station'}), 404
    name = undo_station_add(station)
    return jsonify({'success': bool(name), 'name': name, 'message': None if name else 'Nothing to undo'})


@app.route('/api/detection_status')
def get_detection_status():
    """Get current card detection status with detailed state information"""

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
