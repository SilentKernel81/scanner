# ============================================================================
# FILE: server/card_data.py
# Card data: updating and rebuilding the card database from the web interface.
# ============================================================================
from flask import jsonify
from flask_socketio import emit
import threading
import time

from card_scanner import games

from card_scanner.core import image_index
from card_scanner.server.state import app, database, printing_check, socketio
from card_scanner.server.logs import logger
from card_scanner.server.lock import guarded


# Newer card data found by the update check: game id -> message (shown on the page)
data_update_notices = {}
data_updates_running = set()
# The last failure of a download that is being tried again (game id -> message); the pages say it
data_update_errors = {}
# Seconds to wait after each failure of the first download (the last is repeated): without card
# data nothing can be identified, so it is not given up - a network that is not there yet at
# start-up (a laptop, a Pi booting) is the usual reason
FIRST_DOWNLOAD_RETRIES = (60, 300, 900, 1800)


def start_card_data_update(game, retry=False):
    """
    Download a game's card data in the background (progress/complete/error events). retry: the
    first download, with no data to fall back on - a failure is tried again after a pause,
    for as long as the program runs (an update of data that is there is asked for again by the
    person, not repeated).
    """
    if game.id in data_updates_running:
        return False
    data_updates_running.add(game.id)

    def progress_callback(message):
        socketio.emit('database_update_progress', {'message': message})
        logger.info(f"Database update: {message}")

    def update_task():
        failures = 0
        try:
            while True:
                try:
                    logger.info(f"Updating {game.label} card data from {game.source}")
                    game.download(progress_callback)
                    total = game.card_count()
                    data_update_notices.pop(game.id, None)
                    data_update_errors.pop(game.id, None)
                    socketio.emit('database_update_complete', {'game': game.id, 'total_cards': total})
                    logger.info(f"{game.label} card data updated: {total} cards")
                    return
                except Exception as e:
                    logger.exception(f"Database update failed: {e}")
                    socketio.emit('database_update_error', {'game': game.id, 'message': str(e)})
                    if not retry or game.card_count():
                        data_update_errors.pop(game.id, None)
                        return
                    pause = FIRST_DOWNLOAD_RETRIES[min(failures, len(FIRST_DOWNLOAD_RETRIES) - 1)]
                    failures += 1
                    data_update_errors[game.id] = f"{e} - trying again in {pause // 60} min"
                    logger.warning(f"{game.label} card data: trying again in {pause} s")
                    time.sleep(pause)
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
# The picture index (core/image_index.py): a fingerprint of every printing's picture, for reading a card by its picture.
# It is extended from Scryfall's pictures when the person asks (Settings > Storage), after a card database update: only the printings
# that are new since are downloaded (a few seconds for a handful, hours the first time: about 12 GB for the whole game). One build at a
# time; it can be stopped, and started again where it stopped. The readers (printing_check.index) follow the new index by themselves.
# ============================================================================

index_state = {'running': False, 'stopping': False, 'done': 0, 'total': 0, 'error': None, 'result': None}
index_lock = threading.Lock()
index_cancel = threading.Event()


def index_status():
    """
    What the Settings page shows of the picture index: how many printings of the card database have a picture and how many of those are in the
    index (the rest is what the button would download), whether a build is running and how far it is, and how the last one ended.
    """
    printings = database.printing_images() if database else []
    pictured = [printing for printing, address in printings if address]
    index = printing_check.index() if printing_check else None
    indexed = sum(1 for printing in pictured if index is not None and index.row(printing) is not None)
    with index_lock:
        state = dict(index_state)
    return {'printings': len(pictured), 'indexed': indexed, 'missing': len(pictured) - indexed, 'built': index.built if index is not None else None,
            'running': state['running'], 'stopping': state['stopping'], 'done': state['done'], 'total': state['total'], 'error': state['error'],
            'result': state['result']}


def start_index_update():
    """Extend the picture index in the background; False when a build is running already"""
    with index_lock:
        if index_state['running']:
            return False
        index_state.update(running=True, stopping=False, done=0, total=0, error=None, result=None)
        index_cancel.clear()

    def progress(done, total):
        with index_lock:
            index_state.update(done=done, total=total)

    def task():
        error = result = None
        try:
            logger.info("Picture index: extending it from Scryfall's pictures")
            built = image_index.build(database.printing_images(), printing_check.index_folder, fetch=True, log=logger.info,
                                      progress=progress, cancel=index_cancel)
            kept, new, skipped = built
            reason = getattr(built, 'reason', 'done')          # image_index.BuildResult: 'done', 'cancelled' or 'refused'
            result = {'printings': kept, 'new': new, 'skipped': skipped, 'stopped': reason == 'cancelled'}
            if reason == 'refused':
                # Scryfall kept answering 403 / 429: what was done is kept (build wrote it), but this is not "updated" - it is to be tried again later
                error = (f"Scryfall kept refusing requests (a rate limit), so the build stopped after adding {new} new "
                         f"picture{'' if new == 1 else 's'}. What was done is kept - press the button again later and it goes on from there")
        except image_index.BuildRunning as e:
            error = f"{e} - wait for it to finish (a build started from the command line, perhaps)"
        except Exception as e:
            logger.exception(f"Picture index: the build failed: {e}")
            error = str(e)
        finally:
            with index_lock:
                index_state.update(running=False, stopping=False, error=error, result=result)

    threading.Thread(target=task, daemon=True, name='index-build').start()
    return True


@app.route('/api/image_index')
def image_index_state():
    """The picture index: its coverage of the card database, and a running build's progress"""
    return jsonify({'success': True, **index_status()})


@app.route('/api/image_index/update', methods=['POST'])
@guarded
def image_index_update():
    """Download the pictures of the printings that are not in the index yet and fingerprint them (background; the state says how far)"""
    if not database or not printing_check:
        return jsonify({'success': False, 'message': 'Card data or the printing check is not ready'}), 503
    if not start_index_update():
        return jsonify({'success': False, 'message': 'The picture index is being built already'}), 409
    return jsonify({'success': True, **index_status()})


@app.route('/api/image_index/stop', methods=['POST'])
@guarded
def image_index_stop():
    """Stop a running build after the printing it is on: what was done is kept, the next update goes on from there"""
    with index_lock:
        running = index_state['running']
        if running:
            index_state['stopping'] = True
    if running:
        index_cancel.set()
    return jsonify({'success': True, **index_status()})
