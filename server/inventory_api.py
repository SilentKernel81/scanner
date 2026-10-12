# ============================================================================
# FILE: server/inventory_api.py
# The inventory's routes: list, edit, delete, bulk edits, import / export, clear.
# ============================================================================
import csv
from datetime import datetime
from flask import Response, jsonify, request, send_file
import io

from config import Config
from database import search_key
import games

from server.state import (
    app, deck_store, inventory, review, scan_inventory, socketio, stations, trade_store,
)
from server.logs import logger
from server.desks import desk, desk_for, emit_desk, in_desk, log_to_client
from server.captures import scan_camera, set_pending_capture
from server.decks_api import commander_formats, copies_by_location, decks_using


def inventory_area():
    """The inventory a request is about: the scanner's (?area=scan) or the collection"""
    return scan_inventory if request.args.get('area') == 'scan' else inventory


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
    moved = inventory.take_from(scan_inventory, game.id, location=location, station=camera)
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
            in_decks = deck_store.needed_by_name(game.id, commander_formats(game)) \
                if game.deck_formats and inventory_area() is inventory else {}
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
                'message': f'Card deleted from inventory'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to delete card'}), 400

    return jsonify({'error': 'Inventory not initialized'}), 500


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
    """Take back the cards that were added at one time (JSON: added_at, as in the entries)"""
    added_at = str((request.get_json(silent=True) or {}).get('added_at') or '')
    if not added_at:
        return jsonify({'success': False, 'error': 'added_at is missing'}), 400
    return jsonify({'success': True, **inventory_area().remove_batch(games.active().id, added_at)})


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
            stats = inventory_area().import_entries(parsed['entries'], game.id, replace_existing=replace_existing)
            stats.update(format=parsed['format'], by_name=parsed['by_name'],
                         skipped=len(parsed['not_found']), not_found=parsed['not_found'][:10])
            if parsed['not_found']:
                logger.warning(f"Import: {len(parsed['not_found'])} cards not found: "
                               + ', '.join(parsed['not_found'][:50]))
        elif {'card name', 'set'} <= columns:
            # The app's own columns (the "Card Scanner" export, and CSVs written before it)
            upload_dir = Config.DATA_DIR / 'uploads'
            upload_dir.mkdir(parents=True, exist_ok=True)
            temp_file_path = upload_dir / f"import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            temp_file_path.write_text(content, newline='')
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


def clear_camera(game, camera):
    """
    A camera starts over: the cards it scanned (not yet in the collection) go, and the cards it
    has waiting for review. Returns (clear_inventory's result, review items removed).
    """
    result = scan_inventory.clear_inventory(game.id, station=camera)
    if not result['success']:
        return result, 0
    reviews = review.clear(game.id, camera)
    with in_desk(desk_for(camera)):
        if desk().current_review_id is not None:  # its page had one open
            desk().current_review_id = desk().review_sid = desk().current_card_info = None
            set_pending_capture(None)
            emit_desk('review_item', {'id': None, 'total': 0})
        emit_desk('review_queue_update', {'count': 0})
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
            result, reviews = inventory_area().clear_inventory(game.id), 0

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
