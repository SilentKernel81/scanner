# ============================================================================
# FILE: server/others_api.py
# Other people's collections (Trades tab): a collection file imported under a name to browse,
# cards of it put into a trade as what the trade receives, or on the buy list. Kept in
# data/others.db, apart from the collection (core/others.py): the ids here are not ids of the
# collection, and nothing in this module changes the collection or the scanned cards.
# ============================================================================
import csv
import io
import re

from flask import Response, jsonify, request

from card_scanner import games
from card_scanner.core.inventory import now, read_collection_csv
from card_scanner.core.others import CARD_COLUMNS, OthersError, card_key

from card_scanner.server.state import app, others, trade_store
from card_scanner.server.logs import logger
from card_scanner.server.trades_api import collection_changed


@app.errorhandler(OthersError)
def others_refused(error):
    return jsonify({'success': False, 'error': str(error)}), 400


def read_collection_file(game, content):
    """
    The cards of a collection file as rows of core/others.py, whichever program wrote it: the
    app's own export (the "Card Scanner" columns), or one of the game's other formats
    (Game.import_formats: Moxfield, ManaBox). Returns (format name, rows, not found: [what could
    not be used], rejected: how many rows of the file are left out - cards not found, rows without
    a name, rows that cannot be read). Raises OthersError for a file that is none of them.
    Nothing is written.
    """
    reader = csv.DictReader(io.StringIO(content))
    columns = {(name or '').strip().lower() for name in reader.fieldnames or []}
    parsed = game.import_rows(columns, ({(key or '').strip().lower(): value for key, value in row.items()
                                         if isinstance(value, str)} for row in reader))
    if parsed is not None:
        rows = []
        for entry in parsed['entries']:
            fields = entry['fields']
            rows.append({
                'card_id': fields.get('card_id'), 'card_name': fields['name'], 'set_name': fields.get('set_name') or '',
                'set_code': fields.get('set_code') or None, 'card_number': fields.get('number') or '',
                'rarity': fields.get('rarity'), 'type_line': fields.get('type_line'), 'mana_cost': fields.get('mana_cost'),
                'colors': fields.get('colors'), 'color_identity': fields.get('color_identity'),
                'price_usd': float(fields.get('price') or 0), 'quantity': entry['quantity'],
                'condition': entry.get('condition') or 'Near Mint', 'finish': entry['finish'],
            })
        unnamed = parsed.get('unnamed', 0)
        left_out = list(parsed['not_found']) + ([f'{unnamed} row(s) without a card name'] if unnamed else [])
        return parsed['format'], rows, left_out, len(parsed['not_found']) + unnamed
    if {'card name', 'set'} <= columns:
        stats = {'skipped': 0, 'errors': 0}
        rows = read_collection_csv(csv.DictReader(io.StringIO(content)), game.id, list(game.finishes), now(), stats,
                                   lambda message, level='info': logger.warning(f'Other collection: {message}'))
        unusable = stats['skipped'] + stats['errors']
        return 'Card Scanner', rows, [f'{unusable} row(s) could not be read'] if unusable else [], unusable
    formats = ' or '.join([*game.import_formats, 'Card Scanner'])
    raise OthersError(f'The columns of this file are not those of a {formats} CSV')


@app.route('/api/others', methods=['GET'])
def other_collections():
    """The imported collections of the active game: name, format, entries, cards, value"""
    return jsonify({'success': True, 'collections': others.collections(games.active().id)})


@app.route('/api/others/import', methods=['POST'])
def import_other_collection():
    """
    Import a collection file (multipart: file, name, replace) under a name. The name is the
    person's; one that is there already is refused unless replace is 'true' (then its cards are
    swapped for the file's). The collection of the user is not touched.
    """
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    if not file.filename.lower().endswith('.csv'):
        return jsonify({'success': False, 'error': 'Only CSV files are supported'}), 400
    game = games.active()
    content = file.read().decode('utf-8-sig', errors='replace')
    format_name, rows, not_found, rejected = read_collection_file(game, content)
    replace = request.form.get('replace', 'false').lower() == 'true'
    if replace and rejected:
        # As the inventory's own replace does: swapped for a file with rows left out, a collection
        # would lose the cards those rows stand for. Nothing is changed.
        raise OthersError(f"{rejected} row{'s' if rejected != 1 else ''} of the file cannot be used "
                          f"({', '.join(not_found[:5])}{' ...' if len(not_found) > 5 else ''}) - the collection was "
                          'not replaced. Correct the file, or import it under another name.')
    if not rows:
        raise OthersError(f'No card of this {format_name} file could be used'
                          + (f' (not found in the {game.label} card data: {", ".join(not_found[:5])})' if not_found else ''))
    result = others.import_collection(game.id, request.form.get('name'), rows, format_name, file.filename,
                                      replace=replace)
    if not_found:
        logger.warning(f"Other collection {result['name']}: {len(not_found)} not found: " + ', '.join(not_found[:50]))
    return jsonify({'success': True, **result, 'format': format_name, 'not_found': not_found[:10],
                    'not_found_count': rejected})


