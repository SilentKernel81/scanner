# ============================================================================
# FILE: server/card_data.py
# Card data: updating the card database from the web interface (and on a schedule), and the picture index.
# ============================================================================
from datetime import datetime, timedelta
from flask import jsonify, request
from flask_socketio import emit
import threading
import time
import uuid

from card_scanner import games
from card_scanner.config import Config

from card_scanner.core import image_index, prices, shop_prices
from card_scanner.server.state import app, app_settings, database, inventory, maintenance_barrier, printing_check, scan_inventory, socketio
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
                    change = begin_price_change()   # the download changes the shown prices: the entries follow (reprice_collections)
                    try:
                        game.download(progress_callback)
                    except Exception:
                        price_change_dropped(change)    # nothing was swapped in (the swap is the download's last step)
                        raise
                    price_change_done(change)
                    total = game.card_count()
                    data_update_notices.pop(game.id, None)
                    data_update_errors.pop(game.id, None)
                    socketio.emit('database_update_complete', {'game': game.id, 'total_cards': total})
                    logger.info(f"{game.label} card data updated: {total} cards")
                    finished(True, f"{total:,} cards")
                    reprice_collections(game)   # an entry without a price today keeps its own (unless a change of source is unfinished)
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


# Every repricing of the entries, and every change of price source with the repricing it needs, one at a time. Each
# repricing works out the new prices from the shown columns as they are and writes them: a change of source between the
# two (another thread) would otherwise be overwritten with the prices of the source before it (a Codex review of
# 2026-10-24). An RLock: the change of source reprices while it holds it
pricing_lock = threading.RLock()
# data/settings.json: {'sources': [source ids]} while a change of source has not finished repricing both inventories -
# the source it came from and EVERY source chosen since: a change that failed half-way may have written any of their prices
# (a Codex review of 2026-10-24: remembering only the first, a change back to it kept euros of the half-done change as
# dollars). Until it has finished, every repricing finishes it, also after a failure or a restart (resume_price_transition)
PRICE_TRANSITION_KEY = 'price_transition'


# data/settings.json: {token: 'running' | 'done'} - one entry per change of the shown prices that the entries must follow
# (a card data download, a shops' price update, a repricing itself). 'running' from BEFORE the change begins, 'done' once it
# is in; a repricing forgets only the entries that were 'done' when it began, and only once both inventories are priced.
# A change still running when a repricing (a change of source, say) begins keeps its entry, and its own repricing follows
# its swap - a single flag was cleared by that earlier repricing, and the swap's repricing skipped (a Codex review of
# 2026-10-24). While any entry is 'done', an update that finds nothing new still reprices; at start every entry counts as
# done (resume_price_transition) - one stopped before its swap costs a repricing that changes nothing
REPRICE_PENDING_KEY = 'reprice_pending'
pending_lock = threading.Lock()      # the read-modify-write of that entry in data/settings.json


def _durable(key, value):
    """A setting that must be on disk before the change it records goes on (Settings.set_durably: a failed write raises).
    Only for what comes BEFORE a change - a write after one that fails leaves a note behind, and the note is safe"""
    setter = getattr(app_settings, 'set_durably', None)
    (setter or app_settings.set)(key, value)


def _pending():
    entries = app_settings.get(REPRICE_PENDING_KEY) if app_settings else None
    return dict(entries) if isinstance(entries, dict) else {}


def _set_pending(token, state):
    """state 'running' / 'done', or None to forget the token. 'running' is written durably: it is noted before the change
    it stands for, and a change that a restart could not know about must not begin (_durable raises - the change fails)"""
    if not app_settings:
        return
    with pending_lock:
        entries = _pending()
        if state is None:
            entries.pop(token, None)
        else:
            entries[token] = state
        (_durable if state == 'running' else app_settings.set)(REPRICE_PENDING_KEY, entries)


def begin_price_change():
    """A change the entries must follow is about to begin: its token, noted as running"""
    token = uuid.uuid4().hex
    _set_pending(token, 'running')
    return token


def price_change_done(token):
    """The change is in (the new prices shown): the next repricing follows it"""
    _set_pending(token, 'done')


def price_change_dropped(token):
    """The change did not happen (a download that failed): nothing to follow"""
    _set_pending(token, None)


def reprice_pending():
    """Whether a change that is in has not been followed by a finished repricing yet"""
    return 'done' in _pending().values()


def transition_done(name):
    """Note that one inventory ('collection', 'scan') is repriced in an unfinished change of price source"""
    pending = app_settings.get(PRICE_TRANSITION_KEY) if app_settings else None
    if isinstance(pending, dict):
        done = [store for store in pending.get('done') or [] if isinstance(store, str)]
        if name not in done:
            app_settings.set(PRICE_TRANSITION_KEY, {**pending, 'done': done + [name]})


