# ============================================================================
# FILE: server/captures.py
# What becomes of a captured card: read, looked up, added or queued for review - and the
# captures waiting to be read, on record until they are settled.
# ============================================================================
import cv2
from datetime import datetime
from flask import request
from pathlib import Path
import threading

from card_scanner.config import Config
from card_scanner import games
from card_scanner.core import frames

from card_scanner.server.state import (
    app_settings, capture_outcomes, capture_outcomes_lock, identification, maintenance_barrier,
    pending, printing_check, review, scan_inventory, seen_captures, socketio, stations,
)
from card_scanner.server.logs import logger, scanned_cards_logger
from card_scanner.server.desks import desk, desk_for, emit_desk, in_desk, log_to_client, queue_changed, scanner


def get_ai_model_info():
    """Get current AI model info as a string for logging"""
    return identification.ai_info() if identification else None


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
    desk().pending_capture = str(image_path) if image_path else None


# The capture whose result this thread is applying (submit_capture): its uid (pending.py), which
# the add / the review item notes in its own commit - read again after a crash, it counts once
settling = threading.local()


def capture_key():
    return getattr(settling, 'uid', None)


def queue_for_review(game, image_path, name='', number='', set_code='', foil='unknown', card=None, why=None,
                     station=None):
    """
    A capture that wasn't added automatically goes to the review queue; scanning goes on.
    station: the stations.py station it came from (None: the scanner page's camera).
    Returns why it was queued.
    """
    review.add(game.id, image_path, name, number, set_code, foil, card, station=desk().id, capture_key=capture_key())
    if card:
        why = why or 'printing not confirmed'
        what = f"{card['name']} ({why})"
    else:
        why = 'not found' if name else 'not read'
        what = f"'{name}' (not found)" if name else "a card the AI couldn't read"
    log_to_client(f"{station_prefix(station)}Queued for review: {what}", level="warning")
    # queued: a card was just added to the queue (the page plays the queue alert)
    emit_desk('review_queue_update', {'count': review.count(game.id, desk().review_filter), 'queued': True})
    # Nothing waits on the page for this card (normal mode blocks auto-capture until Add / Skip)
    if scanner:
        scanner.card_under_review = False
    return why


def station_prefix(station):
    """Start of a log line about a station's card"""
    return f"[{station['name']}] " if station else ''


def route_identified(image_path, card_name, collector_number, set_code, processing_time=None, foil='unknown',
                     fast=False, reader=None, game_id=None, station=None, captured_at=None):
    """
    After the AI (or OCR - reader says which read the card): look the card up and add it automatically, show it, or queue it for review.
    While a review is open, a card captured meanwhile (manual capture, auto scanning without
    automatic adds) waits in the queue instead of taking the reviewed card's place and capture.
    game_id: the game being scanned when the card was captured.
    station, captured_at: for a capture sent by a station (always fast: added or queued).
    Returns what became of it: {'status': 'added' (+ 'card') | 'review' (+ 'reason') | 'shown'}
    """
    if game_id and game_id != games.active_id():
        # The game was switched while this capture waited for (or was with) the AI: it is not
        # looked up as a card of the other game - it waits in its own game's review queue,
        # without what was read (the prompt and parser may have been the other game's)
        review.add(game_id, image_path, foil=foil, station=desk().id, capture_key=capture_key())
        log_to_client(f"{station_prefix(station)}Captured before the game was switched: kept in the "
                      f"{games.get(game_id).label} review queue", level="warning")
        if scanner:
            scanner.card_under_review = False
        return {'status': 'review', 'reason': 'game switched'}
    reviewing = desk().current_review_id is not None
    if not fast and not reviewing:
        set_pending_capture(image_path)
    if card_name and card_name.strip():
        return search_and_emit_card(card_name, collector_number, processing_time, was_fast_scan_mode=fast,
                                    set_code=set_code, image_path=image_path, foil=foil, reader=reader,
                                    station=station, captured_at=captured_at)
    # Nothing was read (the AI gave no answer): the printing check's lookup still shows what the picture and the strip would have answered
    if printing_check and image_path:
        printing_check.observe(image_path, None, station, reader or get_ai_model_info())
    if fast or reviewing:
        return {'status': 'review', 'reason': queue_for_review(games.active(), image_path, foil=foil, station=station)}
    return {'status': 'shown'}


