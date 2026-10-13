# ============================================================================
# FILE: server/decks_api.py
# Decks (the deck pages): lists of cards; what is owned comes from the collection a deck is
# of - the collection for a deck of paper cards, the digital collection for an Arena deck.
# ============================================================================
from flask import Response, jsonify, request

from card_scanner.core import arena
from card_scanner.core.database import search_key
from card_scanner import games
from card_scanner.core.recommendations import Unavailable

from card_scanner.server.state import app, arena_inventory, deck_store, inventory, recommend
from card_scanner.server.logs import logger


def commander_formats(game):
    return [key for key, rules in game.deck_formats.items() if rules.get('commander')]


def copies_by_location(cards):
    """{(search_key(card name), location in lower case): copies} of inventory entries"""
    placed = {}
    for card in cards:
        key = (search_key(card['name']), card['location'].strip().lower())
        placed[key] = placed.get(key, 0) + card['quantity']
    return placed


def decks_using(card, in_decks, placed):
    """
    Names of the decks an inventory entry counts for. A deck lists card names, not copies, so the
    location tells them apart: a deck whose cards are all at the location named after it (a
    precon added with "I own it", a deck sorted into its own box) uses those copies, and the same
    card in another place is free. A deck without its copies there uses the card wherever it is.
    """
    key, location = search_key(card['name']), card['location'].strip().lower()
    return [used['deck'] for used in in_decks.get(key, [])
            if location == used['deck'].strip().lower()
            or placed.get((key, used['deck'].strip().lower()), 0) < used['quantity']]


def deck_format_for(game, site_format, entries):
    """Deck format for a deck from another site, whose format name may be unknown here"""
    if site_format in game.deck_formats:
        return site_format
    if any(entry['board'] == 'commander' for entry in entries):
        return commander_formats(game)[0]
    return 'legacy' if 'legacy' in game.deck_formats else next(iter(game.deck_formats))


def resolve_entries(game, entries, deck_format):
    """
    Entries from a pasted list or another site, as stored in a deck: the card data's spelling
    of each name and a printing to show. Returns (entries, names not in the card data).
    """
    has_commander = deck_format in commander_formats(game)
    by_name = game.cards_by_names([entry['name'] for entry in entries])
    # The printing the source names (a precon's own cards, a Moxfield deck) is the one shown
    printings = game.card_details([entry.get('scryfall_id') for entry in entries])
    resolved, unknown = [], []
    for entry in entries:
        board = entry.get('board') or 'main'
        if board == 'maybe':  # cards the other site lists outside the deck
            if not has_commander:
                continue
            board = 'side'
        if board == 'commander' and not has_commander:
            board = 'main'
        card = by_name.get(search_key(entry['name']))
        if not card:
            unknown.append(entry['name'])
        printing = entry.get('scryfall_id') if card and entry.get('scryfall_id') in printings else None
        resolved.append({'name': card['name'] if card else entry['name'],
                         'card_id': printing or (card['id'] if card else None),
                         'quantity': entry.get('quantity') or 1, 'board': board})
    return resolved, unknown


def deck_covers(deck, cards):
    """
    The cards whose picture stands for a deck - in the deck list and behind the deck's name:
    [{'name', 'image_uri'}], the first one shown first.

    cards: the deck's entries as [{'name', 'board', 'image_uri', 'price', 'type_line'}].
      1. the card chosen as cover (decks.cover), while it is in the deck;
      2. else the commanders, in the order they were added - two of them are both given, and
         the page shows the first with a button to see the other;
      3. else the deck's most valuable card that is not a land (a deck of a format without a
         commander looks like something before anyone chose) - any card if it has only lands.
    Empty only for a deck without cards the card data knows.
    """
    face = lambda card: {'name': card['name'], 'image_uri': card.get('image_uri') or ''}
    chosen = [card for card in cards if card['name'] == deck.get('cover')]
    if chosen:
        return [face(chosen[0])]
    commanders = [card for card in cards if card['board'] == 'commander']
    if commanders:
        return [face(card) for card in commanders]
    played = [card for card in cards if card['board'] == 'main' and card.get('image_uri')]
    spells = [card for card in played if 'Land' not in (card.get('type_line') or '').split(' // ')[0]]
    best = max(spells or played, key=lambda card: card.get('price') or 0, default=None)
    return [face(best)] if best else []