@app.route('/api/others/<int:collection_id>', methods=['DELETE'])
def delete_other_collection(collection_id):
    """Forget an imported collection (the cards a trade or the buy list took from it stay there)"""
    others.delete(collection_id)
    return jsonify({'success': True})


@app.route('/api/others/<int:collection_id>/cards')
def other_collection_cards(collection_id):
    """A page of an imported collection: ?q= words, ?sort= (name, price, price_each, set, quantity),
    ?page= (from 0), ?size="""
    try:
        size = max(1, min(int(request.args.get('size', 50)), 200))
        page = max(0, int(request.args.get('page', 0)))
    except ValueError:
        return jsonify({'success': False, 'error': 'page and size must be numbers'}), 400
    cards, total = others.cards(collection_id, request.args.get('q', ''), request.args.get('sort', 'name'),
                                page * size, size)
    return jsonify({'success': True, 'cards': cards, 'total': total, 'page': page, 'size': size,
                    'collection': others.collection(collection_id)})


def chosen_cards(collection_id, items):
    """[(card_dict, copies)] for the {'row', 'quantity'} the page sent - rows of that collection only"""
    chosen = []
    for item in items or []:
        try:
            row, copies = int(item.get('row')), max(1, int(item.get('quantity', 1)))
        except (ValueError, TypeError, AttributeError):
            raise OthersError('Each card needs a row and a quantity')
        chosen.append((others.card(collection_id, row), copies))
    if not chosen:
        raise OthersError('No card chosen')
    return chosen


@app.route('/api/others/<int:collection_id>/receive', methods=['POST'])
def receive_from_other_collection(collection_id):
    """
    JSON name, items [{row, quantity}]: the open trade with that name (made when there is none)
    receives these cards of the collection - copies the other person does not have are cut. Their
    copies are kept as they are now; nothing of the user's collection is touched.
    """
    data = request.get_json(silent=True) or {}
    game = games.active()   # read once: the game of the trade is the game the page was showing
    if others.collection(collection_id)['game'] != game.id:
        raise OthersError('This collection is of another game than the one being used - switch to its game first')
    chosen = chosen_cards(collection_id, data.get('items'))
    seller = chosen[0][0]['seller']
    result = trade_store.receive(game.id, data.get('name'), seller, [
        {'card': card, 'quantity': copies, 'most': card['quantity'],
         'key': card_key({'card_id': card['card_id'], 'card_name': card['name'], 'set_name': card['set_name'],
                          'card_number': card['number'], 'condition': card['condition'], 'finish': card['finish']})}
        for card, copies in chosen])
    collection_changed()   # other pages show the trade (a new one, or more it receives); the collection itself is untouched
    return jsonify({'success': True, **result, 'seller': seller})


@app.route('/api/others/<int:collection_id>/buy', methods=['POST'])
def buy_from_other_collection(collection_id):
    """JSON items [{row, quantity}]: put these cards of the collection on the buy list"""
    chosen = chosen_cards(collection_id, (request.get_json(silent=True) or {}).get('items'))
    result = others.buy(collection_id, [(card['row'], copies) for card, copies in chosen])
    return jsonify({'success': True, **result})


@app.route('/api/buy_list', methods=['GET', 'DELETE'])
def buy_list():
    """GET: the buy list of the active game (cards with 'seller', 'row', 'quantity', 'most').
    DELETE: empty it, or ?seller= only that seller's part."""
    game = games.active()
    if request.method == 'DELETE':
        return jsonify({'success': True, 'removed': others.clear_buy_list(game.id, request.args.get('seller'))})
    return jsonify({'success': True, 'cards': others.buy_list(game.id)})


@app.route('/api/buy_list/<int:row_id>', methods=['PUT'])
def buy_list_card(row_id):
    """JSON quantity: how many of one card to buy; 0 takes it off the list"""
    try:
        quantity = int((request.get_json(silent=True) or {}).get('quantity'))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'quantity must be a number'}), 400
    others.set_buy_quantity(row_id, quantity)
    return jsonify({'success': True})


@app.route('/api/buy_list/export/<fmt>')
def buy_list_export(fmt):
    """The buy list (or ?seller= one seller's part) as a file in one of the game's export formats"""
    game = games.active()
    formats = game.export_formats()
    if fmt not in formats:
        return jsonify({'error': 'Unknown export format'}), 404
    seller = request.args.get('seller')
    cards = [card for card in others.buy_list(game.id) if not seller or card['seller'].lower() == seller.lower()]
    _label, _prefix, writer = formats[fmt]
    text = io.StringIO(newline='')
    writer(cards, text)
    name = re.sub(r'[^A-Za-z0-9_-]+', '_', seller or 'all').strip('_') or 'all'
    return Response(text.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="buy_{name}_{fmt}.csv"'})
