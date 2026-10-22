# ============================================================================
# FILE: server/card_data.py
# Card data: updating and rebuilding the card database from the web interface.
# ============================================================================
from datetime import datetime, timedelta
from flask import jsonify
from flask_socketio import emit
import threading
import time

from card_scanner import games
from card_scanner.config import Config

from card_scanner.core import image_index
from card_scanner.server.state import app, app_settings, database, printing_check, socketio
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


def start_card_data_update(game, retry=False, on_done=None):
    """
    Download a game's card data in the background (progress/complete/error events). retry: the
    first download, with no data to fall back on - a failure is tried again after a pause,
    for as long as the program runs (an update of data that is there is asked for again by the
    person, or by the schedule, not repeated). on_done(ok, message): called once when the
    download has ended, either way (the schedule remembers how its own attempt ended).
    """
    if game.id in data_updates_running:
        return False
    data_updates_running.add(game.id)

    def finished(ok, message):
        # The caller's callback must never turn the end of a download into a crash of its thread
        if on_done:
            try:
                on_done(ok, message)
            except Exception as e:
                logger.exception(f"Card data update: the end of the download could not be noted: {e}")

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
                    finished(True, f"{total:,} cards")
                    extend_index_after_update()
                    return
                except Exception as e:
                    logger.exception(f"Database update failed: {e}")
                    socketio.emit('database_update_error', {'game': game.id, 'message': str(e)})
                    if not retry or game.card_count():
                        data_update_errors.pop(game.id, None)
                        finished(False, str(e))
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


# ============================================================================
# The automatic update of the card data (docs/plans/UPDATE_SCHEDULE_PLAN.md, phase 1). Built like the automatic backup
# (backups_api.run_backup_schedule): no clock time, a loop that asks every few minutes whether a download is DUE - the
# last one is the chosen number of hours old or older. A laptop or a Pi that was off when it was due catches up once it
# runs again, which an "at 3 a.m." would not. Off by default: until now updates were never started without the person.
# ============================================================================

CARD_DATA_SCHEDULE_HOURS = (0, 24, 72, 168, 720)  # 0: off; a day, three days, a week, a month
SCHEDULE_TICK = 600               # seconds between two "is anything due?" questions
SCHEDULE_FIRST_DOWNLOAD = 300     # no automatic download in the first minutes: a Pi's or a laptop's network may not be up yet
UPDATE_CHECK_EVERY = 24 * 3600    # the update check (the notice on the page) stays once a day
AUTO_RETRY_AFTER = timedelta(hours=1)  # an automatic download that failed is not tried again before this (Scryfall down)
AUTO_LAST_KEY = 'card_data_auto_last'  # data/settings.json: {game id: {'at', 'ok', 'message'}} of the last automatic attempt


def card_data_schedule():
    """Every how many hours the card data is downloaded on its own (0: off) - the person's choice, else config.yaml's"""
    value = app_settings.get('card_data_every_hours', Config.DATABASE_AUTO_UPDATE_HOURS) if app_settings else Config.DATABASE_AUTO_UPDATE_HOURS
    # A hand-edited data/settings.json or config.yaml with another number (or a true / "24") is off, never a guess:
    # downloading on its own is something the person must have chosen
    if isinstance(value, bool) or value not in CARD_DATA_SCHEDULE_HOURS:
        return 0
    return value


def last_auto_attempt(game_id):
    """{'at' (ISO time), 'ok' (None while it runs), 'message'} of the last automatic download of a game, or None"""
    attempts = app_settings.get(AUTO_LAST_KEY) if app_settings else None
    attempt = attempts.get(game_id) if isinstance(attempts, dict) else None
    return attempt if isinstance(attempt, dict) else None


def note_auto_attempt(game_id, at, ok=None, message=''):
    """Remember an automatic download in data/settings.json - kept across a restart, so the back-off and 'when next' are too"""
    attempts = app_settings.get(AUTO_LAST_KEY)
    attempts = dict(attempts) if isinstance(attempts, dict) else {}  # a copy: the settings object saves what it is given
    attempts[game_id] = {'at': at.isoformat(timespec='seconds'), 'ok': ok, 'message': message}
    app_settings.set(AUTO_LAST_KEY, attempts)


