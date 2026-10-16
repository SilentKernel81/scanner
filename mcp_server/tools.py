"""
What the assistant can ask the card scanner about: the decks and the collection.

Every tool is a plain function taking the server's `ScannerApi` first, so that the tests can use
a stand-in and the MCP library is only needed in card_scanner_mcp.py. Nothing here changes
anything: the tools read, and `propose_deck_changes` works out what a change WOULD do and says
so - the person makes it on the decks page.

Answers are kept short on purpose (names, numbers, short lists): a local model does badly with
long tool results, and a cloud one is slower and costlier with them.
"""
import re
import time

from scanner_api import ScannerError

# ----------------------------------------------------------------------------
# Finding a deck, a card
# ----------------------------------------------------------------------------


def normal(text):
    """A name as typed -> what is compared: lower case, letters and digits only ("Giada, Font of Hope" = "giada font of hope")"""
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


def front(name):
    """The front face of a double-faced card's name ("Delver of Secrets // Insectile Aberration")"""
    return normal((name or '').split(' // ')[0])


def deck_list(api):
    return api.get('/api/decks')['decks']


def find_deck(api, deck):
    """
    A deck's id from what the person said: its id, its name (or part of it), or its commander.
    Says which decks match when it is not one.
    """
    decks = deck_list(api)
    wanted = str(deck).strip()
    if wanted.isdigit():
        for item in decks:
            if item['id'] == int(wanted):
                return item
    key = normal(wanted)
    exact = [item for item in decks if normal(item['name']) == key]
    if len(exact) == 1:
        return exact[0]
    part = [item for item in decks if key and (key in normal(item['name'])
                                               or any(key in normal(name) for name in item.get('commanders') or []))]
    if len(part) == 1:
        return part[0]
    if not part:
        raise ScannerError(f"No deck matches '{deck}'. The decks are: " + ', '.join(f"{item['name']} (#{item['id']})" for item in decks))
    raise ScannerError(f"Several decks match '{deck}': " + ', '.join(f"{item['name']} (#{item['id']})" for item in part)
                       + ". Say which one (its name or id).")


def load_deck(api, deck):
    """The deck as the server's page counts it: cards with copies owned, issues, totals"""
    item = find_deck(api, deck)
    return api.get(f"/api/decks/{item['id']}")['deck']


def card_line(entry, text=False):
    """One card of a deck, as short as it can be said"""
    card = entry.get('card') or {}
    line = {'name': entry['name'], 'quantity': entry['quantity'], 'owned': entry['owned'],
            'type': card.get('type_line'), 'cost': card.get('mana_cost') or '', 'cmc': card.get('cmc'),
            'price': card.get('price')}
    # Another deck wanting a basic land is no news (every deck does): left out; a long list is cut
    if entry.get('elsewhere') and 'Basic' not in (card.get('type_line') or ''):
        names = [other['deck'] for other in entry['elsewhere']]
        line['also_in_decks'] = names[:4] + ([f'and {len(names) - 4} more'] if len(names) > 4 else [])
    if text:
        line['text'] = card.get('oracle_text') or ''
    return line


# ----------------------------------------------------------------------------
# Decks
# ----------------------------------------------------------------------------


def list_decks(api):
    """Every deck: id, name, format, commander, number of cards and how much of it you own"""
    return [{'id': item['id'], 'name': item['name'], 'format': item['format'], 'kind': item['kind'],
             'commanders': item.get('commanders') or [], 'cards': item['cards'],
             'owned': item['owned'], 'owned_percent': item['owned_percent']} for item in deck_list(api)]


def get_deck(api, deck, with_card_text=False):
    """A deck's cards, by board (main / commander / side), with the copies you own, what it costs and its problems"""
    data = load_deck(api, deck)
    boards = {}
    for entry in data['cards']:
        boards.setdefault(entry['board'], []).append(card_line(entry, with_card_text))
    for cards in boards.values():
        cards.sort(key=lambda card: (card.get('cmc') or 0, card['name']))
    return {'id': data['id'], 'name': data['name'], 'format': data['format'], 'kind': data['kind'],
            'notes': data.get('notes') or '', 'totals': data['totals'], 'issues': data['issues'], 'boards': boards}


