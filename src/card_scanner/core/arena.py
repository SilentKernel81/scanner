"""
The digital collection: what the user has in MTG Arena, read from the files an exporter writes.

Arena itself gives no list of what you own. A third-party exporter reads it while the game
runs and writes, among other files:

    mtga_collection.json   {'export_date', 'cards': [{'count', 'name', 'set',
                           'collector_number', 'rarity', 'rebalanced', ...}],
                           'inventory': {'WildCardCommons', 'WildCardUnCommons',
                           'WildCardRares', 'WildCardMythics', 'Gold', 'Gems', ...}}
    mtga_decks.json        {'export_date', 'decks': [{'id', 'name', 'format',
                           'deck': [{'count', 'name', 'set', 'collector_number', ...}]}]}

(the layout of one real export, 2026-10-13: 1,972 entries, 3 decks). This module reads those
two, finds each entry's card in the card data, and keeps what is known of the account
beside the cards. It writes nothing itself but the account: the cards go into an
InventoryManager of their own (data/arena_inventory.db: InventoryManager.replace_all), the
decks into the deck store (DeckManager.replace_arena_decks) - server/arena_api.py puts the
three together.

The digital collection is a copy. Arena has the original, so an import replaces everything,
and nothing here is changed by hand.
"""
import json

from card_scanner.core.database import search_key

# What an Arena deck's format is here (games/mtg_decks.py: DECK_FORMATS). Explorer became
# Pioneer on Arena; anything not listed - a format Arena adds later, an event's - is
# Historic, which allows nearly every card and so reports the fewest false problems.
FORMATS = {'standard': 'standard', 'historic': 'historic', 'timeless': 'timeless', 'alchemy': 'alchemy',
           'explorer': 'pioneer', 'pioneer': 'pioneer', 'brawl': 'brawl', 'historicbrawl': 'brawl',
           'standardbrawl': 'standardbrawl'}
FALLBACK_FORMAT = 'historic'

# Wildcards by rarity: the export's key, and the name used here and on the pages
WILDCARDS = (('common', 'WildCardCommons'), ('uncommon', 'WildCardUnCommons'),
             ('rare', 'WildCardRares'), ('mythic', 'WildCardMythics'))

ACCOUNT_TABLE = '''
    CREATE TABLE IF NOT EXISTS arena_account (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )'''


class NotAnExport(ValueError):
    """The file is not one of the exporter's - said in words the page can show"""


def read_collection(text):
    """
    The exporter's collection file -> {'exported', 'cards': [{'name', 'set', 'number',
    'quantity', 'rarity', 'rebalanced'}], 'wildcards': {rarity: n}, 'gold', 'gems'}.
    Anything else raises NotAnExport: the digital collection is replaced by what is read
    here, so a file that only looks roughly right must not get that far.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise NotAnExport('This is not the collection file of the Arena exporter (mtga_collection.json): it is not JSON') from e
    cards = data.get('cards') if isinstance(data, dict) else None
    if not isinstance(cards, list) or not all(isinstance(card, dict) and 'name' in card and 'count' in card for card in cards):
        raise NotAnExport('This is not the collection file of the Arena exporter (mtga_collection.json): '
                          'it has no list "cards" with a name and a count each')
    try:
        read = [{'name': str(card['name']), 'set': str(card.get('set') or '').lower(),
                 'number': str(card.get('collector_number') or ''), 'quantity': int(card['count']),
                 'rarity': str(card.get('rarity') or ''), 'rebalanced': bool(card.get('rebalanced'))}
                for card in cards]
    except (ValueError, TypeError) as e:
        raise NotAnExport(f'The collection file has an entry that cannot be read: {e}') from e
    account = data.get('inventory') if isinstance(data.get('inventory'), dict) else {}
    number = lambda key: int(account.get(key) or 0) if str(account.get(key) or 0).lstrip('-').isdigit() else 0
    return {'exported': str(data.get('export_date') or ''), 'cards': [card for card in read if card['quantity'] > 0],
            # A rarity you have none of is left out of the export: it is 0, not unknown
            'wildcards': {rarity: number(key) for rarity, key in WILDCARDS},
            'gold': number('Gold'), 'gems': number('Gems')}


def read_decks(text):
    """
    The exporter's deck file -> [{'arena_id', 'name', 'format' (one of ours), 'cards':
    [{'name', 'set', 'number', 'quantity', 'board'}]}].

    Seen in the one export there was to look at: each deck has one list, 'deck'. Whether the
    exporter writes a sideboard or a commander as lists of their own was not seen (none of
    the three decks had either); 'sideboard' and 'commander' are read if they are there.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise NotAnExport('This is not the deck file of the Arena exporter (mtga_decks.json): it is not JSON') from e
    decks = data.get('decks') if isinstance(data, dict) else None
    if not isinstance(decks, list):
        raise NotAnExport('This is not the deck file of the Arena exporter (mtga_decks.json): it has no list "decks"')
    read = []
    for deck in decks:
        if not isinstance(deck, dict) or not deck.get('id'):
            raise NotAnExport('The deck file has a deck without the id Arena gives it')
        cards = []
        for key, board in (('commander', 'commander'), ('deck', 'main'), ('sideboard', 'side')):
            for card in deck.get(key) or []:
                try:
                    cards.append({'name': str(card['name']), 'set': str(card.get('set') or '').lower(),
                                  'number': str(card.get('collector_number') or ''),
                                  'quantity': int(card['count']), 'board': board})
                except (KeyError, ValueError, TypeError) as e:
                    raise NotAnExport(f"The deck {deck.get('name')!r} has a card that cannot be read: {e}") from e
        style = ''.join(ch for ch in str(deck.get('format') or '').lower() if ch.isalnum())
        read.append({'arena_id': str(deck['id']), 'name': str(deck.get('name') or 'Arena deck')[:80],
                     'format': FORMATS.get(style, FALLBACK_FORMAT),
                     'cards': [card for card in cards if card['quantity'] > 0]})
    return read


