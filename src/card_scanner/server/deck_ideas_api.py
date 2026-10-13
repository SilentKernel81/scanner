# ============================================================================
# FILE: server/deck_ideas_api.py
# Deck ideas from other sites, popular decks and preconstructed decks.
# ============================================================================
from flask import jsonify, request
import threading

from card_scanner.core.database import search_key
from card_scanner import games
from card_scanner.core.recommendations import Unavailable

from card_scanner.server.state import app, database, deck_store, inventory, recommend, scan_inventory, socketio
from card_scanner.server.logs import logger
from card_scanner.server.desks import log_to_client
from card_scanner.server.decks_api import deck_covers, deck_payload, get_deck_or_404


@app.route('/api/inventory/suggested')
def inventory_suggested():
    """
    The owned cards that EDHREC lists for the commander of one of the decks:
    {'cards': {card name: [deck names]}, 'unknown': [decks EDHREC has no answer for]}
    """
    game = games.active()
    front = lambda name: search_key(name.split(' // ')[0])  # EDHREC names a double-faced card by its front
    owned = {}
    for card in inventory.get_all_cards(game.id):
        owned.setdefault(front(card['name']), set()).add(card['name'])
    cards, unknown = {}, []
    for deck in deck_store.list_decks(game.id):
        if not deck['commanders']:
            continue
        try:
            found = recommend.commander_cards(deck['commanders'])
        except Unavailable:
            found = None
        if not found:
            unknown.append(deck['name'])
            continue
        for category in found['categories']:
            for suggestion in category['cards']:
                for name in owned.get(front(suggestion['name']), ()):
                    decks = cards.setdefault(name, [])
                    if deck['name'] not in decks:
                        decks.append(deck['name'])
    return jsonify({'success': True, 'cards': cards, 'unknown': unknown})


@app.route('/api/decks/popular')
def popular_decks():
    """Public decks on other sites: with a deck's commander (deck_id), or of a format"""
    game = games.active()
    commander, deck_format = None, request.args.get('format')
    if request.args.get('deck_id'):
        deck, error = get_deck_or_404(int(request.args['deck_id']))
        if error:
            return error
        deck_format = deck['format']
        commander = next((card['name'] for card in deck['cards'] if card['board'] == 'commander'), None)
    if deck_format not in game.deck_formats:
        return jsonify({'success': False, 'error': 'Unknown format'}), 400
    found, problems = recommend.popular_decks(commander=commander, deck_format=deck_format)
    return jsonify({'success': True, 'decks': found, 'problems': problems, 'commander': commander})


@app.route('/api/precons')
def precon_list():
    """Preconstructed decks (MTGJSON), newest first - to browse, open as a deck, or add as owned"""
    try:
        return jsonify({'success': True, 'precons': recommend.precon_list()})
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502


def precon_known_cards(game, entries):
    """
    A preconstructed deck's entries with what the card data knows of each, for showing them:
    [{'name', 'board', 'quantity', 'image_uri', 'price', 'type_line'}]. The picture is the
    printing that is in the box (its Scryfall id), else any printing of the name; a card the
    card data does not have (a set newer than the last update) has no picture and no price.
    """
    by_id = database.cards_by_ids([entry['scryfall_id'] for entry in entries if entry['scryfall_id']])
    by_name = game.cards_by_names([entry['name'] for entry in entries])
    known = []
    for entry in entries:
        printed = by_id.get(entry['scryfall_id'])
        card = by_name.get(search_key(entry['name']))
        data = game.deck_card_payload(card) if card else {}
        known.append({'name': entry['name'], 'board': entry['board'], 'quantity': entry['quantity'],
                      'image_uri': (printed['image_uri'] if printed else None) or data.get('image_uri') or '',
                      'price': data.get('price'), 'type_line': data.get('type_line') or ''})
    return known


# The cover of each preconstructed deck that was asked about: file -> [{'name', 'image_uri'}].
# Working one out means reading the deck's whole list (recommendations.precon: from MTGJSON the
# first time, from the cache of its answers afterwards - a megabyte to parse either way); the
# list is printed and does not change, so what was worked out is kept while the server runs.
precon_cover_memo = {}
PRECON_COVERS_AT_ONCE = 8   # per request: each deck not read before is one request to MTGJSON