def prices_withheld(name):
    """
    Whether an inventory's prices may be of another currency than the one shown: an unfinished change of price source went
    through another currency, and has not repriced that inventory yet. InventoryManager.prices_withheld asks this (set in
    startup.py): its entries then show no price and its totals 0 - never dollars as euros (a Codex review of 2026-10-24) -
    until the next repricing finishes the change (reprice_collections; the schedule's tick tries every 10 minutes).
    """
    went_through = price_transition()
    if not went_through or not database:
        return False
    pending = app_settings.get(PRICE_TRANSITION_KEY)
    if name in ((pending.get('done') or []) if isinstance(pending, dict) else []):
        return False
    shown = prices.source(database.price_source)['currency']
    return any(prices.source(source)['currency'] != shown for source in went_through)


def current_card_price(game_id, card_id, finish):
    """InventoryManager.current_price: a printing's price in a finish in the card data as it is now (the chosen source),
    or None when the game or the printing is not there (the price given is kept)"""
    game = games.get(game_id) if game_id else None
    return game.current_price(card_id, finish) if game else None


def finish_unfinished_repricing():
    """The schedule's tick: a repricing that failed (a change of source half-done, new prices not followed) is tried again
    - nothing schedules it otherwise until the next update or start"""
    if price_transition() is not None or reprice_pending():
        reprice_collections(games.active())


def price_transition():
    """The sources an unfinished change of price source went through (PRICE_TRANSITION_KEY), the first the one it came
    from; None when there is none"""
    pending = app_settings.get(PRICE_TRANSITION_KEY) if app_settings else None
    sources = pending.get('sources') if isinstance(pending, dict) else None
    sources = [source for source in sources if prices.known(source)] if isinstance(sources, list) else []
    return sources or None


def reprice_collections(game, strict=False):
    """
    After a card data update, a price update or a change of price source: the collection's and the scanned cards' entries
    take their printing's price in the card data as it is now (Game.price_changes). Until 2026-10-23 an entry kept the
    price of the day it was added, for good. While a change of source has not finished (price_transition), the entries
    the source now chosen cannot price lose their price - any of them may hold the number of a source the change went
    through - and, if any of those sources had another currency, so do the entries that do not know their printing. The
    change counts as finished only once both inventories are done. Not the digital collection (Arena cards have no price,
    on purpose: arena_api.py) nor other people's (theirs is what they offered). Under pricing_lock. A failure is logged
    and leaves the old prices (and an unfinished change still to be finished); strict: it is raised, for the change of
    source to answer it. Returns (entries priced again, entries whose price was taken away).
    """
    with pricing_lock:
        try:
            # This repricing's own entry, and the changes it follows: those done before it began - forgotten below, only once
            # both inventories are priced; a change still running keeps its entry (its own repricing comes after it)
            own = begin_price_change()
            price_change_done(own)
            followed = [token for token, state in _pending().items() if state == 'done']
            went_through = price_transition()
            chosen = database.price_source
            # Unfinished: whatever an entry holds may be any of these sources' number - kept only where the chosen one has
            # its own (conservative: a change back to where it began also clears what that source cannot price)
            new_source = went_through is not None
            new_currency = new_source and any(prices.source(source)['currency'] != prices.source(chosen)['currency']
                                              for source in went_through)
            changed = cleared = 0
            # Both inventories in ONE pass inside the barrier, from the first price worked out to the notes forgotten: copies
            # that move between them (scanned cards added to the collection: take_from, under the barrier too) must not go
            # from the inventory not priced yet to the one priced already - they would keep their old price, maybe in the
            # old currency, and the change be noted as finished (a Codex review of 2026-10-24). It also keeps a full backup
            # (backups.create_full) from copying half a repricing. Lock order: pricing_lock, then the barrier - always
            with maintenance_barrier:
                for name, store in (('collection', inventory), ('scan', scan_inventory)):
                    if store:
                        # Its lock from the entries read to their prices written: no edit in between (InventoryManager.exclusive).
                        # Only the card data is read meanwhile, with no SQLite write lock held: no cycle with a change of source
                        with store.exclusive():
                            new_prices, unpriced = game.price_changes(store, new_source, new_currency)
                            # One transaction per inventory: done whole (and noted so - its prices are shown again) or not at all
                            priced, taken = store.apply_prices(new_prices, unpriced)
                            if went_through is not None:
                                # Noted under the same lock the readers ask "withheld?" under, with the rows they read
                                # (InventoryManager.get_stats / get_all_cards): new prices and "shown again" are one moment
                                transition_done(name)
                        changed, cleared = changed + priced, cleared + taken
                if went_through is not None:
                    app_settings.set(PRICE_TRANSITION_KEY, None)    # both inventories done: the change is finished
                for token in followed:
                    price_change_dropped(token)
            logger.info(f"{game.label}: {changed} entries priced again" + (f", {cleared} without a price in the new source" if cleared else ''))
            if changed or cleared:
                # The pages that show totals or lists reload them (scanner.js, collection.js, decks.js)
                socketio.emit('inventory_prices_updated', {'stats': scan_inventory.get_stats(game.id)}, namespace='/')
            return changed, cleared
        except Exception as e:
            logger.exception(f"{game.label}: the entries could not be priced again: {e}")
            if strict:
                raise
            return 0, 0


