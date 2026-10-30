# ============================================================================
# FILE: server/trades_api.py
# Trades (collection page): cards set aside until a trade is confirmed.
# ============================================================================
from flask import Response, jsonify, request
import io
import re
import sqlite3

from card_scanner import games
from card_scanner.core.inventory import TRADE_HELD
from card_scanner.core.trades import TradeError

from card_scanner.server.state import app, socketio, trade_store
from card_scanner.server.logs import logger


@app.errorhandler(TradeError)
def trade_refused(error):
    return jsonify({'success': False, 'error': str(error)}), 400


@app.errorhandler(sqlite3.IntegrityError)
def write_refused(error):
    """A change the database refused - for the page, the one about copies a trade holds"""
    if TRADE_HELD not in str(error):
        logger.exception(f"Refused by the database: {error}")
    return jsonify({'success': False, 'error': str(error)}), 409


def collection_changed():
    """Other pages showing the collection read it again. An event of its own: a scanner page
    takes inventory_updated for a card that was added, and drops the card it is showing"""
    socketio.emit('collection_updated', {}, namespace='/')


@app.route('/api/trades', methods=['GET', 'POST'])
def trade_list():
    """
    GET: the active game's trades, open ones first, then the confirmed ones.
    POST (JSON: name, ids): set every free copy of these collection entries aside for the open
    trade with that name - a new one when there is none.
    """
    game = games.active()
    if request.method == 'GET':
        return jsonify({'success': True, 'trades': trade_store.list(game.id)})
    data = request.get_json(silent=True) or {}
    try:
        ids = [int(row_id) for row_id in data.get('ids') or []]
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'ids must be numbers'}), 400
    result = trade_store.add(game.id, data.get('name'), ids)
    collection_changed()
    return jsonify({'success': True, **result})


@app.route('/api/trades/<int:trade_id>', methods=['PUT', 'DELETE'])
def trade_item(trade_id):
    """PUT (JSON: name): rename. DELETE ?action=cancel: cancel an open trade (its cards stay in the collection);
    ?action=forget: take a confirmed one out of the history. Each refuses the other's trade - never one in place of the
    other: a "Cancel" from a page still showing a trade confirmed meanwhile once deleted its record (no action: cancel,
    which a page from before this asked for both)."""
    if request.method == 'PUT':
        trade_store.rename(trade_id, (request.get_json(silent=True) or {}).get('name'))
    elif request.args.get('action') == 'forget':
        trade_store.delete(trade_id)
    else:
        trade_store.cancel(trade_id)
    collection_changed()
    return jsonify({'success': True})


@app.route('/api/trades/<int:trade_id>/cards/<int:row_id>', methods=['PUT'])
def trade_card(trade_id, row_id):
    """How many copies of one of its cards an open trade holds (JSON: quantity; 0 takes it out)"""
    try:
        quantity = int((request.get_json(silent=True) or {}).get('quantity'))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'quantity must be a number'}), 400
    trade_store.set_quantity(trade_id, row_id, quantity)
    collection_changed()
    return jsonify({'success': True})


@app.route('/api/trades/<int:trade_id>/receives/<int:row_id>', methods=['PUT'])
def trade_receive(trade_id, row_id):
    """How many copies of one card an open trade receives (JSON: quantity; 0 takes it out). The
    cards are someone else's (server/others_api.py): nothing of the collection changes."""
    try:
        quantity = int((request.get_json(silent=True) or {}).get('quantity'))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'quantity must be a number'}), 400
    trade_store.set_receive_quantity(trade_id, row_id, quantity)
    collection_changed()   # other pages show the trade again (nothing of the collection itself changed)
    return jsonify({'success': True})


@app.route('/api/trades/<int:trade_id>/confirm', methods=['POST'])
def trade_confirm(trade_id):
    """The trade happened: its cards leave the collection, the trade stays as a record. JSON fingerprint (required): the
    one of the trade the person saw (GET /api/trades) - refused when the trade holds anything else now. A request without
    one - a page from before the fingerprint, which sent nothing or only its totals - is refused before anything is
    looked at: it cannot say what the person saw, and copies leaving cannot be undone (a Codex finding)"""
    data = request.get_json(silent=True) or {}
    fingerprint = data.get('fingerprint')
    if not isinstance(fingerprint, str) or not fingerprint:
        return jsonify({'success': False, 'error': 'This page is older than the server: reload it, look at the trade '
                                                   'again and confirm once more'}), 409
    result = trade_store.confirm(trade_id, {'fingerprint': fingerprint})
    collection_changed()
    return jsonify({'success': True, **result})


@app.route('/api/trades/<int:trade_id>/export/<fmt>')
def trade_export(trade_id, fmt):
    """Download a trade's cards (open or confirmed) in one of the game's export formats;
    ?side=receive: the cards it receives instead of the ones it gives"""
    game = games.active()
    formats = game.export_formats()
    trade = next((trade for trade in trade_store.list(game.id) if trade['id'] == trade_id), None)
    if fmt not in formats or not trade:
        return jsonify({'error': 'Unknown trade or export format'}), 404
    _label, _prefix, writer = formats[fmt]
    text = io.StringIO(newline='')
    writer(trade['receives'] if request.args.get('side') == 'receive' else trade['cards'], text)
    name = re.sub(r'[^A-Za-z0-9_-]+', '_', trade['name']).strip('_') or 'trade'
    return Response(text.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="trade_{name}{"_receive" if request.args.get("side") == "receive" else ""}_{fmt}.csv"'})