@app.route('/api/precons/covers')
def precon_covers():
    """
    The card that stands for each of some preconstructed decks, for the list's tiles:
    ?files=<file>,<file>,... (at most PRECON_COVERS_AT_ONCE) -> {'covers': {file: [{'name',
    'image_uri'}]}}. Which card: as for a deck of your own (decks_api.deck_covers) - its
    commander (two: both, the first shown first), else its most valuable card that is not a
    land. A deck that could not be read right now is left out of the answer - the page asks
    again another time; one that MTGJSON does not have gets an empty list.

    The page asks only for the decks that are in view (decks.js: loadPreconCovers): there are
    some 1,500 of them, and MTGJSON's list of decks has no pictures - only each deck's own
    file says what is in it.
    """
    game = games.active()
    files = [name for name in (request.args.get('files') or '').split(',') if name][:PRECON_COVERS_AT_ONCE]
    covers = {}
    for file_name in files:
        if file_name not in precon_cover_memo:
            try:
                entries = recommend.precon(file_name)
            except ValueError:
                continue   # not a file name: nothing to say about it
            except Unavailable:
                continue   # MTGJSON does not answer right now: asked again later
            precon_cover_memo[file_name] = deck_covers({'cover': ''}, precon_known_cards(game, entries)) if entries else []
        covers[file_name] = precon_cover_memo[file_name]
    return jsonify({'success': True, 'covers': covers})


@app.route('/api/precons/<file_name>')
def precon_cards(file_name):
    """A preconstructed deck's cards, to look at: nothing is saved (a deck is made with POST /api/decks, 'precon')"""
    game = games.active()
    try:
        entries = recommend.precon(file_name)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if not entries:
        return jsonify({'success': False, 'error': 'MTGJSON does not have this deck'}), 404
    owned = inventory.owned_by_name(game.id)
    # image_uri: the card's picture, shown beside the line it is pointed at in the list
    cards = [{'name': card['name'], 'quantity': card['quantity'], 'board': card['board'],
              'owned': owned.get(search_key(card['name']), 0), 'image_uri': card['image_uri']}
             for card in precon_known_cards(game, entries)]
    return jsonify({'success': True, 'cards': cards})


@app.route('/api/precons/<file_name>/own', methods=['POST'])
def precon_own(file_name):
    """
    "I own this precon": its cards go into the inventory - the printings and finishes that are
    in the box, Near Mint, at 'location' - and it becomes a deck to change from there.
    JSON: name (of the deck), location.
    """
    game = games.active()
    data = request.get_json(silent=True) or {}
    try:
        precon = next((deck for deck in recommend.precon_list() if deck['file'] == file_name), None)
        entries = recommend.precon(file_name) if precon else None
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if not entries:
        return jsonify({'success': False, 'error': 'MTGJSON does not have this deck'}), 404
    name = str(data.get('name') or precon['name'])[:80]
    location = str(data.get('location') or '').strip()[:60]

    # The exact printing: by its Scryfall id, else set + number, else any printing of the name
    by_id = database.cards_by_ids([entry['scryfall_id'] for entry in entries])
    by_name = game.cards_by_names([entry['name'] for entry in entries])
    deck_entries, unknown, added = [], [], 0
    for entry in entries:
        card = by_id.get(entry['scryfall_id']) \
            or (database.get_card_by_set_number(entry['set'], entry['number']) if entry['set'] and entry['number'] else None) \
            or by_name.get(search_key(entry['name']))
        if not card:
            unknown.append(entry['name'])
            continue
        finish = game.suggested_finish(card, 'foil' if entry['foil'] else 'non-foil')
        inventory.add_card(game.inventory_fields(card, finish), game.id, finish, 'Near Mint', entry['quantity'],
                           location=location, quiet=True)
        added += entry['quantity']
        deck_entries.append({'name': card['name'], 'card_id': card['id'], 'quantity': entry['quantity'],
                             'board': entry['board']})
    inventory.last_added = {}  # Undo takes back one add, not a whole deck
    deck_id = deck_store.create(game.id, name, precon['format'])
    deck_store.import_cards(deck_id, deck_entries)
    log_to_client(f"Added to inventory: {added} cards of {precon['name']}"
                  + (f" ({location})" if location else ''), level="success")
    # (the event's stats are the scanner page's counters: the scanned cards)
    socketio.emit('inventory_updated', {'auto': False, 'stats': scan_inventory.get_stats(game.id)}, namespace='/')
    return jsonify({'success': True, 'added': added, 'unknown': unknown,
                    'deck': deck_payload(deck_store.get(deck_id))})


# "What can I build?": rankings that need a request per commander / precon, so they run in
# the background and the page asks for their state. kind -> state
deck_ideas = {}
deck_ideas_lock = threading.Lock()