def resume_price_transition():
    """At start: a change of price source, or a repricing, that a failure or a stop left unfinished is finished (in the
    background)"""
    for token, state in _pending().items():
        if state == 'running':
            price_change_done(token)         # stopped before or after its change: priced again either way, to be sure
    if price_transition() is not None or reprice_pending():
        logger.info(f"Prices: finishing an unfinished repricing" + (f" (the change through {', '.join(price_transition())} to "
                                                                   f"{database.price_source})" if price_transition() else ''))
        threading.Thread(target=reprice_collections, args=(games.active(),), daemon=True, name='price-transition').start()


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


def restore_auto_attempt(game_id, previous):
    """Put back a game's last automatic attempt as it was (previous: last_auto_attempt's answer, None when there was none)"""
    attempts = app_settings.get(AUTO_LAST_KEY)
    attempts = dict(attempts) if isinstance(attempts, dict) else {}
    if previous is None:
        attempts.pop(game_id, None)
    else:
        attempts[game_id] = previous
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
            previous = last_auto_attempt(game.id)
            note_auto_attempt(game.id, now)
            if start_card_data_update(game, on_done=done):
                logger.info(f"{game.label} card data: automatic update (every {every_hours} h)")
                started.append(game.id)
            else:
                # A download began between the question and now (the button): not the schedule's attempt - the last real one is put back
                restore_auto_attempt(game.id, previous)
        except Exception as e:
            # One game's trouble (its database, its settings) must not keep the others from their update
            logger.exception(f"{game.label} card data: the automatic update could not start: {e}")
    return started


def schedule_tick(check_due, downloads_due):
    """
    One turn of the loop below. check_due: time for the update check (the notice); downloads_due: past the first minutes
    after start. Each of the three is asked on its own: one failing (logged) never keeps the others from their turn. The
    card data comes before the index: a download it just started makes the index wait for it (index_due).
    """
    steps = [('update check', check_card_data_updates)] if check_due else []
    if downloads_due:
        steps += [('automatic card data update', scheduled_card_data_updates), ('automatic picture index update', scheduled_index_update),
                  ('automatic price update', scheduled_shop_price_update), ('unfinished repricing', finish_unfinished_repricing)]
    for name, step in steps:
        try:
            step()
        except Exception as e:
            logger.error(f"The {name} failed: {e}", exc_info=True)


def run_update_schedule():
    """
    The update check (the notice: 10 s after start, then once a day, as before), the automatic card data download and the
    picture index's retry (every SCHEDULE_TICK, from SCHEDULE_FIRST_DOWNLOAD after start) on one thread. Nothing that fails
    in a tick ends the loop (schedule_tick).
    """
    def loop():
        started = time.monotonic()
        last_check = None
        time.sleep(10)
        while True:
            check_due = last_check is None or time.monotonic() - last_check >= UPDATE_CHECK_EVERY
            if check_due:
                last_check = time.monotonic()
            schedule_tick(check_due, time.monotonic() - started >= SCHEDULE_FIRST_DOWNLOAD)
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


# ============================================================================
# The picture index (core/image_index.py): a fingerprint of every printing's picture, for reading a card by its picture.
# It is extended from Scryfall's pictures when the person asks (Settings > Card data), after a card database update: only the printings
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