def added_payload(game, card, finish, quantity):
    """What an inventory_updated event says was added (the page shows it with an Undo)"""
    return {'name': card['name'], 'set': card['set'], 'number': card['number'],
            'finish': game.finishes[finish], 'quantity': quantity}


def scan_stats(game_id):
    """The scanned cards' totals as the current desk's page shows them: a station's page counts what that station scanned"""
    return scan_inventory.get_stats(game_id, desk().id)


def scan_camera():
    """The scanned cards' camera filter of a request (?camera= or JSON 'camera'): a station id, or None for every camera"""
    camera = request.args.get('camera') or (request.get_json(silent=True) or {}).get('camera') or ''
    return camera if stations and stations.get(camera) else None


def scan_location():
    """Where cards being scanned are put (the inventory location new entries get; '' = none): each station has its own"""
    station = desk().station
    if station:
        return station.get('location') or ''
    return (app_settings.get('scan_location', '') if app_settings else '') or ''


def add_automatically(game, card, image_path, foil, station=None, captured_at=None):
    """
    A confirmed card goes straight into the inventory: one Near Mint copy in the likely finish.
    Done here rather than by the page, so it doesn't depend on a page being open (or running
    the current script - an old tab once sent every add without its card).
    station: where it was captured, when not at the scanner page's camera - its location is
    used if it has one, and the add is that station's to undo. Returns what was added (added_payload).
    """
    finish = game.suggested_finish(card, foil)
    row_id = scan_inventory.add_card(game.inventory_fields(card, finish), game.id, finish, 'Near Mint', 1,
                                     capture=image_path, location=scan_location(), when=captured_at,
                                     source=desk().id, capture_key=capture_key())
    added = added_payload(game, card, finish, 1)
    emit_desk('inventory_updated', {'auto': True, 'stats': scan_stats(game.id), 'added': added})
    start_price_update(game, card, [row_id])
    return added


def search_and_emit_card(card_name, collector_number, processing_time=None, was_fast_scan_mode=False, set_code=None,
                         image_path=None, foil='unknown', reader=None, station=None, captured_at=None):
    """
    Search for card in database and emit results to client

    Args:
        card_name: Card name from AI
        collector_number: Collector number from AI
        set_code: Set code from AI (e.g. "HOB")
        processing_time: AI processing time in seconds
        was_fast_scan_mode: Whether this was a fast scan auto-add
        reader: what read the card when it wasn't the vision AI ("light-ocr")
        station, captured_at: for a capture sent by a station (see route_identified)

    Returns:
        dict: what became of the card (see route_identified)
    """

    # Log search action
    if collector_number:
        log_to_client(f"{station_prefix(station)}Auto-searching database for: {card_name} #{collector_number}")
    else:
        log_to_client(f"{station_prefix(station)}Auto-searching database for: {card_name}")

    # Search database
    game = games.active()
    read_by = reader or get_ai_model_info()
    db_card_info = game.identify(card_name, collector_number, set_code, ai_model=read_by)

    # Shadow mode of the printing check: does the picture look like the printing just resolved? It only
    # writes down where it does not (data/logs/printing_check.jsonl), in the background, and changes nothing
    # (also when nothing was found: the lookup then shows what the picture and the strip alone would have answered)
    if printing_check and image_path:
        printing_check.observe(image_path, db_card_info, station, read_by, foil=foil)

    # Adding automatically, only cards whose exact printing was confirmed are added
    # (Game.confirmed_matches, e.g. set + number); anything less certain goes to the review
    # queue and scanning goes on
    confirmed = game.is_confirmed(db_card_info)
    auto_add = was_fast_scan_mode and confirmed

    # Log scanned card
    log_scanned_card(
        card_name=card_name,
        collector_number=collector_number,
        ai_model=read_by,
        db_found=(db_card_info is not None),
        added_to_inventory=auto_add,
        processing_time=processing_time
    )

    if not auto_add and (was_fast_scan_mode or desk().current_review_id is not None):
        why = queue_for_review(game, image_path, card_name, collector_number, set_code, foil, db_card_info,
                               why=None if was_fast_scan_mode else 'captured during a review', station=station)
        return {'status': 'review', 'reason': why}
    if auto_add:
        # Added here, not through the current card: an open review keeps its card
        return {'status': 'added', 'card': add_automatically(game, db_card_info, image_path, foil, station, captured_at)}
    if db_card_info:
        # Waiting for Add / Skip anyway: show the current prices
        db_card_info = game.with_prices(db_card_info)
        if image_path:
            db_card_info['capture'] = str(image_path)
        desk().current_card_info = db_card_info
        emit_desk('card_found', {
            'card': game.card_payload(db_card_info),
            'auto_add': False
        })
    else:
        # Try to find similar cards
        similar = game.similar(card_name, limit=5)
        if similar:
            emit_desk('similar_cards', {'cards': similar})
        else:
            emit_desk('card_not_found', {'card_name': card_name})

    return {'status': 'shown'}