def run_deck_ideas(kind, game, state):
    """Fills state['items'] in a background thread; state['stop'] ends it early"""
    try:
        owned = inventory.owned_by_name(game.id)
        if kind == 'commanders':
            # Legendary creatures in the inventory, by how much of what is played with them is owned
            legends = sorted({row['name'] for row in inventory.get_all_cards(game.id)
                              if 'Legendary' in row['type_line'] and 'Creature' in row['type_line']})
            cards = game.cards_by_names(legends)
            legends = [name for name in legends if cards.get(search_key(name))
                       and game.can_be_commander(cards[search_key(name)])]
            state['total'] = len(legends)
            for name in legends:
                if state['stop']:
                    break
                found = recommend.commander_cards([name])
                if found:
                    played = {card['name']: card['inclusion'] for category in found['categories']
                              for card in category['cards']}
                    have = [card for card in played if owned.get(search_key(card))]
                    weight = sum(played.values())
                    card = cards[search_key(name)]
                    state['items'].append({
                        'name': card['name'], 'image_uri': card['image_uri'], 'identity': card['color_identity'],
                        'decks': found['decks'], 'owned': len(have), 'total': len(played),
                        'fit': round(100 * sum(played[card_name] for card_name in have) / weight) if weight else 0,
                    })
                state['done'] += 1
        elif kind == 'precons':
            # Every list is a request the first time: with a kind chosen on the page, only
            # the decks of that kind are read
            precons = [precon for precon in recommend.precon_list()
                       if not state.get('type') or precon['type'] == state['type']]
            state['total'] = len(precons)
            for precon in precons:
                if state['stop']:
                    break
                entries = recommend.precon(precon['file'])
                if entries:
                    total = sum(entry['quantity'] for entry in entries)
                    have = sum(min(entry['quantity'], owned.get(search_key(entry['name']), 0)) for entry in entries)
                    state['items'].append({**precon, 'owned': have, 'total': total,
                                           'percent': round(100 * have / total) if total else 0})
                state['done'] += 1
        else:
            # Public decks that play a card in a format (Archidekt), by the share of each that is owned
            found, problems = recommend.popular_decks(card=state['card'], deck_format=state['format'], limit=10)
            if problems and not found:
                state['error'] = problems[0]
            state['total'] = len(found)
            for item in found:
                if state['stop']:
                    break
                try:
                    played = [entry for entry in recommend.deck_from_url(item['url'])['entries']
                              if entry['board'] in ('main', 'commander')]
                except Unavailable:
                    played = []  # a deck made private since it was listed
                if played:
                    total = sum(entry['quantity'] for entry in played)
                    have = sum(min(entry['quantity'], owned.get(search_key(entry['name']), 0)) for entry in played)
                    state['items'].append({**item, 'owned': have, 'total': total,
                                           'percent': round(100 * have / total) if total else 0})
                state['done'] += 1
    except Unavailable as e:
        state['error'] = str(e)
    except Exception as e:
        logger.exception(f"Deck ideas ({kind}) failed: {e}")
        state['error'] = 'Something went wrong - see data/logs/app.log'
    finally:
        state['running'] = False


@app.route('/api/decks/ideas/<kind>', methods=['GET', 'POST'])
def deck_ideas_state(kind):
    """
    "What can I build?" searches, each a background run the page polls:
      commanders  owned legendary creatures, by the share of their EDHREC cards that is owned
      precons     preconstructed decks (JSON: type - only that kind; none: all), by the share owned
      card        public decks that play a card (JSON: card, format), by the share owned
    POST starts a run - replacing one that is running (answers are cached, so a second run is
    quick); POST with {"stop": true} ends it, keeping what was found; GET returns its state.
    """
    game = games.active()
    if kind not in ('commanders', 'precons', 'card') or not game.deck_formats:
        return jsonify({'success': False, 'error': 'Unknown search'}), 404
    data = request.get_json(silent=True) or {}
    with deck_ideas_lock:
        state = deck_ideas.get(kind)
        if request.method == 'POST' and data.get('stop'):
            if state:
                state['stop'] = True
        elif request.method == 'POST':
            if kind == 'card' and (not data.get('card') or data.get('format') not in game.deck_formats):
                return jsonify({'success': False, 'error': 'Choose a format and a card'}), 400
            if state:
                state['stop'] = True  # the run it replaces ends at its next step
            state = deck_ideas[kind] = {'running': True, 'stop': False, 'done': 0, 'total': 0, 'items': [],
                                        'error': None, 'card': data.get('card'), 'format': data.get('format'),
                                        'type': str(data.get('type') or '')}
            threading.Thread(target=run_deck_ideas, args=(kind, game, state), daemon=True).start()
    if not state:
        return jsonify({'success': True, 'started': False, 'running': False, 'items': []})
    order = 'fit' if kind == 'commanders' else 'percent'
    return jsonify({'success': True, 'started': True, 'running': state['running'], 'done': state['done'],
                    'total': state['total'], 'error': state['error'], 'stopped': state['stop'],
                    'card': state['card'], 'format': state['format'],
                    'items': sorted(list(state['items']), key=lambda item: item[order], reverse=True)})