def _parse_time(text):
    """A time written by isoformat (card_data_info, the attempts above), or None for anything else"""
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def card_data_due(game, now, every_hours):
    """
    Whether the schedule should download this game's card data now: the schedule is on, the data in use was downloaded
    every_hours ago or longer (by anyone - a press of the button counts too), and no automatic attempt was made in the last
    hour (a download that keeps failing would otherwise be tried at every tick). The times are local and naive, as
    card_data_info's downloaded_at is written (database.set_data_info: datetime.now()).
    """
    if not every_hours:
        return False
    info = database.get_data_info(game.id) or {}
    downloaded = _parse_time(info.get('downloaded_at'))
    # Data from before card_data_info existed has no date: it is old enough
    if downloaded is not None and now - downloaded < timedelta(hours=every_hours):
        return False
    attempt = _parse_time((last_auto_attempt(game.id) or {}).get('at'))
    if attempt is not None and now - attempt < AUTO_RETRY_AFTER:
        return False
    return True


def scheduled_card_data_updates(now=None):
    """
    Start the automatic download of every game whose card data is due (card_data_due); the ids of the games started.
    Only games that have data: the first download is startup's, with its own retries (FIRST_DOWNLOAD_RETRIES). A download
    running already (the button, the first download) is left alone - start_card_data_update refuses a second.
    """
    every_hours = card_data_schedule()
    if not every_hours or not database or not app_settings:
        return []
    now = now or datetime.now()
    started = []
    for game in games.all_games():
        try:
            if not game.card_count() or game.id in data_updates_running or not card_data_due(game, now, every_hours):
                continue

            # How it ended goes beside the time it began: the same game's entry, written again
            def done(ok, message, game_id=game.id, at=now):
                note_auto_attempt(game_id, at, ok, message)

            # The attempt is noted BEFORE the download starts: a server stopped in the middle still counts it for the back-off
            note_auto_attempt(game.id, now)
            if start_card_data_update(game, on_done=done):
                logger.info(f"{game.label} card data: automatic update (every {every_hours} h)")
                started.append(game.id)
        except Exception as e:
            # One game's trouble (its database, its settings) must not keep the others from their update
            logger.exception(f"{game.label} card data: the automatic update could not start: {e}")
    return started


def run_update_schedule():
    """
    The update check (the notice: 10 s after start, then once a day, as before) and the automatic download (every
    SCHEDULE_TICK, from SCHEDULE_FIRST_DOWNLOAD after start) on one thread. Nothing that fails in a tick ends the loop.
    """
    def loop():
        started = time.monotonic()
        last_check = None
        time.sleep(10)
        while True:
            try:
                if last_check is None or time.monotonic() - last_check >= UPDATE_CHECK_EVERY:
                    last_check = time.monotonic()
                    check_card_data_updates()
                if time.monotonic() - started >= SCHEDULE_FIRST_DOWNLOAD:
                    scheduled_card_data_updates()
            except Exception as e:
                logger.error(f"The automatic card data update failed: {e}", exc_info=True)
            time.sleep(SCHEDULE_TICK)
    threading.Thread(target=loop, daemon=True, name='update-schedule').start()


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
# It is extended from Scryfall's pictures when the person asks (Settings > Card reading > Picture), after a card database update: only the printings
# that are new since are downloaded (a few seconds for a handful, hours the first time: about 12 GB for the whole game). One build at a
# time; it can be stopped, and started again where it stopped. The readers (printing_check.index) follow the new index by themselves.
# ============================================================================

index_state = {'running': False, 'stopping': False, 'done': 0, 'total': 0, 'error': None, 'result': None}
index_lock = threading.Lock()
index_cancel = threading.Event()


def extend_index_wanted():
    """Whether the person (or config.yaml's reading.extend_index_after_update) wants the picture index extended after a card data download; their choice wins"""
    value = app_settings.get('reading_extend_index', Config.READING_EXTEND_INDEX) if app_settings else Config.READING_EXTEND_INDEX
    return value is True


def extend_index_after_update():
    """
    Called after a card data download that SUCCEEDED: the new printings have no picture in the index yet, and until they do the picture alone adds nothing
    (PrintingCheck.missing_printings), so extend it now rather than wait for someone to press the button. Never a first build: with no index (or no printing
    check) it does nothing - that is hours and ~12 GB, started only by the button. Nothing here may turn a finished update into a failed one, so it never raises.
    """
    try:
        if not printing_check or printing_check.index() is None or not extend_index_wanted():
            return False
        started = start_index_update()                # False: a build is running already, which is the extension itself
        if started:
            logger.info("Picture index: extending it after the card data update")
        return started
    except Exception as e:
        logger.exception(f"Picture index: could not start extending it after the card data update: {e}")
        return False


def index_progress():
    """A running build's progress, for the banner: {'running', 'done', 'total'} (cheap: no database, no index read)"""
    with index_lock:
        return {'running': bool(index_state['running']), 'done': index_state['done'], 'total': index_state['total']}


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