def announce_and_route(image_path, card_info, card_number, game_id, processing_time=None, fast=False, to=None,
                       station=None, captured_at=None):
    """
    What identification read from a capture (card_info; None: nothing): tell the page(s) and
    look the card up, add it or queue it for review (route_identified).
    processing_time: seconds the capture took when the reader gave none; to: only this page;
    station, captured_at: for a capture sent by a station. Returns route_identified's outcome
    with what was read ('read': name, number, set code, foil, reader).
    """
    info = card_info if isinstance(card_info, dict) else {}
    processing_time = info.get('processing_time') or processing_time
    socketio.emit('card_captured', {
        'image_path': str(image_path),
        'card_name': info.get('name', ''),
        'collector_number': info.get('collector_number', ''),
        'set_code': info.get('set_code', ''),
        'card_number': card_number,
        'processing_time': processing_time,
        'foil': info.get('foil', 'unknown')
    }, namespace='/', to=to or desk().room)
    outcome = route_identified(image_path, info.get('name', ''), info.get('collector_number', ''),
                               info.get('set_code', ''), processing_time, info.get('foil', 'unknown'), fast=fast,
                               reader=info.get('reader'), game_id=game_id, station=station, captured_at=captured_at)
    outcome['read'] = {'name': info.get('name', ''), 'number': info.get('collector_number', ''),
                       'set': info.get('set_code', ''), 'foil': info.get('foil', 'unknown'),
                       'reader': info.get('reader') or get_ai_model_info()} if info else None
    return outcome


def read_rgb(path):
    """An image file as an RGB array, or None"""
    image = cv2.imread(str(path)) if path and Path(path).is_file() else None
    return None if image is None else cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def foil_file(image_path):
    """Where a capture's perspective-corrected card is kept while it waits to be read"""
    image_path = Path(image_path)
    return image_path.with_name(f"{image_path.stem}_foil.jpg")