def start_index_update(on_done=None):
    """
    Extend the picture index in the background; False when a build is running already. on_done(ok, message): called once
    when the build has ended - ok is True when it finished, False when it failed, was refused by Scryfall or was stopped,
    None when it never began because a build started elsewhere (the command line) holds the index (the schedule's skip).
    """
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
        busy = False    # image_index.BuildRunning: another process is building - not this build's failure
        refused = False  # Scryfall kept refusing: told to the schedule in its own words (the result's shape is the page's, and pinned)
        try:
            logger.info("Picture index: extending it from Scryfall's pictures")
            built = image_index.build(database.printing_images(), printing_check.index_folder, fetch=True, log=logger.info,
                                      progress=progress, cancel=index_cancel)
            kept, new, skipped = built
            reason = getattr(built, 'reason', 'done')          # image_index.BuildResult: 'done', 'cancelled' or 'refused'
            result = {'printings': kept, 'new': new, 'skipped': skipped, 'stopped': reason == 'cancelled'}
            if reason == 'refused':
                refused = True
                # Scryfall kept answering 403 / 429: what was done is kept (build wrote it), but this is not "updated" - it is to be tried again later
                error = (f"Scryfall kept refusing requests (a rate limit), so the build stopped after adding {new} new "
                         f"picture{'' if new == 1 else 's'}. What was done is kept - press the button again later and it goes on from there")
        except image_index.BuildRunning as e:
            busy = True
            error = f"{e} - wait for it to finish (a build started from the command line, perhaps)"
        except Exception as e:
            logger.exception(f"Picture index: the build failed: {e}")
            error = str(e)
        finally:
            with index_lock:
                index_state.update(running=False, stopping=False, error=error, result=result)
        if on_done:
            # After the state is written: whoever is told sees the build as ended. Never allowed to raise into the thread
            if busy:
                ok, message = None, error
            elif refused:
                # Worded for the schedule: the error above tells the person to press the button, which the schedule does not need
                ok, message = False, (f"Scryfall kept refusing requests (a rate limit) after {result['new']} new "
                                      f"picture{'' if result['new'] == 1 else 's'} - what was done is kept, and the next run goes on from there")
            elif error:
                ok, message = False, error
            elif result and result['stopped']:
                ok, message = False, f"stopped after {result['new']} new picture{'' if result['new'] == 1 else 's'}"
            else:
                ok, message = True, f"{result['new']} new picture{'' if result['new'] == 1 else 's'}"
            try:
                on_done(ok, message)
            except Exception as e:
                logger.exception(f"Picture index: the end of the build could not be noted: {e}")

    threading.Thread(target=task, daemon=True, name='index-build').start()
    return True


# ----------------------------------------------------------------------------
# The picture index's automatic retry (docs/plans/UPDATE_SCHEDULE_PLAN.md, phase 2). The index already follows the card data
# (extend_index_after_update, after every download that succeeded), so its own schedule is not a second timer beside that one:
# it goes back to what is still MISSING - a build Scryfall refused half-way (its rate limit), one stopped, one that failed -
# at most once per the chosen number of hours. It never makes a first build, nor finishes one that was stopped early: that
# is hours and ~12 GB, and stays the button's.
# ----------------------------------------------------------------------------

INDEX_SCHEDULE_HOURS = (0, 6, 24, 168)     # 0: off; every 6 hours, a day, a week
INDEX_AUTO_MIN_COVERAGE = 0.9              # below this share of the printings indexed, the index is a first build not finished: not the schedule's
INDEX_AUTO_LAST_KEY = 'image_index_auto_last'  # data/settings.json: {'at', 'ok', 'message'} of the last automatic build


def index_schedule():
    """Every how many hours the picture index goes back to what is missing (0: off) - the person's choice, else config.yaml's"""
    value = app_settings.get('image_index_every_hours', Config.READING_INDEX_RETRY_HOURS) if app_settings else Config.READING_INDEX_RETRY_HOURS
    # As card_data_schedule: anything but one of the listed numbers is off - a download nobody chose is never guessed
    if isinstance(value, bool) or value not in INDEX_SCHEDULE_HOURS:
        return 0
    return value


def last_index_attempt():
    """{'at', 'ok' (None while it runs, or when another build held the index), 'message'} of the last automatic build, or None"""
    attempt = app_settings.get(INDEX_AUTO_LAST_KEY) if app_settings else None
    return attempt if isinstance(attempt, dict) else None


def note_index_attempt(at, ok=None, message=''):
    """Remember an automatic build in data/settings.json (kept across a restart, like the card data's)"""
    app_settings.set(INDEX_AUTO_LAST_KEY, {'at': at.isoformat(timespec='seconds'), 'ok': ok, 'message': message})