def collection_of(kind):
    """The inventory a deck of this kind counts as "owned": the digital collection (what the
    user has in MTG Arena) for an Arena deck, the collection for a deck of paper cards"""
    return arena_inventory if kind == 'arena' else inventory


def is_basic_land(card):
    """A basic land (Forest, Snow-Covered Island, Wastes): Arena gives as many as a deck wants"""
    front = ((card or {}).get('type_line') or '').split(' // ')[0]
    return 'Basic' in front and 'Land' in front


def deck_payload(deck):
    """A deck as the page shows it: card data, copies owned, other decks wanting them, issues, totals"""
    game = games.get(deck['game'])
    kind = deck.get('kind') or 'paper'
    considering = deck['format'] in commander_formats(game)  # its 'side' board isn't played
    by_name = game.cards_by_names([card['name'] for card in deck['cards']])
    # Owned: in the collection the deck is of. And only decks of that kind want the same copies
    owned = collection_of(kind).owned_by_name(game.id)
    needed = deck_store.needed_by_name(game.id, commander_formats(game), kind)
    # The image is the printing the deck entry was added with (a precon's own printing, the
    # search result clicked); the rest is the card's data, the same for every printing
    printings = game.card_details([entry['card_id'] for entry in deck['cards']])
    cards, checked = [], []
    for entry in deck['cards']:
        key = search_key(entry['name'])
        card = by_name.get(key)
        payload = game.deck_card_payload(card) if card else None
        printing = printings.get(entry['card_id']) or {}
        if payload and printing.get('image_uri'):
            payload['image_uri'] = printing['image_uri']
        cards.append({
            'name': entry['name'], 'quantity': entry['quantity'], 'board': entry['board'],
            'card': payload,
            # The printing shown (the picker changes it)
            'printing': {'id': entry['card_id'], 'set_code': printing.get('set_code') or '',
                         'number': printing.get('number') or ''},
            'owned': owned.get(key, 0),
            'elsewhere': [other for other in needed.get(key, []) if other['deck_id'] != deck['id']],
        })
        checked.append({'name': entry['name'], 'quantity': entry['quantity'], 'board': entry['board'], 'card': card})
    played = [card for card in cards if not (considering and card['board'] == 'side')]
    wanted = {}
    for card in played:
        wanted[card['name']] = wanted.get(card['name'], 0) + card['quantity']
    if kind == 'arena':
        # Arena hands out basic lands freely: an Arena deck has as many as it plays, whatever
        # the export counted (it lists one of each printing you were given)
        for card in cards:
            if is_basic_land(card['card']):
                card['owned'] = max(card['owned'], wanted.get(card['name'], card['quantity']))
    price = lambda card: (card['card'] or {}).get('price') or 0.0
    first = {card['name']: card for card in played}
    missing = {name: max(0, count - first[name]['owned']) for name, count in wanted.items()}
    wildcards = None
    issues = game.check_deck(deck['format'], checked)
    if kind == 'arena':
        # A card Arena does not have cannot be in an Arena deck, whatever the format allows
        # (a list pasted from a paper deck brings them): a problem of the deck - and no
        # wildcard crafts it, so it is not counted below
        _text, absent = game.arena_decklist(deck['cards'], deck['format'])
        absent = [name for name in absent if name in wanted]
        if absent:
            issues = [{'level': 'error', 'message': 'Not on Arena', 'cards': sorted(absent)}] + issues
        # What the missing cards cost in Arena: a wildcard of the card's rarity each (the
        # card data's rarity - of one paper printing, which Arena shares for nearly every
        # card), beside the wildcards the account had at the last import
        wildcards = {'needed': arena.wildcards_needed(
                         [((first[name]['card'] or {}).get('rarity'), count)
                          for name, count in missing.items() if count and name not in absent]),
                     'have': arena.account(arena_inventory).get('wildcards') or {}}
    return {
        'id': deck['id'], 'name': deck['name'], 'format': deck['format'], 'notes': deck['notes'],
        'updated_at': deck['updated_at'], 'cards': cards,
        # kind: 'paper' or 'arena' - which collection "owned" was counted in; from_arena: the
        # deck came with an Arena export (the next import replaces it); wildcards: an Arena
        # deck's missing cards as what crafting them costs
        'kind': kind, 'from_arena': bool(deck.get('arena_id')), 'wildcards': wildcards,
        # cover: the card chosen to stand for the deck ('' when none is, or it left the deck);
        # covers: what is shown - that card, or what stands in for it (deck_covers)
        'cover': deck.get('cover') if any(card['name'] == deck.get('cover') for card in cards) else '',
        'covers': deck_covers(deck, [{'name': card['name'], 'board': card['board'], **{
            key: (card['card'] or {}).get(key) for key in ('image_uri', 'price', 'type_line')}} for card in cards]),
        'issues': issues,
        'totals': {
            'cards': sum(card['quantity'] for card in played if card['board'] != 'side'),
            'side': sum(card['quantity'] for card in cards if card['board'] == 'side'),
            'price': round(sum(price(first[name]) * count for name, count in wanted.items()), 2),
            'missing': sum(missing.values()),
            'missing_price': round(sum(price(first[name]) * count for name, count in missing.items()), 2),
        },
    }