def find_card(game, database, entry, by_name):
    """
    The card of one of the export's entries ({'name', 'set', 'number'}), and how it was
    found: (card, 'printing' | 'name') or (None, None).

    Arena's set and collector number are believed only when the card that has them in the
    card data carries the entry's name. They are usually the printed set's - 1,891 of 1,972
    entries of the export this was written against - but not always: Arena numbers the basic
    lands of some sets its own way (its Forest LCI #400 is the printed set's Mountain), and
    a rebalanced card ("A-Ancestral Katana") has the number of the card it changes. A
    double-faced or adventure card goes by its front name in Arena ("The Arkenstone") and by
    both in the card data ("The Arkenstone // Seek the Heart"): fronts are compared.
    Otherwise the name decides, in whatever printing the card data shows a card by - so a
    card that only exists in one of Arena's digital sets is still found if it was ever
    printed, and shown as that printing.
    by_name: {search_key(name): card} for the entries' names (Game.cards_by_names).
    """
    front = lambda name: search_key(name.split(' // ')[0])
    name = entry['name'][2:] if entry['name'].startswith('A-') else entry['name']
    if entry['set'] and entry['number']:
        card = database.get_card_by_set_number(entry['set'], entry['number'], exact=True)
        if card and front(card['name']) == front(name):
            return card, 'printing'
    card = by_name.get(search_key(name))
    return (card, 'name') if card else (None, None)


def resolve(game, database, entries, skip_rebalanced=True):
    """
    The export's entries as cards of the card data: (found, summary).

    found: [{'card', 'quantity', 'board' (decks only), 'name' (as the export wrote it)}];
    summary: {'by_printing', 'by_name', 'rebalanced' (entries skipped), 'unknown': [names]}.

    Rebalanced entries are skipped: "A-Ancestral Katana" is the same card as Ancestral
    Katana in the formats that change it, a second face of something already counted (the
    exporter's own totals leave them out too).
    """
    by_name = game.cards_by_names([entry['name'][2:] if entry['name'].startswith('A-') else entry['name'] for entry in entries])
    found, summary = [], {'by_printing': 0, 'by_name': 0, 'rebalanced': 0, 'unknown': []}
    for entry in entries:
        if skip_rebalanced and (entry.get('rebalanced') or entry['name'].startswith('A-')):
            summary['rebalanced'] += 1
            continue
        card, how = find_card(game, database, entry, by_name)
        if not card:
            summary['unknown'].append(entry['name'])
            continue
        summary['by_printing' if how == 'printing' else 'by_name'] += 1
        found.append({'card': card, 'quantity': entry['quantity'], 'board': entry.get('board', 'main'), 'name': entry['name']})
    summary['unknown'] = sorted(set(summary['unknown']))
    return found, summary


# -- The account: beside the cards, in the digital collection's own file ------------

def save_account(manager, account):
    """
    What is known of the Arena account at the last import - {'exported', 'imported',
    'wildcards', 'gold', 'gems', 'summary'} - in the digital collection's file. Not
    committed here: the caller writes the cards in the same transaction, so that the cards
    and the date they are from cannot disagree.
    """
    manager.conn.execute(ACCOUNT_TABLE)
    manager.conn.execute('DELETE FROM arena_account')
    manager.conn.executemany('INSERT INTO arena_account (key, value) VALUES (?, ?)',
                             [(key, json.dumps(value)) for key, value in account.items()])


def account(manager):
    """The account as save_account wrote it, or {} when nothing was imported yet"""
    with manager._lock:
        manager.conn.execute(ACCOUNT_TABLE)
        return {row['key']: json.loads(row['value']) for row in manager.conn.execute('SELECT key, value FROM arena_account')}


def wildcards_needed(missing):
    """
    What crafting cards costs: one wildcard of a card's rarity per copy.
    missing: [(rarity, copies)] -> {'common': n, 'uncommon': n, 'rare': n, 'mythic': n}.
    A rarity that is none of the four (a basic land's, a special printing's) counts as
    nothing: basic lands are free in Arena, and the rest cannot be told from here.
    """
    needed = {rarity: 0 for rarity, _key in WILDCARDS}
    for rarity, copies in missing:
        if rarity in needed:
            needed[rarity] += copies
    return needed