def index_due(now, every_hours):
    """
    Whether the schedule should extend the picture index now. The cheap questions first, the count of what is missing (every
    printing of the card data against the index) last:
    - the schedule is on, the printing check and an index are there (no index: never a first build), no build is running;
    - no card data download is running - when it succeeds it extends the index itself (extend_index_after_update);
    - the last automatic build began every_hours ago or longer;
    - something is missing, and the index covers at least INDEX_AUTO_MIN_COVERAGE of the printings.
    """
    if not every_hours or not database or not printing_check or printing_check.index() is None:
        return False
    if data_updates_running:
        return False
    with index_lock:
        if index_state['running']:
            return False
    attempt = _parse_time((last_index_attempt() or {}).get('at'))
    if attempt is not None and now - attempt < timedelta(hours=every_hours):
        return False
    status = index_status()
    if not status['missing'] or not status['printings']:
        return False
    return status['indexed'] / status['printings'] >= INDEX_AUTO_MIN_COVERAGE


def scheduled_index_update(now=None):
    """Start the automatic build when it is due (index_due); True when one was started"""
    every_hours = index_schedule()
    if not every_hours or not app_settings:
        return False
    now = now or datetime.now()
    if not index_due(now, every_hours):
        return False

    previous = last_index_attempt()

    def done(ok, message):
        if ok is None:
            # Never began: another process (a build from the command line) held the index. That is not an attempt of the
            # schedule's, so the last real one is put back - else the interval (a week, perhaps) would start from a build that
            # never ran, and the printings that build leaves missing would wait for it. The next tick asks again
            app_settings.set(INDEX_AUTO_LAST_KEY, previous)
            logger.info(f"Picture index: the automatic update did not begin - {message}")
            return
        note_index_attempt(now, ok, message)

    # Noted before it starts, as the card data's: a server stopped mid-build still counts the attempt
    note_index_attempt(now)
    if not start_index_update(on_done=done):
        # A build began between the question and now (the button): that is the extension itself, and not the schedule's
        app_settings.set(INDEX_AUTO_LAST_KEY, previous)
        return False
    logger.info(f"Picture index: automatic update of what is missing (every {every_hours} h)")
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


# ============================================================================
# The shops' prices (core/shop_prices.py): MTGJSON's file of the day - Card Kingdom's and Mana Pool's prices - downloaded
# when the person asks (Settings > Card data > Prices) or on its own schedule (docs/plans/PRICE_SOURCES_PLAN.md, phase 3).
# About 16 MB; nothing is downloaded while MTGJSON's date is the one in use. A shop's source in use is applied again and
# the entries priced again when new prices come in.
# ============================================================================

shop_state = {'running': False, 'message': '', 'error': None, 'result': None}
shop_lock = threading.Lock()
PRICES_SCHEDULE_HOURS = (0, 24, 72, 168)       # 0: off; a day, three days, a week
PRICES_AUTO_LAST_KEY = 'shop_prices_auto_last'  # data/settings.json: {'at', 'ok', 'message'} of the last automatic update


def shop_prices_status():
    """The shops' prices: MTGJSON's date of those in use, when they came, how many printings, and an update's progress"""
    info = (database.get_data_info(shop_prices.INFO_KEY) if database else None) or {}
    with shop_lock:
        state = dict(shop_state)
    return {**state, 'date': info.get('source_updated'), 'downloaded_at': _shown(_parse_time(info.get('downloaded_at'))),
            'printings': database.shop_priced_printings() if database else 0}


def start_shop_price_update(force=False, on_done=None):
    """
    Download the shops' prices of the day in the background (shop_prices.import_prices); False when an update runs
    already. on_done(ok, message) once at the end - ok True also when MTGJSON had nothing newer. After new prices: when a
    shop's source is the one shown, the entries are priced again (its prices are applied by replace_shop_prices itself).
    """
    with shop_lock:
        if shop_state['running']:
            return False
        shop_state.update(running=True, message='Starting', error=None, result=None)

    def progress(message):
        with shop_lock:
            shop_state['message'] = message
        logger.info(f"Shop prices: {message}")

    def task():
        ok, message = False, ''
        try:
            change = begin_price_change()       # the swap may change the shown prices: noted before it (REPRICE_PENDING_KEY)
            try:
                date, count = shop_prices.import_prices(database, progress, force=force)
                ok, message = True, f"Prices of {date}: {count:,} printings"
                price_change_done(change)       # in, whatever the source is now (it may have changed during the download)
            except shop_prices.NothingNew as e:
                ok, message = True, str(e)
                price_change_dropped(change)
            except Exception:
                price_change_dropped(change)    # nothing was swapped in: replace_shop_prices is whole or nothing
                raise
            if reprice_pending() or price_transition() is not None:
                # New prices in, or an earlier repricing unfinished (also when MTGJSON had nothing new): a failure here is
                # the update's failure, and the note stays for the next one
                try:
                    reprice_collections(games.active(), strict=True)
                except Exception as e:
                    ok, message = False, f"The prices are in, but the cards could not be priced again ({e}): tried again at the next update"
        except Exception as e:
            logger.exception(f"Shop prices: the update failed: {e}")
            message = str(e)
        finally:
            with shop_lock:
                shop_state.update(running=False, message='', error=None if ok else message, result=message)
            if on_done:
                try:
                    on_done(ok, message)
                except Exception as e:
                    logger.exception(f"Shop prices: the end of the update could not be noted: {e}")

    threading.Thread(target=task, daemon=True, name='shop-prices').start()
    return True