def get_deck_or_404(deck_id):
    deck = deck_store.get(deck_id) if deck_store else None
    if not deck:
        return None, (jsonify({'success': False, 'error': 'Deck not found'}), 404)
    return deck, None


def entries_from_source(game, data, deck_format):
    """
    Cards for a deck from what the page sent: 'text' (a pasted list), 'url' (Moxfield /
    Archidekt), 'precon' (MTGJSON file name) or 'average' (EDHREC's average deck for these
    commander names). Returns (entries or None, name from the source, its format name).
    """
    if data.get('text'):
        return game.parse_decklist(data['text'], deck_format), None, None
    if data.get('url'):
        found = recommend.deck_from_url(data['url'])
        return found['entries'], found['name'], found['format']
    if data.get('precon'):
        entries = recommend.precon(data['precon'])
        if entries is None:
            raise Unavailable('MTGJSON does not have this deck')
        return entries, None, None
    if data.get('average'):
        entries = recommend.average_deck(data['average'])
        if entries is None:
            raise Unavailable('EDHREC has no average deck for this commander')
        return entries, None, None
    return None, None, None


@app.route('/api/decks', methods=['GET', 'POST'])
def decks_collection():
    """List the active game's decks, or create one (JSON: name, format, and optionally a source
    of cards - see entries_from_source - or 'commander': a card name to start with)"""
    game = games.active()
    if not game.deck_formats:
        return jsonify({'success': False, 'error': f'No deck builder for {game.label}'}), 400
    if request.method == 'GET':
        # Each deck counts what is owned in the collection it is of (collection_of)
        owned_in = {'paper': inventory.owned_by_name(game.id), 'arena': arena_inventory.owned_by_name(game.id)}
        considering = commander_formats(game)
        listed, entries_of = [], {}   # entries_of: deck id -> its entries, for the tiles' pictures
        for deck in deck_store.list_decks(game.id):
            wanted = {}
            entries_of[deck['id']] = deck_store.get(deck['id'])['cards']
            for card in entries_of[deck['id']]:
                if not (card['board'] == 'side' and deck['format'] in considering):
                    wanted[card['name']] = wanted.get(card['name'], 0) + card['quantity']
            listed.append({**deck, 'from_arena': bool(deck['arena_id']), '_wanted': wanted})
        # (how much of each is owned is counted below, once the cards' data is at hand: an
        # Arena deck's basic lands count as owned, and that needs their type)
        # What a deck's tile shows: its cover (deck_covers: the card chosen, else the
        # commanders, else its most valuable card), in the printing the deck has - as the
        # deck's own page shows it - and the commanders' colours. Asked for all decks at once:
        # two look-ups, not two per deck.
        entries = [entry for cards in entries_of.values() for entry in cards]
        printings = game.card_details([entry['card_id'] for entry in entries]) if entries else {}
        by_name = game.cards_by_names([entry['name'] for entry in entries]) if entries else {}
        for deck in listed:
            known = []
            for entry in entries_of[deck['id']]:
                card = by_name.get(search_key(entry['name']))
                data = game.deck_card_payload(card) if card else {}
                known.append({'name': entry['name'], 'board': entry['board'],
                              'image_uri': (printings.get(entry['card_id']) or {}).get('image_uri') or data.get('image_uri') or '',
                              'price': data.get('price'), 'type_line': data.get('type_line'),
                              'identity': data.get('identity') or []})
            deck['covers'] = deck_covers(deck, known)
            deck['commander_cards'] = [{'name': card['name'], 'image_uri': card['image_uri'], 'identity': card['identity']}
                                       for card in known if card['board'] == 'commander']
            wanted, owned = deck.pop('_wanted'), owned_in[deck['kind']]
            basics = {card['name'] for card in known if is_basic_land(card)} if deck['kind'] == 'arena' else set()
            have = sum(count if name in basics else min(count, owned.get(search_key(name), 0)) for name, count in wanted.items())
            total = sum(wanted.values())
            deck['owned'], deck['owned_percent'] = have, round(100 * have / total) if total else 0
            del deck['arena_id']   # Arena's id for the deck is nothing a page needs
        return jsonify({'success': True, 'decks': listed, 'has_deck_data': bool(game.has_deck_data())})

    data = request.get_json(silent=True) or {}
    deck_format = data.get('format')
    try:
        entries, source_name, source_format = entries_from_source(game, data, deck_format)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if entries is not None and deck_format not in game.deck_formats:
        deck_format = deck_format_for(game, source_format, entries)
    if deck_format not in game.deck_formats:
        return jsonify({'success': False, 'error': 'Choose a format'}), 400
    if entries is None and data.get('commander'):
        entries = [{'name': data['commander'], 'quantity': 1, 'board': 'commander'}]
    unknown = []
    # kind: 'arena' for a deck planned against the digital collection; anything else is a paper deck
    deck_id = deck_store.create(game.id, str(data.get('name') or source_name or 'New deck')[:80], deck_format,
                                kind='arena' if data.get('kind') == 'arena' else 'paper')
    if entries:
        resolved, unknown = resolve_entries(game, entries, deck_format)
        deck_store.import_cards(deck_id, resolved)
    logger.info(f"Deck created: {deck_id} ({deck_format})")
    return jsonify({'success': True, 'deck': deck_payload(deck_store.get(deck_id)), 'unknown': unknown})