def check_deck(api, deck):
    """The format's problems with a deck (legality, copies, colours), and what is missing from your collection for it"""
    data = load_deck(api, deck)
    return {'name': data['name'], 'format': data['format'], 'issues': data['issues'] or 'No problems found',
            'totals': data['totals']}


def missing_cards(api, deck):
    """The cards of a deck you do not own (enough copies of), with what they cost"""
    data = load_deck(api, deck)
    wanted = {}
    for entry in data['cards']:
        if entry['board'] == 'side' and data['format'] in ('commander', 'brawl', 'oathbreaker', 'duel', 'paupercommander'):
            continue   # the side board of a commander deck is not played
        slot = wanted.setdefault(entry['name'], {'quantity': 0, 'owned': entry['owned'], 'price': (entry.get('card') or {}).get('price') or 0})
        slot['quantity'] += entry['quantity']
    missing = [{'name': name, 'missing': slot['quantity'] - slot['owned'], 'price_each': slot['price']}
               for name, slot in sorted(wanted.items()) if slot['quantity'] > slot['owned']]
    return {'deck': data['name'], 'missing_cards': missing, 'copies_missing': sum(card['missing'] for card in missing),
            'cost_of_the_missing': round(sum(card['missing'] * (card['price_each'] or 0) for card in missing), 2)}


def suggest_for_deck(api, deck, per_category=8, only_owned=False):
    """
    Cards players put in a deck with the same commander (EDHREC), with how often (inclusion %) and
    how specific to this commander (synergy), marked with the copies you own and whether another
    of your decks uses them. Needs the internet on the server; slow the first time (a few seconds).
    """
    item = find_deck(api, deck)
    answer = api.get(f"/api/decks/{item['id']}/suggestions")
    if not answer.get('categories'):
        return {'deck': item['name'], 'message': answer.get('message') or 'No suggestions for this deck'}
    categories = []
    for category in answer['categories']:
        cards = [{'name': card['name'], 'type': card.get('type_line'), 'cost': card.get('mana_cost') or '',
                  'inclusion_percent': card.get('inclusion'), 'synergy': card.get('synergy'),
                  'owned': card.get('owned', 0), 'used_by_another_deck': bool(card.get('elsewhere')),
                  'price': card.get('price')}
                 for card in category['cards'] if card.get('owned', 0) > 0 or not only_owned]
        if cards:
            categories.append({'category': category['title'], 'cards': cards[:max(1, int(per_category))]})
    return {'deck': item['name'], 'source': answer.get('url'), 'decks_it_is_based_on': answer.get('decks'),
            'categories': categories}


def what_can_i_build(api, kind='commanders', wait_seconds=40, limit=10):
    """
    Decks your collection could make. kind: 'commanders' (legendary creatures you own, ranked by how
    much of their popular deck you own) or 'precons' (preconstructed decks, by the share you own).
    It asks other sites one card at a time, so it can take minutes: this waits up to wait_seconds
    and then returns what it has with running=true - ask again to see more.
    """
    if kind not in ('commanders', 'precons'):
        raise ScannerError("kind must be 'commanders' or 'precons'")
    # The search is a background job on the server: start it, then look at it for a while. Its
    # answers are cached there, so asking again is quick.
    api.post(f'/api/decks/ideas/{kind}', {})
    deadline = time.time() + max(1, int(wait_seconds))
    while True:
        state = api.get_fresh(f'/api/decks/ideas/{kind}')   # never kept: it changes while it runs
        if not state.get('running') or time.time() > deadline:
            break
        time.sleep(2)
    items = state.get('items') or []
    return {'kind': kind, 'running': bool(state.get('running')), 'checked': state.get('done'), 'of': state.get('total'),
            'error': state.get('error'),
            'best': [{key: item.get(key) for key in ('name', 'commander', 'type', 'owned', 'total', 'percent', 'fit', 'url') if key in item}
                     for item in items[:max(1, int(limit))]]}