def prices_schedule():
    """Every how many hours the shops' prices are downloaded on their own (0: off, the default) - the person's choice"""
    value = app_settings.get('prices_every_hours', 0) if app_settings else 0
    # As card_data_schedule: anything but one of the listed numbers is off
    if isinstance(value, bool) or value not in PRICES_SCHEDULE_HOURS:
        return 0
    return value


def last_price_attempt():
    attempt = app_settings.get(PRICES_AUTO_LAST_KEY) if app_settings else None
    return attempt if isinstance(attempt, dict) else None


def shop_prices_due(now, every_hours):
    """
    Whether the schedule should download the shops' prices now: on; no price update and no card data download running;
    the prices in use came every_hours ago or longer (or there are none); no automatic attempt in the last hour. MTGJSON's
    new file is out by about 06:15 UTC (2026-10-23 and -24; its FAQ says 9:00 AM EST): a try before it finds MTGJSON's
    date unchanged, costs one small request (Meta.json), and the hour's back-off brings the next try.
    """
    if not every_hours or not database:
        return False
    with shop_lock:
        if shop_state['running']:
            return False
    if data_updates_running:
        return False
    came = _parse_time((database.get_data_info(shop_prices.INFO_KEY) or {}).get('downloaded_at'))
    if came is not None and now - came < timedelta(hours=every_hours):
        return False
    attempt = _parse_time((last_price_attempt() or {}).get('at'))
    return attempt is None or now - attempt >= AUTO_RETRY_AFTER


def scheduled_shop_price_update(now=None):
    """Start the automatic price update when it is due (shop_prices_due); True when one was started"""
    every_hours = prices_schedule()
    if not every_hours or not app_settings:
        return False
    now = now or datetime.now()
    if not shop_prices_due(now, every_hours):
        return False
    previous = last_price_attempt()

    def done(ok, message):
        app_settings.set(PRICES_AUTO_LAST_KEY, {'at': now.isoformat(timespec='seconds'), 'ok': ok, 'message': message})

    # Noted before it starts, as the card data's: a server stopped in the middle still counts it for the back-off
    app_settings.set(PRICES_AUTO_LAST_KEY, {'at': now.isoformat(timespec='seconds'), 'ok': None, 'message': ''})
    if not start_shop_price_update(on_done=done):
        app_settings.set(PRICES_AUTO_LAST_KEY, previous)    # the button's update began meanwhile: not the schedule's
        return False
    logger.info(f"Shop prices: automatic update (every {every_hours} h)")
    return True


@app.route('/api/prices/update', methods=['POST'])
@guarded
def shop_price_update():
    """Download the shops' prices of the day (background; GET /api/prices/source says how far). Guarded, like the other
    downloads the Settings page starts (image_index_update): it changes every entry's price when a shop is the source"""
    if not database:
        return jsonify({'success': False, 'message': 'The card data is not ready'}), 503
    if not start_shop_price_update(force=bool((request.get_json(silent=True) or {}).get('force'))):
        return jsonify({'success': False, 'message': 'The prices are being updated already'}), 409
    return jsonify({'success': True, **price_source_state()})


# ============================================================================
# The two schedules on the Settings page (docs/plans/UPDATE_SCHEDULE_PLAN.md, phase 3): what they are, how the last automatic
# run ended and when the next is due; and the change, which needs the settings password - turning on downloads the server
# makes on its own is the same kind of decision as the picture index's button (image_index_update), which is guarded too.
# ============================================================================

def _shown(moment):
    """A time for the page: the server's local time with its offset, so a browser in another time zone (a server in Docker
    runs in UTC) shows it in its own. None stays None"""
    return moment.astimezone().isoformat(timespec='seconds') if moment else None


def _shown_attempt(attempt):
    """A remembered attempt with its time made ready for the page"""
    if not attempt:
        return None
    return {**attempt, 'at': _shown(_parse_time(attempt.get('at')))}