@app.route('/api/decks/<int:deck_id>', methods=['GET', 'PUT', 'DELETE'])
def deck_item(deck_id):
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    if request.method == 'DELETE':
        deck_store.delete(deck_id)
        return jsonify({'success': True})
    if request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        deck_format = data.get('format')
        if deck_format is not None and deck_format not in games.get(deck['game']).deck_formats:
            return jsonify({'success': False, 'error': f'Unknown format: {deck_format}'}), 400
        name = str(data['name'])[:80] if data.get('name') is not None else None
        notes = str(data['notes'])[:4000] if data.get('notes') is not None else None
        # cover: the name of one of the deck's cards, or '' to go back to what stands in for it
        cover = str(data['cover']) if data.get('cover') is not None else None
        if cover and not any(card['name'] == cover for card in deck['cards']):
            return jsonify({'success': False, 'error': f'{cover} is not in this deck'}), 400
        deck_store.update(deck_id, name=name, deck_format=deck_format, notes=notes, cover=cover)
        deck = deck_store.get(deck_id)
    return jsonify({'success': True, 'deck': deck_payload(deck)})


@app.route('/api/decks/<int:deck_id>/duplicate', methods=['POST'])
def deck_duplicate(deck_id):
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    # JSON kind: 'paper' or 'arena' - the copy's kind (none: the deck's own)
    kind = (request.get_json(silent=True) or {}).get('kind')
    if kind not in (None, 'paper', 'arena'):
        return jsonify({'success': False, 'error': 'Unknown kind of deck'}), 400
    return jsonify({'success': True, 'deck': deck_payload(deck_store.get(deck_store.duplicate(deck_id, kind)))})


