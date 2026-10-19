# ============================================================================
# FILE: server/inventory_api.py
# The inventory's routes: list, edit, delete, bulk edits, import / export, clear.
# ============================================================================
import csv
import tempfile
from pathlib import Path
from datetime import datetime
from flask import Response, jsonify, request, send_file
import io

from card_scanner.config import Config
from card_scanner.core import frames
from card_scanner.core.database import search_key
from card_scanner import games

from card_scanner.server.state import (
    app, app_settings, arena_inventory, deck_store, inventory, maintenance_barrier, review, scan_inventory, socketio,
    stations, trade_store,
)
from card_scanner.server.logs import logger
from card_scanner.server.desks import desk, desk_for, emit_desk, in_desk, log_to_client
from card_scanner.server.captures import scan_camera, set_pending_capture
from card_scanner.server.decks_api import commander_formats, copies_by_location, decks_using


def inventory_area():
    """The inventory a request is about: the scanner's (?area=scan), the digital collection
    (?area=arena: what the user has in MTG Arena) or the collection"""
    area = request.args.get('area')
    return scan_inventory if area == 'scan' else arena_inventory if area == 'arena' else inventory


# The routes that change an inventory. The digital collection is a copy of what MTG Arena
# has, replaced as a whole by each import (server/arena_api.py): changed by hand it would
# say something Arena does not, until the next import silently took it back. So these
# routes refuse it - listing and exporting it is all that is offered.
CHANGES_AN_INVENTORY = {'delete_inventory_card', 'update_inventory_card', 'bulk_update_inventory',
                        'remove_inventory_batch', 'import_inventory', 'clear_inventory', 'send_inventory_card_to_review'}


@app.before_request
def digital_collection_is_read_only():
    if request.args.get('area') == 'arena' and request.endpoint in CHANGES_AN_INVENTORY:
        return jsonify({'success': False, 'error': 'The Arena collection is a copy of what Arena has: '
                        'it is changed in Arena and brought here by an import'}), 400


@app.route('/api/scan_inventory/to_collection', methods=['GET', 'POST'])
def scan_to_collection():
    """
    Move every scanned card of the active game into the collection (merging with what is there);
    JSON 'location': where they all go ('' = each keeps the location it was scanned into);
    'camera' (a station id; also ?camera=): only the cards that camera scanned.
    GET: what the page asks first - how many cards wait, the locations in use and the decks' names.
    """
    game = games.active()
    camera = scan_camera()
    if request.method == 'GET':
        locations = set(inventory.locations(game.id)) | set(scan_inventory.locations(game.id))
        return jsonify({'success': True, 'cards': scan_inventory.get_stats(game.id, camera)['total_cards'],
                        'locations': sorted(locations, key=str.lower),
                        # the page lists the locations named after a deck apart
                        'decks': [deck['name'] for deck in deck_store.list_decks(game.id)] if game.deck_formats else []})
    location = str((request.get_json(silent=True) or {}).get('location') or '').strip()[:60]
    keys = scan_inventory.capture_keys(game.id, camera)
    # Not while a card is being sent back to review (send_inventory_card_to_review): the copy it
    # is about to release could be moved from under it, and be in the collection and the queue
    with maintenance_barrier:
        moved = inventory.take_from(scan_inventory, game.id, location=location, station=camera)
    release_frames(game, keys)
    socketio.emit('inventory_updated', {'auto': False, 'stats': scan_inventory.get_stats(game.id)}, namespace='/')
    return jsonify({'success': True, **moved, 'stats': scan_inventory.get_stats(game.id)})