def submit_capture(image_path, image, foil_image, number, game_id, station=None, captured_at=None, on_outcome=None,
                   foil_path=None, pending_id=None, outcome_key=None, capture_id=None, uid=None):
    """
    Queue a saved capture to be read (identification.py) and then added or queued for review,
    as when adding automatically. It is on record (pending.py) until it is settled, so a
    restart picks it up again.
    foil_path: the file of foil_image when that is another picture than the card image;
    on_outcome(outcome): called with route_identified's outcome ('seconds' added);
    pending_id, uid: the capture is on record already (resume_pending) - its row and its uid;
    outcome_key: (uploading station's id, capture number) to remember the outcome under;
    capture_id: the uploader's own id for the capture (see seen_captures)
    """
    captured_at = captured_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    owner = desk()  # the capture's desk: its pages get the log lines and the outcome
    if pending_id is None:
        pending_id, uid = pending.add(game_id, station['id'] if station else None, number, Path(image_path).name,
                                      Path(foil_path).name if foil_path else None, foil_image is image, captured_at,
                                      capture_id=capture_id, sender=outcome_key[0] if outcome_key else None)
    queue_changed(+1)

    def identified(card_info, seconds):
        with maintenance_barrier, in_desk(owner):
            kept = False
            try:
                settling.uid = uid  # the add / review item notes it: applied once, whatever is read again
                outcome = announce_and_route(image_path, card_info, number, game_id, seconds, fast=True,
                                             station=station, captured_at=captured_at)
                outcome['seconds'] = round(seconds, 2)
                # The capture's own identity, in every answer about it from here on (this one,
                # a repeated upload, a later question - also after a restart: it is settled
                # with the outcome). A phone names the capture by its number and asks with it
                # where the copy is, or removes it (stations_api.py: station_capture_sync);
                # an outcome without it is of a capture that cannot be followed.
                outcome['sync_key'] = uid
                pending.settle(pending_id, outcome)  # off the queue, its outcome on record
                frames.attach(capture_id, uid)       # its camera frame (if kept) goes with the card
            except Exception as e:
                logger.exception(f"Capture {Path(image_path).name} could not be added or queued for review: {e}")
                log_to_client(f"{station_prefix(station)}Capture {Path(image_path).name} failed: {e}", level="error")
                outcome = {'status': 'error', 'message': str(e)}
                # Stays on record for the next starts (a few times: an error that never goes
                # away must not come back at every start for ever)
                try:
                    kept = pending.fail(pending_id, e, outcome)
                except Exception as error:
                    logger.error(f"Capture {Path(image_path).name}: its failure could not be recorded: {error}")
            finally:
                settling.uid = None
                if foil_path and not kept:
                    Path(foil_path).unlink(missing_ok=True)
                queue_changed(-1)
        if outcome_key:
            remember_outcome(outcome_key, outcome)
        if on_outcome:
            on_outcome(outcome)

    identification.submit(image, foil_image, game_id, identified, scope=lambda: in_desk(owner))


def remember_outcome(key, outcome):
    """What became of a station's capture: key (station id, capture number); the last 500"""
    with capture_outcomes_lock:
        capture_outcomes[key] = outcome
        while len(capture_outcomes) > 500:
            capture_outcomes.popitem(last=False)


def resume_pending():
    """Captures that were waiting to be read when the server stopped are queued again"""
    rows = pending.all()
    if not rows:
        return
    log_to_client(f"Reading {len(rows)} capture(s) that were waiting when the scanner stopped")
    for row in rows:
        image_path = Config.IMAGES_DIR / row['image']
        image = read_rgb(image_path)
        if image is None:
            logger.warning(f"Waiting capture {row['image']} is gone - dropped")
            pending.remove(row['id'])
            continue
        foil_path = Config.IMAGES_DIR / row['foil_image'] if row['foil_image'] else None
        foil_image = image if row['foil_is_image'] else read_rgb(foil_path)
        station = None
        if row['station']:  # a station forgotten meanwhile still gets its card in
            station = stations.get(row['station']) or {'id': row['station'], 'name': row['station'], 'location': None}
        outcome_key = (row['sender'] or row['station'], row['number']) if row['sender'] or row['station'] else None
        if row['capture_id'] and outcome_key:
            seen_captures[row['capture_id']] = outcome_key
        with in_desk(desk_for(row['station']) if row['station'] else None):
            submit_capture(image_path, image, foil_image, row['number'], row['game'], station, row['captured_at'],
                           foil_path=foil_path, pending_id=row['id'], outcome_key=outcome_key, uid=row['uid'])


def start_price_update(game, card, row_ids):
    """Update the prices of entries just added, in the background (games that fetch prices)"""
    if game.fetches_prices:
        threading.Thread(target=update_added_prices, args=(game, card, row_ids), daemon=True, name='prices').start()


def update_added_prices(game, card, row_ids):
    """
    After an add: fetch the card's current prices (games that fetch them per card) and update
    the entries - scanning never waits for the price request
    """
    try:
        card = game.with_prices(dict(card))
        changed = False
        for row_id in row_ids:
            entry = scan_inventory.get_entry(row_id)
            if not entry or entry['card_id'] != card['id']:
                continue  # deleted or merged meanwhile
            price = game.inventory_fields(card, entry['finish'])['price']
            if price != entry['price_usd']:
                scan_inventory.set_price(row_id, price)
                changed = True
        if changed:
            socketio.emit('inventory_prices_updated', {'stats': scan_inventory.get_stats(game.id)}, namespace='/')
    except Exception as e:
        logger.warning(f"Price update for {card.get('name')} failed: {e}")