@app.route('/api/decks/<int:deck_id>/cards', methods=['POST'])
def deck_cards(deck_id):
    """
    Change a deck's cards. JSON: 'cards': [{name, board, card_id, and one of 'change' (+1 / -1),
    'quantity' (set; 0 removes), 'move_to' (another board) or 'printing' (the printing id to
    show the entry in)}]. Returns the deck.
    """
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        for item in data.get('cards') or []:
            name, board = str(item['name']), item.get('board') or 'main'
            if item.get('printing'):
                if not games.get(deck['game']).card_details([item['printing']]):
                    raise ValueError('Unknown printing')
                deck_store.set_printing(deck_id, name, board, item['printing'])
            elif item.get('move_to'):
                deck_store.move_card(deck_id, name, board, item['move_to'])
            elif 'quantity' in item:
                deck_store.set_card(deck_id, name, board, int(item['quantity']), item.get('card_id'))
            else:
                deck_store.add_card(deck_id, name, board, int(item.get('change', 1)), item.get('card_id'))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'deck': deck_payload(deck_store.get(deck_id))})


@app.route('/api/decks/<int:deck_id>/import', methods=['POST'])
def deck_import(deck_id):
    """Add cards from a source (see entries_from_source) to a deck; 'replace': true empties it first"""
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    game = games.get(deck['game'])
    data = request.get_json(silent=True) or {}
    try:
        entries, _name, _format = entries_from_source(game, data, deck['format'])
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if not entries:
        return jsonify({'success': False, 'error': 'No cards found'}), 400
    resolved, unknown = resolve_entries(game, entries, deck['format'])
    try:
        added = deck_store.import_cards(deck_id, resolved, replace=bool(data.get('replace')))
    except ValueError as e:  # refused as a whole: the deck is as it was
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'added': added, 'unknown': unknown,
                    'deck': deck_payload(deck_store.get(deck_id))})


@app.route('/api/decks/<int:deck_id>/export/<kind>')
def deck_export(deck_id, kind):
    """Download a deck as a text list ('text'), or only the copies not owned ('buylist')"""
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    game = games.get(deck['game'])
    entries = deck['cards']
    if kind == 'buylist':
        owned = collection_of(deck.get('kind')).owned_by_name(game.id)
        wanted = {}
        for entry in entries:
            if not (entry['board'] == 'side' and deck['format'] in commander_formats(game)):
                wanted[entry['name']] = wanted.get(entry['name'], 0) + entry['quantity']
        lines = [f"{count - owned.get(search_key(name), 0)} {name}" for name, count in sorted(wanted.items())
                 if count > owned.get(search_key(name), 0)]
        text = '\n'.join(lines) + '\n'
    elif kind == 'text':
        text = game.format_decklist(entries, deck['format'])
    elif kind == 'arena' and game.arena_decklist:
        text, _left_out = game.arena_decklist(entries, deck['format'])
    else:
        return jsonify({'success': False, 'error': f'Unknown export: {kind}'}), 404
    file_name = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in deck['name']).strip() or 'deck'
    return Response(text, mimetype='text/plain', headers={
        'Content-Disposition': f'attachment; filename="{file_name}{"_buylist" if kind == "buylist" else "_arena" if kind == "arena" else ""}.txt"'})