def card_data_next_due(game, every_hours):
    """
    When the schedule will next download a game's card data (card_data_due's two rules turned into a time), or None when it
    is off. A time already past means at the next look (SCHEDULE_TICK) - the page says "soon".
    """
    if not every_hours:
        return None
    downloaded = _parse_time((database.get_data_info(game.id) or {}).get('downloaded_at')) if database else None
    attempt = _parse_time((last_auto_attempt(game.id) or {}).get('at'))
    times = [moment for moment in (downloaded and downloaded + timedelta(hours=every_hours),
                                   attempt and attempt + AUTO_RETRY_AFTER) if moment]
    return max(times) if times else datetime.now()


def index_next_due(every_hours):
    """When the schedule may next go back to the picture index (whether there is anything missing is the page's own count), or None when off"""
    if not every_hours:
        return None
    attempt = _parse_time((last_index_attempt() or {}).get('at'))
    return attempt + timedelta(hours=every_hours) if attempt else datetime.now()


def prices_next_due(every_hours):
    """When the schedule will next look at MTGJSON's prices (shop_prices_due's rules turned into a time), or None when off"""
    if not every_hours:
        return None
    came = _parse_time((database.get_data_info(shop_prices.INFO_KEY) or {}).get('downloaded_at')) if database else None
    attempt = _parse_time((last_price_attempt() or {}).get('at'))
    times = [moment for moment in (came and came + timedelta(hours=every_hours), attempt and attempt + AUTO_RETRY_AFTER) if moment]
    return max(times) if times else datetime.now()


def update_schedule_state():
    """What the Settings page shows of the schedules: the choice, the choices, the last automatic run and the next (the active game's card data)"""
    game = games.active()
    card_hours, index_hours, price_hours = card_data_schedule(), index_schedule(), prices_schedule()
    return {
        'card_data': {'every_hours': card_hours, 'choices': list(CARD_DATA_SCHEDULE_HOURS), 'game': game.label,
                      'last': _shown_attempt(last_auto_attempt(game.id)), 'next': _shown(card_data_next_due(game, card_hours))},
        'image_index': {'every_hours': index_hours, 'choices': list(INDEX_SCHEDULE_HOURS),
                        'min_coverage': INDEX_AUTO_MIN_COVERAGE,
                        'last': _shown_attempt(last_index_attempt()), 'next': _shown(index_next_due(index_hours))},
        'prices': {'every_hours': price_hours, 'choices': list(PRICES_SCHEDULE_HOURS),
                   'last': _shown_attempt(last_price_attempt()), 'next': _shown(prices_next_due(price_hours))},
    }


SCHEDULE_FIELDS = {'card_data': ('card_data_every_hours', CARD_DATA_SCHEDULE_HOURS),
                   'image_index': ('image_index_every_hours', INDEX_SCHEDULE_HOURS),
                   'prices': ('prices_every_hours', PRICES_SCHEDULE_HOURS)}


def parse_update_schedule(data):
    """
    The settings in a request to change the schedules ({'card_data', 'image_index', 'prices'}: hours, each optional) as {setting key:
    value} - or ValueError before anything is saved (as backups.parse_schedule): a request refused for one value changes
    nothing. A boolean is refused although Python counts True as 1, and so is a number that is not one of the choices.
    """
    if not isinstance(data, dict):
        raise ValueError
    changes = {}
    for field, value in data.items():
        if field not in SCHEDULE_FIELDS:
            raise ValueError
        key, choices = SCHEDULE_FIELDS[field]
        if isinstance(value, bool) or not isinstance(value, int) or value not in choices:
            raise ValueError
        changes[key] = value
    if not changes:
        raise ValueError
    return changes


def price_source_state():
    """The price source chosen, its currency and the choices (Settings > Card data > Prices)"""
    chosen = prices.source(database.price_source)
    # 'priced': how many printings each source has a price for - 0 means not choosable until the card data is downloaded again
    choices = [{**choice, 'priced': database.priced_printings(choice['id'])} for choice in prices.choices()]
    return {'source': database.price_source, 'currency': chosen['currency'], 'symbol': chosen['symbol'],
            'from': chosen['from'], 'choices': choices, 'shops': shop_prices_status()}


@app.route('/api/prices/source')
def price_source():
    return jsonify({'success': True, **price_source_state()})