@app.route('/api/inventory')
def get_inventory():
    """Get full inventory list"""
    if inventory:
        try:
            game = games.active()
            # The scanned cards can be narrowed to one camera's (?camera=)
            camera = scan_camera() if inventory_area() is scan_inventory else None
            cards = inventory_area().get_all_cards(game.id, camera)
            if inventory_area() is scan_inventory and not camera:
                # Who scanned each entry: {station name: copies}
                names = {station['id']: station['name'] for station in stations.all()}
                by_entry = scan_inventory.stations_by_entry(game.id)
                for card in cards:
                    card['cameras'] = {names.get(station, station): copies
                                       for station, copies in by_entry.get(card['id'], {}).items()}
            # What the collection page shows and filters with beyond the stored columns
            details = game.card_details([card['card_id'] for card in cards])
            # The decks that use each card (not for the scanned cards)
            # - and of the digital collection's cards, the Arena decks: each kind of deck uses
            # the copies of its own collection
            in_decks = {}
            if game.deck_formats and inventory_area() is not scan_inventory:
                in_decks = deck_store.needed_by_name(game.id, commander_formats(game),
                                                     'arena' if inventory_area() is arena_inventory else 'paper')
            placed = copies_by_location(cards)
            # The open trades that hold copies of each entry (the collection only)
            in_trades = trade_store.by_entry(game.id) if inventory_area() is inventory else {}
            for card in cards:
                card['details'] = details.get(card['card_id'], {})
                card['decks'] = decks_using(card, in_decks, placed)
                card['trades'] = in_trades.get(card['id'], [])

            return jsonify({
                'success': True,
                'cards': cards,
                'total': len(cards)
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'error': 'Inventory not initialized'}), 500