@app.route('/api/decks/<int:deck_id>/arena')
def deck_arena(deck_id):
    """
    A deck as text for MTG Arena's Import button, for the page's window (which shows it, to
    be copied): {'text', 'left_out': [names of the deck's cards that are not on Arena, or
    that the card data does not know], 'cards': how many are in the text}.
    The same text as a file: GET /api/decks/<id>/export/arena.
    """
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    game = games.get(deck['game'])
    if not game.arena_decklist:
        return jsonify({'success': False, 'error': f'No digital client for {game.label}'}), 400
    text, left_out = game.arena_decklist(deck['cards'], deck['format'])
    count = sum(int(line.split(' ', 1)[0]) for line in text.splitlines() if line[:1].isdigit())
    return jsonify({'success': True, 'text': text, 'left_out': left_out, 'cards': count,
                    'check': arena_check(game, deck, left_out)})


def arena_check(game, deck, left_out):
    """
    Can this deck be built in Arena? Its cards against the digital collection (the last
    import of an Arena export), for the Arena window of any deck:

      'imported'   when that import was ('' : never - then there is nothing to compare with)
      'have'       copies of the deck that are in the Arena collection
      'craft'      [{'name', 'rarity', 'copies'}] - on Arena, but not (enough) in the collection
      'wildcards'  {'needed': {rarity: n}, 'have': {rarity: n}} - what crafting them costs
                   (core/arena.py: wildcards_needed), beside what the account had
      'not_on_arena'  how many copies are of cards Arena does not have (their names: left_out)

    A Commander deck's considered cards are not part of the deck and not counted; basic
    lands are free in Arena.
    """
    account = arena.account(arena_inventory)
    owned = arena_inventory.owned_by_name(game.id)
    by_name = game.cards_by_names([entry['name'] for entry in deck['cards']])
    considering = deck['format'] in commander_formats(game)
    wanted = {}
    for entry in deck['cards']:
        if not (considering and entry['board'] == 'side'):
            wanted[entry['name']] = wanted.get(entry['name'], 0) + entry['quantity']
    have, craft, absent = 0, [], 0
    for name, copies in sorted(wanted.items()):
        card = by_name.get(search_key(name))
        if name in left_out:
            absent += copies
            continue
        got = copies if is_basic_land(card) else min(copies, owned.get(search_key(name), 0))
        have += got
        if got < copies:
            craft.append({'name': name, 'rarity': (card or {}).get('rarity') or '', 'copies': copies - got})
    return {'imported': account.get('exported') or '', 'have': have, 'craft': craft, 'not_on_arena': absent,
            'wildcards': {'needed': arena.wildcards_needed([(card['rarity'], card['copies']) for card in craft]),
                          'have': account.get('wildcards') or {}}}


