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


def adopt(names, inventories=(), decks=None, others=None):
    """
    Give what is kept by name the name its printing has now. names: {card_id: name} - the printings the card data
    renamed (CardDatabase.renamed_printings). Only rows of those printings that still have another name change: a run
    with nothing to do changes nothing.

    inventories: InventoryManagers (collection, scanned cards, Arena). An entry is given its new name as a change of
    printing to itself under that name (update_card with all its copies): an entry that becomes identical to another one
    (the same printing added again since, under the new name) is merged into it, its cameras, captures and tags along.
    An entry a trade holds copies of cannot be moved: it keeps its name and is logged, the others still change.
    decks: the DeckManager - a deck entry takes the new name, or is added to the deck's entry of that name on the same
    board; a deck's cover follows. others: other people's collections - their name only (their key is theirs).
    Returns {'entries': n, 'deck_cards': n, 'covers': n, 'others': n, 'kept': n}.
    """
    counts = {'entries': 0, 'deck_cards': 0, 'covers': 0, 'others': 0, 'kept': 0}
    if not names:
        return counts
    for store in inventories:
        if not store:
            continue
        columns = ('id', 'quantity', *store.PRINTING_COLUMNS)
        with store._lock:
            stale = _stale(store.conn, 'inventory', names, columns)
        for row, name in stale:
            printing = {column: row[column] for column in store.PRINTING_COLUMNS}
            try:
                done = store.update_card(row['id'], split_quantity=row['quantity'], printing={**printing, 'card_name': name},
                                         quiet=True)
            except Exception as e:     # a trade holds copies of it (TRADE_HELD), or the entry changed meanwhile
                done = {'success': False, 'message': str(e)}
            if done.get('success'):
                counts['entries'] += 1
            else:
                counts['kept'] += 1
                logger.warning(f"{row['card_name']} (entry {row['id']}) keeps its name, not {name}: {done.get('message')}")
    if decks:
        with decks._transaction():
            for row, name in _stale(decks.conn, 'deck_cards', names, ('id', 'deck_id', 'board', 'quantity', 'card_id', 'card_name')):
                twin = decks.conn.execute('SELECT id FROM deck_cards WHERE deck_id = ? AND card_name = ? AND board = ?',
                                          (row['deck_id'], name, row['board'])).fetchone()
                if twin:
                    decks.conn.execute('UPDATE deck_cards SET quantity = quantity + ? WHERE id = ?', (row['quantity'], twin['id']))
                    decks.conn.execute('DELETE FROM deck_cards WHERE id = ?', (row['id'],))
                else:
                    decks.conn.execute('UPDATE deck_cards SET card_name = ? WHERE id = ?', (name, row['id']))
                counts['deck_cards'] += 1
                counts['covers'] += decks.conn.execute('UPDATE decks SET cover = ? WHERE id = ? AND cover = ?',
                                                       (name, row['deck_id'], row['card_name'])).rowcount
    if others:
        with others._transaction():
            for row, name in _stale(others.conn, 'other_cards', names, ('id', 'card_id', 'card_name')):
                others.conn.execute('UPDATE other_cards SET card_name = ? WHERE id = ?', (name, row['id']))
                counts['others'] += 1
    return counts