def propose_deck_changes(api, deck, add=None, remove=None):
    """
    Work out what a change to a deck WOULD do - nothing is changed. add / remove: lists of
    {'name': ..., 'quantity': 1}. Says, for each card added: whether it exists, whether it is legal in
    the deck's format, within the commander's colours, how many copies you own, and which other decks
    of yours use it; and the deck's size before and after. The person makes the change on the decks page.
    """
    data = load_deck(api, deck)
    commander_cards = [entry for entry in data['cards'] if entry['board'] == 'commander']
    commander_format = bool(commander_cards)
    commander_identity = set()
    for entry in commander_cards:
        commander_identity |= set((entry.get('card') or {}).get('identity') or [])
    # What the deck plays (the side board of a commander deck is not played)
    playing = {front(entry['name']): {'name': entry['name'], 'quantity': entry['quantity'], 'owned': entry['owned']}
               for entry in data['cards'] if not (commander_format and entry['board'] == 'side')}
    cards_before = sum(card['quantity'] for card in playing.values())
    # Which decks use each card you own, by name (the collection entries say)
    users = {}
    for entry in collection(api):
        for deck_name in entry.get('decks') or []:
            users.setdefault(front(entry['name']), set()).add(deck_name)

    added, removed, problems = [], [], 0
    for item in (add or []):
        name, quantity = str(item.get('name', '')).strip(), max(1, int(item.get('quantity') or 1))
        report = {'name': name, 'quantity': quantity}
        found = api.get('/api/cards/search', q=name, format=data['format'], kind=data['kind'])['cards']
        match = next((card for card in found if front(card['name']) == front(name)), None)
        if match is None:
            # Not among the cards legal in the format: is it a card at all?
            anywhere = api.get('/api/cards/search', q=name, kind=data['kind'])['cards']
            real = next((card for card in anywhere if front(card['name']) == front(name)), None)
            report['name'] = real['name'] if real else name
            report['problem'] = f"not legal in {data['format']}" if real else 'no card with this name in the card database'
            problems += 1
            added.append(report)
            continue
        report.update(name=match['name'], copies_owned=match.get('owned', 0), price=match.get('price'))
        identity = set(match.get('identity') or [])
        if commander_format and not identity <= commander_identity:
            report['problem'] = ("outside the commander's colours: the card is "
                                 f"{''.join(sorted(identity)) or 'colourless'}, the deck {''.join(sorted(commander_identity)) or 'colourless'}")
            problems += 1
        existing = playing.get(front(match['name']))
        if existing:
            report['already_in_deck'] = existing['quantity']
            # Commander formats play one copy of a card (basic lands excepted)
            if commander_format and 'Basic' not in (match.get('type_line') or ''):
                report['problem'] = report.get('problem', '') + ('; ' if report.get('problem') else '') \
                    + 'already in the deck (a commander deck plays one copy of a card)'
                problems += 1
        others = sorted(users.get(front(match['name']), set()) - {data['name']})
        if others:
            report['also_used_by'] = others[:4] + ([f'and {len(others) - 4} more'] if len(others) > 4 else [])
        playing[front(match['name'])] = {'name': match['name'], 'owned': match.get('owned', 0),
                                         'quantity': (existing['quantity'] if existing else 0) + quantity}
        added.append(report)
    for item in (remove or []):
        name, quantity = str(item.get('name', '')).strip(), max(1, int(item.get('quantity') or 1))
        existing = playing.get(front(name))
        if not existing:
            removed.append({'name': name, 'problem': 'not in the deck'})
            problems += 1
            continue
        new_quantity = max(0, existing['quantity'] - quantity)
        removed.append({'name': existing['name'], 'copies_removed': existing['quantity'] - new_quantity})
        if new_quantity:
            playing[front(name)] = {**existing, 'quantity': new_quantity}
        else:
            del playing[front(name)]
    cards_after = sum(card['quantity'] for card in playing.values())
    # Copies the deck would need beyond what you own, after the change
    missing_after = sum(max(0, card['quantity'] - card['owned']) for card in playing.values())
    return {'deck': data['name'], 'format': data['format'], 'nothing_was_changed': True,
            'add': added, 'remove': removed, 'problems': problems,
            'cards_before': cards_before, 'cards_after': cards_after, 'copies_missing_after': missing_after,
            'note': 'Nothing was changed: make the change on the decks page.'}


# ----------------------------------------------------------------------------
# The collection
# ----------------------------------------------------------------------------


def collection(api):
    return api.get('/api/inventory')['cards']