@app.route('/api/cards/search')
def search_cards():
    """Deck builder card search: one result per card name, with the copies owned"""
    game = games.active()
    if not game.deck_formats:
        return jsonify({'success': False, 'error': f'No deck builder for {game.label}'}), 400
    args = request.args
    # kind=arena: the search is for an Arena deck - "owned" is what the digital collection has,
    # and only other Arena decks can be using a card
    kind = 'arena' if args.get('kind') == 'arena' else 'paper'
    owned = collection_of(kind).owned_by_name(game.id)
    # free=1: leave out the cards other decks use (deck_id: the deck being built, which doesn't count)
    taken = None
    if args.get('free'):
        current = int(args['deck_id']) if (args.get('deck_id') or '').isdigit() else None
        taken = [key for key, used in deck_store.needed_by_name(game.id, commander_formats(game), kind).items()
                 if any(deck['deck_id'] != current for deck in used)]
    try:
        cmc = float(args['cmc']) if args.get('cmc') not in (None, '') else None
        offset = max(0, int(args.get('offset') or 0))
        cards, more = game.search_cards(
            text=args.get('q'), type_text=args.get('type'), oracle_text=args.get('text'),
            identity=args.get('identity'), colors=args.get('colors'), cmc=cmc, rarity=args.get('rarity'),
            legal_in=args.get('format') or None, names=list(owned) if args.get('owned') else None,
            exclude_names=taken, commander=bool(args.get('commander')), offset=offset)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    # A card that is owned is shown in the printing owned (the search picks one printing per
    # name, which for a card from a preconstructed deck is usually another art)
    printing = collection_of(kind).owned_printing_by_name(game.id)
    details = game.card_details([printing.get(search_key(card['name'])) for card in cards])
    results = []
    for card in cards:
        key = search_key(card['name'])
        payload = {**game.deck_card_payload(card), 'owned': owned.get(key, 0)}
        mine = details.get(printing.get(key)) or {}
        if mine.get('image_uri'):
            payload.update(id=printing[key], image_uri=mine['image_uri'])
        results.append(payload)
    return jsonify({'success': True, 'more': more, 'cards': results})


@app.route('/api/cards/printings')
def card_printings():
    """Every printing of a card (name), newest first, with the copies of each that are owned"""
    game = games.active()
    if not game.deck_formats:
        return jsonify({'success': False, 'error': f'No deck builder for {game.label}'}), 400
    name = request.args.get('name') or ''
    owned = inventory.owned_by_printing(game.id, name)
    return jsonify({'success': True, 'printings': [
        {**printing, 'owned': owned.get(printing['id'], 0)} for printing in game.printings(name)]})


# -- Recommendations (recommendations.py: EDHREC, MTGJSON, Archidekt, Moxfield) --
@app.route('/api/decks/<int:deck_id>/suggestions')
def deck_suggestions(deck_id):
    """Cards played with the deck's commander (EDHREC), with the copies owned; the deck's own cards left out"""
    deck, error = get_deck_or_404(deck_id)
    if error:
        return error
    game = games.get(deck['game'])
    commanders = [card['name'] for card in deck['cards'] if card['board'] == 'commander']
    if not commanders:
        return jsonify({'success': True, 'categories': [], 'message': 'Choose a commander to get suggestions'})
    try:
        found = recommend.commander_cards(commanders)
    except Unavailable as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    if not found:
        return jsonify({'success': True, 'categories': [], 'message': 'EDHREC has no data for this commander'})
    in_deck = {search_key(card['name']) for card in deck['cards']}
    owned = collection_of(deck.get('kind')).owned_by_name(game.id)
    elsewhere = {key for key, used in deck_store.needed_by_name(game.id, commander_formats(game), deck.get('kind') or 'paper').items()
                 if any(other['deck_id'] != deck['id'] for other in used)}
    by_name = game.cards_by_names([card['name'] for category in found['categories'] for card in category['cards']])
    categories = []
    for category in found['categories']:
        cards = []
        for suggestion in category['cards']:
            card = by_name.get(search_key(suggestion['name']))
            if not card or search_key(card['name']) in in_deck:
                continue
            cards.append({**game.deck_card_payload(card), 'inclusion': suggestion['inclusion'],
                          'synergy': suggestion['synergy'], 'owned': owned.get(search_key(card['name']), 0),
                          'elsewhere': search_key(card['name']) in elsewhere})
        if cards:
            categories.append({'title': category['title'], 'cards': cards})
    return jsonify({'success': True, 'decks': found['decks'], 'url': found['url'], 'categories': categories})