@app.route('/api/inventory/delete/<int:row_id>', methods=['DELETE', 'POST'])
def delete_inventory_card(row_id):
    """Delete an inventory entry by its id"""
    if inventory:
        success = inventory_area().delete_card(row_id)

        if success:
            return jsonify({
                'success': True,
                'message': 'Card deleted from inventory'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to delete card'}), 400

    return jsonify({'error': 'Inventory not initialized'}), 500


@app.route('/api/inventory/<int:row_id>/to_review', methods=['POST'])
def send_inventory_card_to_review(row_id):
    """
    Send one copy of an entry back to the review queue (the entry's card is not the right
    printing, or not the right card): the picture of its newest capture waits there with the
    entry's printing as the suggestion, and the reviewer finds the right one - and adds it to the
    scanned cards, from where it goes to the collection again. Works on the scanned cards and on
    the collection (?area=). The copy leaves the entry, so it is counted once.
    """
    area = inventory_area()
    game = games.active()
    # One at a time with a station removing a capture and with the review's own Add: the item
    # queued here must not be found gone, nor the copy removed twice
    with maintenance_barrier:
        # The copy the page showed (its picture file): a repeated request - the first one's answer
        # was lost - must not take another copy
        shown = str((request.get_json(silent=True) or {}).get('capture') or '').rsplit('/', 1)[-1] or None
        candidate = area.review_candidate(row_id, shown)
        if candidate is None and shown and area.review_candidate(row_id) is not None:
            return jsonify({'success': False, 'error': 'That copy is not here any more (already sent back, '
                            'moved or removed) - the list is reloaded'}), 409
        if candidate is None:
            return jsonify({'success': False, 'error': 'This card has no capture to review it by (only cards '
                            'that were scanned have one)'}), 400
        entry = candidate['entry']
        if entry['game'] != game.id:
            # A page left open while the game was switched: its row is another game's, and
            # queued under this one it could only be re-added to the wrong game
            return jsonify({'success': False, 'error': 'This card belongs to another game than the one '
                            'being scanned - switch to its game first (or reload the page)'}), 409
        card = game.get_card(entry['card_id']) if entry.get('card_id') else None
        if card:
            card['match'] = 'sent back'  # not one of the game's confirmed matches: a person decides
        station = candidate['station'] if candidate['station'] and stations.get(candidate['station']) else None
        foil = 'foil' if entry['finish'] in ('foil', 'surge') else 'non-foil'
        # The review item first, then the copy leaves: a failure in between leaves a duplicate
        # (the item is taken back below), never a card that is nowhere. No capture_key - the
        # capture's uid is used up in applied_captures and would make the review refuse the item
        # (and the scanned cards refuse the corrected card later); release_for_review notes it
        # transfer: the item stays out of the queue until the copy has left (confirmed below) and,
        # when the program stops in between, the next start settles it (finish_interrupted_sends)
        area_name = 'scan' if area is scan_inventory else 'collection'
        item_id = review.add(game.id, candidate['file'], entry['card_name'], entry['card_number'] or '',
                             entry['set_code'] or '', foil, card, station=station,
                             transfer=f"{area_name}:{candidate['row']['file']}")
        # The item must hold its own copy of the picture: review.add keeps an item without one
        # when the copy fails (full disk), and the original goes with the entry's copy
        kept = review.get(item_id) if item_id is not None else None
        if not kept or not review.image_path(kept) or not review.image_path(kept).exists():
            if item_id is not None:
                review.remove(item_id)
            return jsonify({'success': False, 'error': 'Not sent: the capture could not be copied to the '
                            'review queue (disk full?). The card stays where it is'}), 500
        try:
            area.release_for_review(candidate)
        except Exception as e:
            # Nothing left the inventory (release_for_review commits last and raises nothing
            # after it): the card was changed, moved to the collection or deleted meanwhile, or
            # an open trade holds it. The item is taken back, whatever the reason
            review.remove(item_id)
            logger.warning(f"Could not send {entry['card_name']} to review: {e}")
            # (an open trade holding the copies says so itself: inventory.TRADE_HELD)
            return jsonify({'success': False, 'error': f'Not sent: {e}'}), 409
        # The copy has left: the item joins the queue. Failing here leaves it pending - the next
        # start sees the copy gone and confirms it - never a card that is in neither place
        try:
            review.confirm_transfer(item_id)
        except Exception:
            logger.exception(f"{entry['card_name']} left the inventory but its review item could not be "
                             "confirmed - it appears in the queue after the next start")
    # (the inventory logged "Sent back to review" itself, to the pages)
    with in_desk(desk_for(station) or desk()):
        emit_desk('review_queue_update', {'count': review.count(game.id, desk().review_filter), 'queued': True})
    return jsonify({'success': True, 'message': f"{entry['card_name']} is waiting in the review queue",
                    'review_id': item_id})


def finish_interrupted_sends():
    """
    At start: settle the cards that were being sent back to review when the program stopped
    (send_inventory_card_to_review writes the review item, then removes the copy, then confirms
    the item). An item whose copy is still in its inventory never saw the copy leave: it is
    dropped, the card is where it was. One whose copy is gone is confirmed - it is all that is
    left of the card. Returns (dropped, confirmed).
    """
    inventories = {'scan': scan_inventory, 'collection': inventory}
    dropped = confirmed = 0
    for item_id, transfer in review.pending_transfers():
        area_name, _, file = transfer.partition(':')
        source = inventories.get(area_name)
        if source is None or not file:
            continue  # not understood: left alone rather than guessed at
        if not source.has_picture(file):
            review.confirm_transfer(item_id)
            confirmed += 1
        elif source.picture_exists(file):
            review.remove(item_id)
            dropped += 1
        else:
            # The row is there but its picture is gone: the release did commit and delete the
            # pictures, and a power cut took the commit back. The item holds the only picture
            # left - the release is finished here, not the item dropped
            source.release_by_picture(file)
            review.confirm_transfer(item_id)
            confirmed += 1
    if dropped or confirmed:
        logger.warning(f"Cards being sent back to review when the program stopped: {confirmed} confirmed, "
                       f"{dropped} left where they were")
    return dropped, confirmed


def same_printing(entry, card):
    """Is this card the printing an inventory entry is? (entries from a CSV have no card id)"""
    if entry['card_id']:
        return entry['card_id'] == card['id']
    return (entry['set_name'] or '') == (card.get('set') or '') and (entry['card_number'] or '') == (card.get('number') or '')


@app.route('/api/inventory/<int:row_id>/printings')
def inventory_entry_printings(row_id):
    """
    The printings an entry can be changed to (Edit card): every printing of its card, newest
    first, the one it is now marked 'current'. Empty for a game without that list.
    """
    entry = inventory_area().get_entry(row_id)
    if not entry:
        return jsonify({'success': False, 'error': 'Card not found'}), 404
    game = games.get(entry['game']) or games.active()
    try:
        printings = game.printings(entry['card_name'])
    except NotImplementedError:
        printings = []
    return jsonify({'success': True, 'printings': [
        {'id': card['id'], 'set': card['set'], 'set_code': card['set_code'], 'number': card['number'],
         'price': card['price'], 'price_foil': card['price_foil'], 'image_uri': card.get('image_uri'),
         'current': same_printing(entry, card)}
        for card in printings]})


@app.route('/api/inventory/update/<int:row_id>', methods=['PUT', 'POST'])
def update_inventory_card(row_id):
    """Update an inventory entry by its id (quantity, condition, finish, location, tags;
    card_id: another printing of the same card)"""
    if inventory:
        try:
            # Get data from request
            data = request.get_json() if request.is_json else {}
            quantity = data.get('quantity')
            condition = data.get('condition')
            finish = data.get('finish')
            split_quantity = data.get('split_quantity')
            location = data.get('location')
            tags = data.get('tags')
            if finish is not None and finish not in games.active().finishes:
                return jsonify({'success': False, 'split': False, 'error': f'Unknown finish: {finish}'}), 400

            # A new finish takes the printing's price in that finish (foil / holo / reverse)
            finish_price = None
            printing = None
            card_id = data.get('card_id')
            entry = inventory_area().get_entry(row_id) if finish or card_id else None
            if entry and card_id:
                # Another printing of the same card: its set, number, rarity and price
                game = games.get(entry['game']) or games.active()
                card = game.get_card(card_id)
                if not card or search_key(card['name']) != search_key(entry['card_name']):
                    return jsonify({'success': False, 'split': False,
                                    'error': f"Not a printing of {entry['card_name']}"}), 400
                if not same_printing(entry, card):
                    fields = game.inventory_fields(card, finish or entry['finish'])
                    printing = {'card_id': fields.get('card_id'), 'card_name': fields['name'],
                                'set_name': fields.get('set_name') or '', 'set_code': fields.get('set_code') or None,
                                'card_number': fields.get('number') or '', 'rarity': fields.get('rarity'),
                                'type_line': fields.get('type_line'), 'mana_cost': fields.get('mana_cost'),
                                'colors': fields.get('colors'), 'color_identity': fields.get('color_identity')}
                    finish_price = float(fields.get('price') or 0)
            if entry and not printing and finish and finish != entry['finish'] and entry['card_id']:
                game = games.get(entry['game']) or games.active()
                card = game.get_card(entry['card_id'])
                if card:
                    finish_price = game.inventory_fields(card, finish)['price']

            # Changing the finish of several copies splits the entry
            result = inventory_area().update_card(row_id, quantity=quantity, condition=condition,
                                           finish=finish, split_quantity=split_quantity,
                                           finish_price=finish_price,
                                           location=str(location)[:60] if location is not None else None,
                                           tags=tags, printing=printing)

            if result['success']:
                return jsonify(result)
            else:
                return jsonify(result), 400

        except Exception as e:
            logger.error(f"Error updating card: {e}")
            return jsonify({'success': False, 'split': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'split': False, 'error': 'Inventory not initialized'}), 500


@app.route('/api/inventory/bulk', methods=['POST'])
def bulk_update_inventory():
    """One change to several entries (JSON: ids, action, value) - see InventoryManager.bulk_update"""
    if not inventory:
        return jsonify({'success': False, 'error': 'Inventory not initialized'}), 500
    data = request.get_json(silent=True) or {}
    try:
        ids = [int(row_id) for row_id in data.get('ids') or []]
        changed = inventory_area().bulk_update(ids, data.get('action'), str(data.get('value') or '')[:60])
    except (ValueError, TypeError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'changed': changed})


@app.route('/api/inventory/remove_batch', methods=['POST'])
def remove_inventory_batch():
    """Take back the cards that came in together (JSON: batch, as in the entries' 'batches')"""
    batch = str((request.get_json(silent=True) or {}).get('batch') or '')
    if not batch:
        return jsonify({'success': False, 'error': 'batch is missing'}), 400
    return jsonify({'success': True, **inventory_area().remove_batch(games.active().id, batch)})


@app.route('/api/export_inventory/<fmt>', methods=['GET', 'POST'])
def export_inventory(fmt):
    """Download the active game's inventory in one of its export formats (Game.export_formats).
    POST (JSON: ids): only those entries - the ones ticked or shown on the page."""
    if not inventory:
        return jsonify({'error': 'Inventory not initialized'}), 500
    game = games.active()
    formats = game.export_formats()
    if fmt not in formats:
        return jsonify({'error': f'Unknown export format: {fmt}'}), 404
    try:
        _label, prefix, writer = formats[fmt]
        if request.method == 'POST':
            ids = {int(row_id) for row_id in (request.get_json(silent=True) or {}).get('ids') or []}
            text = io.StringIO(newline='')
            writer([card for card in inventory_area().get_all_cards(game.id) if card['id'] in ids], text)
            name = f"{prefix}_part_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            return Response(text.getvalue(), mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename="{name}"'})
        export_path = inventory_area().export(game.id, writer, prefix)
        return send_file(str(export_path), mimetype='text/csv', as_attachment=True,
                         download_name=export_path.name)
    except Exception as e:
        logger.exception(f"Export failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/import_inventory', methods=['POST'])
def import_inventory():
    """Import a collection file: another app's (Game.import_rows) or one in the app's own columns"""
    if not inventory:
        return jsonify({'error': 'Inventory not initialized'}), 500

    # Check if file was uploaded
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400

    file = request.files['file']

    # Check if filename is empty
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    # Check file extension
    if not file.filename.lower().endswith('.csv'):
        return jsonify({'success': False, 'error': 'Only CSV files are supported'}), 400

    try:
        replace_existing = request.form.get('replace_existing', 'false').lower() == 'true'
        game = games.active()
        content = file.read().decode('utf-8-sig', errors='replace')
        reader = csv.DictReader(io.StringIO(content))
        columns = {(name or '').strip().lower() for name in reader.fieldnames or []}

        # Another app's collection file (Game.import_formats), matched to the card database
        parsed = game.import_rows(columns, ({(key or '').strip().lower(): value for key, value in row.items()
                                             if isinstance(value, str)} for row in reader))
        if parsed is not None:
            if not parsed['entries']:
                # Nothing is replaced by a file with no card that could be matched
                return jsonify({'success': False, 'error': f"No card in this {parsed['format']} file was found "
                                                           f"in the {game.label} card database"}), 400
            if replace_existing and (parsed['not_found'] or parsed.get('unnamed')):
                # As the app's own file does: a replacement with rows left out would delete the
                # cards those rows stand for. Nothing is changed.
                missing = parsed['not_found']
                listed = ', '.join(missing[:10]) + (f" and {len(missing) - 10} more" if len(missing) > 10 else '')
                reasons = [f"{len(missing)} card{'s' if len(missing) != 1 else ''} not found in the "
                           f"{game.label} card database" + (f" ({listed})" if missing else '')] if missing else []
                if parsed.get('unnamed'):
                    reasons.append(f"{parsed['unnamed']} row{'s' if parsed['unnamed'] != 1 else ''} without a card name")
                return jsonify({'success': False,
                                'error': f"{' and '.join(reasons)} - the collection was not replaced. Correct the "
                                         'file, or add it instead of replacing.'}), 400
            stats = inventory_area().import_entries(parsed['entries'], game.id, replace_existing=replace_existing)
            # skipped: every row that was left out - cards not found, and rows without a name
            unnamed = parsed.get('unnamed', 0)
            stats.update(format=parsed['format'], by_name=parsed['by_name'], unnamed=unnamed,
                         skipped=len(parsed['not_found']) + unnamed, not_found=parsed['not_found'][:10])
            if parsed['not_found']:
                logger.warning(f"Import: {len(parsed['not_found'])} cards not found: "
                               + ', '.join(parsed['not_found'][:50]))
            if unnamed:
                logger.warning(f"Import: {unnamed} rows without a card name left out")
        elif {'card name', 'set'} <= columns:
            # The app's own columns (the "Card Scanner" export, and CSVs written before it)
            upload_dir = Config.DATA_DIR / 'uploads'
            upload_dir.mkdir(parents=True, exist_ok=True)
            # A file of its own per request: two uploads in the same second used to share a name
            with tempfile.NamedTemporaryFile('w', newline='', encoding='utf-8', dir=upload_dir,
                                             prefix='import_', suffix='.csv', delete=False) as handle:
                handle.write(content)
                temp_file_path = Path(handle.name)
            try:
                stats = inventory_area().import_csv(temp_file_path, game.id, list(game.finishes),
                                                    replace_existing=replace_existing)
            finally:
                temp_file_path.unlink()
            stats.update(format='Card Scanner', by_name=0, not_found=[])
        else:
            # Refused before anything is replaced
            formats = ' or '.join([*game.import_formats, 'Card Scanner'])
            return jsonify({'success': False,
                            'error': f"The columns of this file are not those of a {formats} CSV"}), 400

        if stats['success']:
            return jsonify({
                'success': True,
                'stats': stats,
                'inventory_stats': inventory_area().get_stats(game.id),
                'message': f"Import complete: {stats['added']} added, {stats['updated']} updated"
            })
        return jsonify({'success': False, 'error': stats.get('error', 'Import failed')}), 400

    except Exception as e:
        logger.exception(f"Import failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def release_frames(game, keys_before):
    """
    The camera frames kept for training (core/frames.py) go with the scanned cards they were taken
    for: once those are cleared or moved into the collection (`keys_before`: the uids that had a
    copy before; what is not there now is gone). Unless "Keep camera frames" is on in the
    collection page's Settings. Never fails the clear or the move.
    """
    try:
        if not keys_before or (app_settings and app_settings.get('keep_frames', False)):
            return
        gone = keys_before - scan_inventory.capture_keys(game.id)
        removed = frames.forget(gone)
        if removed:
            logger.info(f"Camera frames: {removed} removed with their scanned cards")
    except Exception as e:
        logger.warning(f"Could not release the camera frames of the scanned cards: {e}")


def clear_camera(game, camera):
    """
    A camera starts over: the cards it scanned (not yet in the collection) go, and the cards it
    has waiting for review. Returns (clear_inventory's result, review items removed).
    """
    keys = scan_inventory.capture_keys(game.id, camera)
    result = scan_inventory.clear_inventory(game.id, station=camera)
    if not result['success']:
        return result, 0
    release_frames(game, keys)
    # Not while a card is being sent back to review (send_inventory_card_to_review): the item
    # queued there would be deleted before the copy has left the inventory, and the card be nowhere
    with maintenance_barrier:
        reviews = review.clear(game.id, camera)
    with in_desk(desk_for(camera)):
        if desk().current_review_id is not None:  # its page had one open
            desk().current_review_id = desk().review_sid = desk().current_card_info = None
            set_pending_capture(None)
            emit_desk('review_item', {'id': None, 'total': 0})
        # What is left on its page: nothing of its own, but the items that are nobody's stay
        # (review.py: _of)
        emit_desk('review_queue_update', {'count': review.count(game.id, desk().review_filter)})
        if reviews:
            log_to_client(f"Scanned cards cleared: {reviews} card(s) removed from the review queue too", level="warning")
    return result, reviews


@app.route('/api/clear_inventory', methods=['POST', 'DELETE'])
def clear_inventory():
    """Clear all cards from inventory - of the scanned cards with ?camera=<station id>, only that camera's"""
    if not inventory:
        return jsonify({'error': 'Inventory not initialized'}), 500

    try:
        camera = scan_camera() if inventory_area() is scan_inventory else None
        game = games.active()
        if camera:
            result, reviews = clear_camera(game, camera)
        else:
            keys = scan_inventory.capture_keys(game.id) if inventory_area() is scan_inventory else None
            result, reviews = inventory_area().clear_inventory(game.id), 0
            if keys and result['success']:
                release_frames(game, keys)

        if result['success']:
            if inventory_area() is scan_inventory:
                # Every scanner page counts the scanned cards (its camera's, or everyone's)
                socketio.emit('inventory_updated', {'auto': False, 'cleared': True}, namespace='/')
            return jsonify({
                'success': True,
                'deleted': result['deleted'],
                'camera': camera,
                'reviews': reviews,
                'message': f"Inventory cleared: {result['deleted']} {'cards' if camera else 'entries'} removed"
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Failed to clear inventory')
            }), 400

    except Exception as e:
        logger.exception(f"Clear inventory failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