def short_entry(card):
    """A collection entry as the assistant needs it"""
    return {'name': card['name'], 'set': (card.get('set_code') or '').upper(), 'number': card.get('number'),
            'finish': card.get('finish'), 'copies': card.get('quantity'), 'condition': card.get('condition'),
            'location': card.get('location') or '', 'tags': card.get('tags') or [], 'price': card.get('price'),
            'rarity': card.get('rarity'), 'type': card.get('type_line'), 'used_by_decks': card.get('decks') or []}


def search_collection(api, text='', set_code='', rarity='', card_type='', tag='', location='', in_a_deck=None, limit=25):
    """
    Search the cards you own. text: part of a name (or oracle text is not searched here - use the
    decks page for that); set_code: e.g. 'HOB'; rarity: common/uncommon/rare/mythic; card_type: e.g.
    'Creature'; tag, location: as saved on the entry; in_a_deck: true = only cards a deck uses, false =
    only cards no deck uses. Returns up to `limit` entries and how many matched.
    """
    wanted = normal(text)
    found = []
    for card in collection(api):
        if wanted and wanted not in normal(card['name']):
            continue
        if set_code and (card.get('set_code') or '').lower() != set_code.lower():
            continue
        if rarity and (card.get('rarity') or '').lower() != rarity.lower():
            continue
        if card_type and card_type.lower() not in (card.get('type_line') or '').lower():
            continue
        if tag and tag.lower() not in [t.lower() for t in card.get('tags') or []]:
            continue
        if location and location.lower() not in (card.get('location') or '').lower():
            continue
        if in_a_deck is not None and bool(card.get('decks')) != bool(in_a_deck):
            continue
        found.append(card)
    found.sort(key=lambda card: (-(card.get('price') or 0) * (card.get('quantity') or 1)))
    return {'matches': len(found), 'copies': sum(card.get('quantity') or 0 for card in found),
            'shown': min(len(found), int(limit)), 'sorted_by': 'total value, highest first',
            'cards': [short_entry(card) for card in found[:max(1, int(limit))]]}


def find_card(api, name):
    """Everything about one card: the copies you own (printing, finish, where they are), the decks using them, and the card's text"""
    key = front(name)
    owned = [card for card in collection(api) if front(card['name']) == key]
    details = None
    try:
        found = api.get('/api/cards/search', q=name)['cards']
        details = next((card for card in found if front(card['name']) == key), None)
    except ScannerError:
        pass   # the collection part is still an answer
    return {'name': (details or (owned[0] if owned else {})).get('name', name),
            'copies_owned': sum(card.get('quantity') or 0 for card in owned),
            'entries': [short_entry(card) for card in owned],
            'card': None if not details else {'type': details.get('type_line'), 'cost': details.get('mana_cost'),
                                              'colours': details.get('colors'), 'identity': details.get('identity'),
                                              'rarity': details.get('rarity'), 'price': details.get('price'),
                                              'text': details.get('oracle_text')}}


def collection_summary(api, top=8):
    """How big the collection is and what it is made of: cards, value, the biggest sets, rarities, types, locations"""
    cards = collection(api)
    stats = api.get('/api/stats')['collection']

    def tally(key, weight=lambda card: card.get('quantity') or 0):
        counts = {}
        for card in cards:
            counts[key(card)] = counts.get(key(card), 0) + weight(card)
        return sorted(counts.items(), key=lambda pair: -pair[1])

    def main_type(card):
        line = (card.get('type_line') or '').split(' // ')[0]
        for kind in ('Creature', 'Land', 'Instant', 'Sorcery', 'Artifact', 'Enchantment', 'Planeswalker', 'Battle'):
            if kind in line:
                return kind
        return 'Other'
    return {'cards': stats.get('total_cards'), 'different_entries': stats.get('unique_cards'),
            'value_usd': stats.get('total_value'),
            'by_set': tally(lambda card: card.get('set_name') or '?')[:top],
            'by_rarity': tally(lambda card: card.get('rarity') or '?'),
            'by_type': tally(main_type),
            'by_location': tally(lambda card: card.get('location') or '(none)')[:top],
            'in_a_deck': sum(card.get('quantity') or 0 for card in cards if card.get('decks'))}
