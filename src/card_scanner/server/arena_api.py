# ============================================================================
# FILE: server/arena_api.py
# The digital collection: what the user has in MTG Arena, brought here by importing the
# files an exporter writes (core/arena.py says what they look like).
# ============================================================================
from datetime import datetime
from flask import jsonify, request

from card_scanner.core import arena
from card_scanner import games

from card_scanner.server.state import app, arena_inventory, database, deck_store, maintenance_barrier, socketio
from card_scanner.server.logs import logger

UPLOAD_MAX_BYTES = 20 * 1024 ** 2   # the export this was written against is 0.4 MB


def arena_summary(game):
    """What the pages show of the digital collection: when it is from, how much is in it, the wildcards"""
    account = arena.account(arena_inventory)
    stats = arena_inventory.get_stats(game.id)
    return {'imported': account.get('imported') or '', 'exported': account.get('exported') or '',
            'cards': stats['total_cards'], 'entries': stats['unique_cards'],
            'wildcards': account.get('wildcards') or {}, 'gold': account.get('gold') or 0, 'gems': account.get('gems') or 0,
            'summary': account.get('summary') or {}}


@app.route('/api/arena')
def arena_info():
    """The digital collection in a few numbers (GET /api/inventory?area=arena lists its cards)"""
    return jsonify({'success': True, 'arena': arena_summary(games.active())})


@app.route('/api/arena/import', methods=['POST'])
def arena_import():
    """
    Replace the digital collection by an Arena export. Form data, field 'files': the
    exporter's mtga_collection.json, its mtga_decks.json, or both (told apart by what is in
    them, not by their names). One file alone replaces its own part and leaves the other.

    Everything is read and every card looked up before anything is changed; a file that is
    not the exporter's is refused and the digital collection stays as it was. Then:
      1. the cards and the account (wildcards, dates) replace what was there - one
         transaction in the digital collection's own file;
      2. with a deck file: the decks that came from Arena before are replaced by these (the
         deck store, another file: its own transaction). If that fails the collection is
         already the new one and the answer says so.
    Rebalanced cards ("A-...") are skipped: they are a second face of a card already counted.
    Nothing of the paper collection, the scanned cards or the decks made here is touched.
    """
    game = games.active()
    if not game.arena_decklist:
        return jsonify({'success': False, 'error': f'{game.label} has no digital collection'}), 400
    collection = decks = None
    try:
        for upload in request.files.getlist('files'):
            raw = upload.read(UPLOAD_MAX_BYTES + 1)
            if len(raw) > UPLOAD_MAX_BYTES:
                return jsonify({'success': False, 'error': f'{upload.filename} is too large to be an Arena export'}), 400
            text = raw.decode('utf-8-sig', errors='replace')
            # Which of the two files this is: the deck file has a list "decks", the other "cards"
            if '"decks"' in text[:2000] and '"cards"' not in text[:2000]:
                decks = arena.read_decks(text)
            else:
                collection = arena.read_collection(text)
    except arena.NotAnExport as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    if collection is None and decks is None:
        return jsonify({'success': False, 'error': 'Choose the files of the Arena exporter: mtga_collection.json, '
                        'mtga_decks.json, or both'}), 400

    # Either file alone is an import of its own: the collection file replaces the cards and
    # leaves the Arena decks, the deck file replaces the decks Arena brought and leaves the cards
    entries, summary = [], None
    if collection is not None:
        found, summary = arena.resolve(game, database, collection['cards'])
        # Stored without a price: a card in Arena cannot be sold, and its paper printing's price
        # would make the digital collection look like money (lists, statistics, exports)
        entries = [{'fields': {**game.inventory_fields(item['card'], game.suggested_finish(item['card'], 'non-foil')), 'price': 0},
                    'finish': game.suggested_finish(item['card'], 'non-foil'), 'quantity': item['quantity']} for item in found]
    new_decks, deck_unknown = [], []
    for deck in decks or []:
        cards, deck_summary = arena.resolve(game, database, deck['cards'], skip_rebalanced=False)
        deck_unknown += deck_summary['unknown']
        merged = {}   # Arena lists a card once per printing; a deck here has it once per board
        for item in cards:
            key = (item['card']['name'], item['board'])
            if key in merged:
                merged[key]['quantity'] += item['quantity']
            else:
                merged[key] = {'name': item['card']['name'], 'card_id': item['card']['id'],
                               'quantity': item['quantity'], 'board': item['board']}
        new_decks.append({**deck, 'cards': list(merged.values())})

    deck_error = None
    # Two files, two commits: the barrier keeps a full backup (backups.create_full) from
    # being taken between them, with the new collection beside the decks of before
    with maintenance_barrier:
        if collection is not None:
            account = {'exported': collection['exported'], 'imported': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       'wildcards': collection['wildcards'], 'gold': collection['gold'], 'gems': collection['gems'],
                       'summary': {**summary, 'unknown': summary['unknown'][:200]}}
            try:
                with arena_inventory._transaction():
                    # The account and the cards are one edit: both go in, or neither does
                    arena.save_account(arena_inventory, account)
                    arena_inventory.replace_all(game.id, entries)
            except Exception as e:
                logger.error(f"Arena import failed: {e}", exc_info=True)
                return jsonify({'success': False, 'error': f'The import failed and nothing was changed: {e}'}), 500
        if decks is not None:
            try:
                deck_store.replace_arena_decks(game.id, new_decks)
            except Exception as e:
                logger.error(f"Arena import: the decks failed: {e}", exc_info=True)
                if collection is None:
                    return jsonify({'success': False, 'error': f'The decks were not imported, and nothing was changed: {e}'}), 500
                deck_error = f'The collection was imported, but the decks were not: {e}'
    result = arena_summary(game)
    logger.info('Arena import: '
                + (f"{result['cards']} cards in {result['entries']} entries "
                   f"({summary['by_printing']} by set and number, {summary['by_name']} by name, "
                   f"{summary['rebalanced']} rebalanced skipped, {len(summary['unknown'])} unknown)" if collection is not None
                   else 'the collection left as it is')
                + (f", {len(new_decks)} decks" if decks is not None and not deck_error else ''))
    socketio.emit('arena_updated')   # an open collection page or deck page shows the new state
    # collection: whether the cards were replaced; decks: how many came in (None: no deck file)
    return jsonify({'success': True, 'arena': result, 'collection': collection is not None,
                    'decks': None if decks is None else len(new_decks), 'deck_unknown': sorted(set(deck_unknown)),
                    'warning': deck_error})
