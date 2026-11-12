# ============================================================================
# FILE: core/card_names.py
# What is kept by a card's name - the collection's and the scanned cards' entries, the Arena
# collection, other people's cards, the decks - follows its printing when the card data renames
# it. Since 2026-11-11 the import names a reversible printing (Secret Lair's two-sided cards) as
# the card it is: "Ajani Goldmane", where Scryfall writes "Ajani Goldmane // Ajani Goldmane".
# Entries kept before still had the old name: counted apart from the card (owned_by_name groups
# by name), and a deck's entry no longer found. A Codex review of 2026-11-11 found it; the live
# collection had one such entry (Teferi's Ageless Insight).
# ============================================================================
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CHUNK = 500


def _stale(conn, table, names, columns):
    """The rows of a table whose printing (card_id) the card data now names otherwise: [(row, new name)]"""
    found = []
    ids = list(names)
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE card_id IN ({', '.join('?' * len(chunk))})",
                                chunk):
            if row['card_name'] != names[row['card_id']]:
                found.append((row, names[row['card_id']]))
    return found


KEY_COLUMNS = ('game', 'set_name', 'card_number', 'condition', 'finish', 'location')   # with card_name: an entry's key


def adopt(names, inventories=(), decks=None, others=None):
    """
    Give what is kept by name the name its printing has now. names: {card_id: name} - the printings the card data
    renamed (CardDatabase.renamed_printings). Only rows of those printings that still have another name change: a run
    with nothing to do changes nothing.

    inventories: InventoryManagers (collection, scanned cards, Arena). Each entry is read again and changed under its
    inventory's lock, in one transaction of its own: an edit made meanwhile (another printing, more copies - the
    person's, or a station's) is never undone or split by a name read before it, and one entry that cannot change
    never takes the others back. The entry keeps its row: its name changes in place, so a trade holding its copies, its
    cameras and its captures stay as they are. Only when an entry of the new name has the same key (the printing added
    again since) is it merged into that one (update_card: copies, cameras, captures and tags along) - which a trade
    holding copies of it refuses: then it keeps its name and is said in 'waiting', tried again later (the update
    schedule's tick: card_data.retry_waiting_card_names).
    decks: the DeckManager - a deck entry takes the new name, or is added to the deck's entry of that name on the same
    board; a deck's cover follows. others: other people's collections - their name only (their key is theirs).
    Each inventory, the decks and the others are done on their own: one that fails (its file locked, its table not as
    expected) is said in 'errors' and never keeps the rest from changing, nor what is waiting from being said - the
    caller tries again while there are errors (card_data.adopt_card_names).
    Returns {'entries', 'deck_cards', 'covers', 'others': how many changed, 'kept': how many could not,
    'waiting': [{'inventory', 'entry', 'name', 'new_name', 'why'}] of those, 'errors': [what failed: why]}.
    """
    counts = {'entries': 0, 'deck_cards': 0, 'covers': 0, 'others': 0, 'kept': 0, 'waiting': [], 'errors': []}
    if not names:
        return counts
    for number, store in enumerate(inventories):
        if not store:
            continue
        try:
            _adopt_entries(store, names, counts)
        except Exception as e:
            counts['errors'].append(f"inventory {getattr(store, 'db_file', number)}: {e}")
    for what, store, section in (('decks', decks, _adopt_decks), ('others', others, _adopt_others)):
        if store:
            try:
                section(store, names, counts)
            except Exception as e:
                counts['errors'].append(f'{what}: {e}')
    return counts


def _adopt_entries(store, names, counts):
    """One inventory's entries (see adopt)"""
    with store._lock:
        candidates = [(row['id'], name) for row, name in _stale(store.conn, 'inventory', names, ('id', 'card_id', 'card_name'))]
    for row_id, name in candidates:
        with store._lock:   # read again and changed as one: no edit in between
            row = store.conn.execute(
                f"SELECT id, quantity, {', '.join(dict.fromkeys(store.PRINTING_COLUMNS + KEY_COLUMNS))} "
                'FROM inventory WHERE id = ?', (row_id,)).fetchone()
            if row is None or names.get(row['card_id']) != name or row['card_name'] == name:
                continue    # gone, another printing, or renamed meanwhile: nothing of this run's to do
            twin = store.conn.execute(
                f"SELECT id FROM inventory WHERE card_name = ? AND {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)} AND id != ?",
                (name, *(row[c] for c in KEY_COLUMNS), row_id)).fetchone()
            try:
                if twin is None:
                    with store._transaction():
                        store.conn.execute('UPDATE inventory SET card_name = ? WHERE id = ?', (name, row_id))
                else:
                    printing = {column: row[column] for column in store.PRINTING_COLUMNS}
                    done = store.update_card(row_id, split_quantity=row['quantity'],
                                             printing={**printing, 'card_name': name}, quiet=True)
                    if not done.get('success'):
                        raise RuntimeError(done.get('message'))
                counts['entries'] += 1
            except Exception as e:     # a trade holds copies of it (TRADE_HELD): the merge would delete the entry
                counts['kept'] += 1
                counts['waiting'].append({'inventory': Path(str(store.db_file)).name, 'entry': row_id, 'name': row['card_name'],
                                      'new_name': name, 'why': str(e)})


def _adopt_decks(decks, names, counts):
    """The decks' entries and covers (see adopt): one transaction - counted only once it is committed"""
    cards = covers = 0
    with decks._transaction():
        for row, name in _stale(decks.conn, 'deck_cards', names, ('id', 'deck_id', 'board', 'quantity', 'card_id', 'card_name')):
            twin = decks.conn.execute('SELECT id FROM deck_cards WHERE deck_id = ? AND card_name = ? AND board = ?',
                                      (row['deck_id'], name, row['board'])).fetchone()
            if twin:
                decks.conn.execute('UPDATE deck_cards SET quantity = quantity + ? WHERE id = ?', (row['quantity'], twin['id']))
                decks.conn.execute('DELETE FROM deck_cards WHERE id = ?', (row['id'],))
            else:
                decks.conn.execute('UPDATE deck_cards SET card_name = ? WHERE id = ?', (name, row['id']))
            cards += 1
            covers += decks.conn.execute('UPDATE decks SET cover = ? WHERE id = ? AND cover = ?',
                                         (name, row['deck_id'], row['card_name'])).rowcount
    counts['deck_cards'] += cards
    counts['covers'] += covers


def _adopt_others(others, names, counts):
    """Other people's cards (see adopt): one transaction - counted only once it is committed"""
    changed = 0
    with others._transaction():
        for row, name in _stale(others.conn, 'other_cards', names, ('id', 'card_id', 'card_name')):
            others.conn.execute('UPDATE other_cards SET card_name = ? WHERE id = ?', (name, row['id']))
            changed += 1
    counts['others'] += changed
