# ============================================================================
# FILE: server/pages.py
# The pages themselves, the camera's live view, thumbnails and the header's numbers.
# ============================================================================
from flask import Response, abort, jsonify, render_template, send_from_directory
import time

from config import Config
import games

from server.state import app, database, inventory, review, stations
from server.desks import desk, desk_for
from server.captures import scan_stats
from server.card_data import data_update_notices, data_updates_running


@app.route('/')
def index():
    """The cameras (stations), each with a link to its page - or, with a camera on this machine, its scanner page"""
    if Config.CAMERA_TYPE.lower() == 'remote':
        return render_template('stations.html')
    return render_template('scanner.html', station=None)


@app.route('/scan/<station_id>')
def scan_page(station_id):
    """A station's scanner page: its camera, its cards, its review queue"""
    if desk_for(station_id) is None:
        abort(404)
    return render_template('scanner.html', station=stations.get(station_id))


@app.context_processor
def page_links():
    """settings_url: where the camera / AI settings are - a scanner page (the first station's when there are several)"""
    if Config.CAMERA_TYPE.lower() != 'remote' or not stations or not stations.all():
        return {'settings_url': '/#settings'}
    return {'settings_url': f"/scan/{stations.all()[0]['id']}#settings"}


@app.route('/collection')
def collection():
    """Inventory management and deck building"""
    return render_template('collection.html')


def generate_frames(camera):
    """MJPEG stream of a camera's annotated live view (each new frame once; encoding is shared)"""
    last_id = -1
    while True:
        frame_id, jpeg = camera.get_stream_jpeg() if camera else (-1, None)
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
        generate_frames(desk().scanner),  # ?station= names the camera
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
            'review': review.count(game.id, desk().review_filter) if review else 0,
            # The scanner page's counters: what was scanned and not moved to the collection yet
            'inventory': scan_stats(game.id),
            'collection': inventory.get_stats(game.id)
        })

    return jsonify({'error': 'Components not initialized'}), 500