@app.route('/api/prices/source', methods=['POST'])
@guarded
def set_price_source():
    """
    Show and count another source's prices (JSON: 'source', an id of prices.SOURCES). The card data's shown prices change
    at once; then every entry of the collection and of the scanned cards is priced again in that source
    (reprice_collections) - the totals are in its currency from then on. Guarded: it rewrites every entry's price.
    """
    source_id = (request.get_json(silent=True) or {}).get('source')
    if not prices.known(source_id):
        return jsonify({'success': False, 'error': 'Unknown price source'}), 400
    chosen = prices.source(source_id)
    if not database.priced_printings(source_id):
        # Card data from before its prices were kept, or no shop prices downloaded yet: every card would show the new
        # currency with the old number, or nothing at all
        first = 'update the prices first' if chosen['kind'] == 'shops' else 'update the card database first'
        return jsonify({'success': False, 'error': f'There are no prices from that source yet: {first}'}), 409
    with pricing_lock:
        # Remembered BEFORE anything changes - the source shown now and this one, added to an earlier change that is still
        # unfinished: a failure or a stop below leaves it for the next repricing to finish (price_transition)
        before = app_settings.get(PRICE_TRANSITION_KEY)
        went_through = price_transition() or ([database.price_source] if database.price_source != source_id else [])
        previous = database.price_source
        try:
            if went_through:
                # A new target: no inventory is repriced for it yet ('done' starts empty - each must be priced again and
                # is withheld until it is). On disk BEFORE anything changes: a restart must know of the change
                _durable(PRICE_TRANSITION_KEY, {'sources': went_through + [source_id]})
        except Exception as e:
            logger.exception(f"Price source: the change could not be noted: {e}")
            return jsonify({'success': False, 'error': f'The settings could not be saved ({e}); nothing was changed'}), 500
        try:
            # All or nothing: the source is recorded only once its prices are in the shown columns (set_price_source)
            database.set_price_source(source_id)
        except Exception as e:
            # The shown prices never changed, so nothing written meanwhile took the new source's: the note as it was
            logger.exception(f"Price source: {source_id} could not be applied: {e}")
            app_settings.set(PRICE_TRANSITION_KEY, before)
            return jsonify({'success': False, 'error': f'The prices could not be changed ({e}); nothing was changed'}), 500
        try:
            # On disk too, or a restart would show the earlier source over prices of this one
            _durable('price_source', source_id)
        except Exception as e:
            # Undone - which is itself a change of source: between the UPDATE and now an entry may have been written at the
            # new source's price (InventoryManager._priced). The note, on disk already and naming this source, is KEPT, and
            # the entries repriced back; a repricing that fails leaves it for the next one (a Codex review of 2026-10-24)
            logger.exception(f"Price source: {source_id} could not be saved, going back to {previous}: {e}")
            database.set_price_source(previous)
            reprice_collections(games.active())
            return jsonify({'success': False, 'error': f'The choice could not be saved ({e}); the prices are back to '
                                                       f"{prices.source(previous)['label']}"}), 500
        try:
            # The entries the new source cannot price lose their price - with a new currency also those imported without
            # their printing (Game.price_changes)
            changed, cleared = reprice_collections(games.active(), strict=True)
        except Exception:
            changed = None
    # Every page shows prices: they take the new currency (common.js: priceSymbol) and reload their totals - also when the
    # repricing failed: the source and the card data's prices did change
    socketio.emit('price_source_changed', {'source': source_id, 'currency': chosen['currency'], 'symbol': chosen['symbol']}, namespace='/')
    if changed is None:
        return jsonify({'success': False, 'error': 'The source is changed, but not every card could be priced again: '
                                                   'it is finished at the next update or start, or by choosing it again'}), 500
    logger.info(f"Price source: {source_id}, {changed} entries priced again, {cleared} left without a price")
    return jsonify({'success': True, 'repriced': changed, 'unpriced': cleared, **price_source_state()})


@app.route('/api/updates/schedule')
def update_schedule():
    """The automatic updates of the card data and the picture index: their choice, last run and next"""
    return jsonify({'success': True, **update_schedule_state()})


@app.route('/api/updates/schedule', methods=['POST'])
@guarded
def set_update_schedule():
    """Change how often the card data is downloaded, the picture index goes back to what is missing and the shops' prices
    are downloaded (JSON: 'card_data', 'image_index', 'prices': hours, 0 off)"""
    try:
        changes = parse_update_schedule(request.get_json(silent=True))    # all of it checked before any of it is saved
    except ValueError:
        return jsonify({'success': False, 'error': 'Unknown update interval'}), 400
    for key, value in changes.items():
        app_settings.set(key, value)
    logger.info(f"Automatic updates: card data every {card_data_schedule()} h, picture index every {index_schedule()} h, "
                f"shop prices every {prices_schedule()} h (0: off)")
    return jsonify({'success': True, **update_schedule_state()})
