# ============================================================================
# FILE: others.py
# Other people's collections, imported from a file to trade with them or to buy from them
# (the Trades tab), and the list of cards to buy.
#
# Kept apart from everything the user owns: their own file (data/others.db), their own tables,
# their own ids - an id here means nothing in the collection, and no route that changes the
# collection takes one (server/others_api.py is the only way in). What leaves this file is a
# snapshot of a card in the shape the pages and the exporters know (card_dict): a trade that
# receives a card (trades.py: trade_receives) and the buy list keep that snapshot, so deleting an
# imported collection never changes either.
# ============================================================================
import json
import threading

from card_scanner.core import storage
from card_scanner.config import Config
from card_scanner.core.inventory import now

OTHERS_FILE = Config.DATA_DIR / 'others.db'

CARD_COLUMNS = ('card_id', 'card_name', 'set_name', 'set_code', 'card_number', 'rarity', 'type_line',
                'mana_cost', 'colors', 'color_identity', 'price_usd', 'quantity', 'condition', 'finish')

SORTS = {
    'name': 'card_name COLLATE NOCASE, set_name, card_number',
    'price': 'price_usd * quantity DESC, card_name COLLATE NOCASE',
    'price_each': 'price_usd DESC, card_name COLLATE NOCASE',
    'set': 'set_name COLLATE NOCASE, CAST(card_number AS INTEGER), card_number',
    'quantity': 'quantity DESC, card_name COLLATE NOCASE',
}


class OthersError(Exception):
    """What the page shows as it is"""


def card_key(values):
    """What makes two rows of a file the same card: the printing, the condition and the finish"""
    printing = values.get('card_id') or f"{values['card_name']}|{values.get('set_name') or ''}|{values.get('card_number') or ''}"
    return f"{printing}|{values['condition']}|{values['finish']}".lower()


def card_dict(row, collection=None):
    """A row of other_cards as the pages and the exporters know a card (inventory._row_dict) -
    with 'seller' and 'row' (its id here, which is not an id of the collection)"""
    return {
        'id': None, 'row': row['id'], 'game': row['game'] if 'game' in row.keys() else None,
        'card_id': row['card_id'], 'name': row['card_name'], 'set_name': row['set_name'],
        'set_code': row['set_code'] or '', 'number': row['card_number'], 'rarity': row['rarity'] or '',
        'type_line': row['type_line'] or '', 'mana_cost': row['mana_cost'] or '', 'colors': row['colors'] or '',
        'color_identity': row['color_identity'] or '', 'price': row['price_usd'] or 0.0,
        'quantity': row['quantity'], 'condition': row['condition'], 'finish': row['finish'],
        'timestamp': '', 'location': '', 'tags': [], 'added_at': '', 'added_quantity': 0, 'batches': [],
        'seller': collection,
    }


class Others(storage.Transactional):
    def __init__(self, db_file=None):
        self.conn = storage.connect(db_file or OTHERS_FILE)
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS other_collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,
                    name TEXT NOT NULL,
                    format TEXT,                 -- what the file was (Moxfield, ManaBox, Card Scanner)
                    file TEXT,                   -- its name, as a reminder
                    imported_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_other_collections_name ON other_collections(game, name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS other_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection_id INTEGER NOT NULL REFERENCES other_collections(id) ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    card_id TEXT,
                    card_name TEXT NOT NULL,
                    set_name TEXT NOT NULL DEFAULT '',
                    set_code TEXT,
                    card_number TEXT NOT NULL DEFAULT '',
                    rarity TEXT, type_line TEXT, mana_cost TEXT, colors TEXT, color_identity TEXT,
                    price_usd REAL NOT NULL DEFAULT 0,
                    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
                    condition TEXT NOT NULL DEFAULT 'Near Mint',
                    finish TEXT NOT NULL,
                    UNIQUE (collection_id, key)
                );
                CREATE INDEX IF NOT EXISTS idx_other_cards_name ON other_cards(collection_id, card_name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS buy_list (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,
                    seller TEXT NOT NULL,        -- the imported collection's name when it was added
                    key TEXT NOT NULL,
                    card TEXT NOT NULL,          -- JSON: the card as it was (card_dict)
                    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
                    most INTEGER NOT NULL,       -- how many the seller has
                    added_at TEXT NOT NULL,
                    UNIQUE (game, seller, key)
                );
            ''')
            self.conn.commit()

    # ------------------------------------------------------------------------
    # Imported collections
    # ------------------------------------------------------------------------

    def import_collection(self, game, name, entries, format_name='', file_name='', replace=False):
        """
        Keep a collection read from a file (entries: dicts with the CARD_COLUMNS) under a name.
        A name that is there already is refused, unless replace: then its cards are swapped for
        these in the same edit. Rows of the same card are added up. Returns {'id', 'cards',
        'entries', 'value'}.
        """
        name = ' '.join(str(name or '').split())[:60]
        if not name:
            raise OthersError('The collection needs a name (whose it is)')
        rows = {}
        for values in entries:
            values = {**values, 'condition': values.get('condition') or 'Near Mint'}
            key = card_key(values)
            if key in rows:
                rows[key]['quantity'] += values['quantity']
            else:
                rows[key] = {**values}
        if not rows:
            raise OthersError('No card of the file could be used')

        with self._transaction():
            existing = self.conn.execute('SELECT id FROM other_collections WHERE game = ? AND name = ? COLLATE NOCASE',
                                         (game, name)).fetchone()
            if existing and not replace:
                raise OthersError(f'There is a collection called "{name}" already')
            if existing:
                self.conn.execute('DELETE FROM other_cards WHERE collection_id = ?', (existing['id'],))
                self.conn.execute('UPDATE other_collections SET format = ?, file = ?, imported_at = ? WHERE id = ?',
                                  (format_name, file_name, now(), existing['id']))
                collection_id = existing['id']
            else:
                collection_id = self.conn.execute(
                    'INSERT INTO other_collections (game, name, format, file, imported_at) VALUES (?, ?, ?, ?, ?)',
                    (game, name, format_name, file_name, now())).lastrowid
            self.conn.executemany(
                f"INSERT INTO other_cards (collection_id, key, {', '.join(CARD_COLUMNS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(CARD_COLUMNS))})",
                ((collection_id, key, *(values.get(column) for column in CARD_COLUMNS)) for key, values in rows.items()))
        return {'id': collection_id, 'name': name, 'entries': len(rows),
                'cards': sum(values['quantity'] for values in rows.values()),
                'value': round(sum((values.get('price_usd') or 0) * values['quantity'] for values in rows.values()), 2),
                'replaced': bool(existing)}

    def collections(self, game):
        """[{'id', 'name', 'format', 'file', 'imported_at', 'entries', 'cards', 'value'}], by name"""
        with self._lock:
            return [dict(row) for row in self.conn.execute('''
                SELECT c.id, c.name, c.format, c.file, c.imported_at, COUNT(k.id) AS entries,
                       COALESCE(SUM(k.quantity), 0) AS cards, ROUND(COALESCE(SUM(k.price_usd * k.quantity), 0), 2) AS value
                FROM other_collections c LEFT JOIN other_cards k ON k.collection_id = c.id
                WHERE c.game = ? GROUP BY c.id ORDER BY c.name COLLATE NOCASE''', (game,))]

    def collection(self, collection_id):
        with self._lock:
            row = self.conn.execute('SELECT * FROM other_collections WHERE id = ?', (collection_id,)).fetchone()
        if not row:
            raise OthersError('This collection is not here any more')
        return dict(row)

    def delete(self, collection_id):
        """Forget an imported collection. Trades and the buy list keep the cards they took from it."""
        with self._transaction():
            self.collection(collection_id)
            self.conn.execute('DELETE FROM other_collections WHERE id = ?', (collection_id,))  # its cards go with it

    # ------------------------------------------------------------------------
    # Browsing
    # ------------------------------------------------------------------------

    def cards(self, collection_id, query='', sort='name', offset=0, limit=50):
        """(a page of the collection's cards as card_dict, how many match). query: words that
        must each be in the name, set, type or rarity"""
        collection = self.collection(collection_id)
        where, values = ['collection_id = ?'], [collection_id]
        for word in str(query or '').lower().split():
            where.append("(LOWER(card_name) LIKE ? OR LOWER(set_name) LIKE ? OR LOWER(COALESCE(set_code, '')) = ? "
                         "OR LOWER(COALESCE(type_line, '')) LIKE ? OR LOWER(COALESCE(rarity, '')) = ?)")
            values += [f'%{word}%', f'%{word}%', word, f'%{word}%', word]
        order = SORTS.get(sort, SORTS['name'])
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM other_cards WHERE {' AND '.join(where)}", values).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT * FROM other_cards WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?",
                [*values, max(1, min(int(limit), 200)), max(0, int(offset))]).fetchall()
        return [{**card_dict(row, collection['name']), 'game': collection['game']} for row in rows], total

    def card(self, collection_id, row_id):
        """One card of a collection as card_dict - the row must be that collection's"""
        collection = self.collection(collection_id)
        with self._lock:
            row = self.conn.execute('SELECT * FROM other_cards WHERE id = ? AND collection_id = ?',
                                    (row_id, collection_id)).fetchone()
        if not row:
            raise OthersError('This card is not in the collection any more')
        return {**card_dict(row, collection['name']), 'game': collection['game']}

    # ------------------------------------------------------------------------
    # The buy list
    # ------------------------------------------------------------------------

    def buy(self, collection_id, items):
        """
        Put cards of an imported collection on the buy list: items [(row id, copies)] - the copies
        add to what the list holds of that card from that seller, never above what the seller has.
        Returns {'cards': copies on the list now for these, 'seller'}.
        """
        added = 0
        with self._transaction():
            collection = self.collection(collection_id)
            for row_id, copies in items:
                card = self.card(collection_id, int(row_id))
                copies = max(1, int(copies))
                key = card_key({'card_id': card['card_id'], 'card_name': card['name'], 'set_name': card['set_name'],
                                'card_number': card['number'], 'condition': card['condition'], 'finish': card['finish']})
                self.conn.execute('''
                    INSERT INTO buy_list (game, seller, key, card, quantity, most, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game, seller, key) DO UPDATE SET
                        quantity = MIN(quantity + excluded.quantity, excluded.most), most = excluded.most''',
                                  (collection['game'], collection['name'], key, json.dumps(card),
                                   min(copies, card['quantity']), card['quantity'], now()))
                added += 1
        return {'seller': collection['name'], 'cards': added}

    def buy_list(self, game):
        """The buy list's cards (card_dict with 'row', 'seller' and 'most'), by seller then name"""
        with self._lock:
            rows = self.conn.execute('SELECT * FROM buy_list WHERE game = ? ORDER BY seller COLLATE NOCASE, id', (game,)).fetchall()
        cards = [{**json.loads(row['card']), 'row': row['id'], 'seller': row['seller'], 'quantity': row['quantity'],
                  'most': row['most']} for row in rows]
        return sorted(cards, key=lambda card: (card['seller'].lower(), card['name'].lower(), card['set_name'], card['number']))

    def set_buy_quantity(self, row_id, quantity):
        """How many of one buy-list card; 0 takes it off"""
        with self._transaction():
            row = self.conn.execute('SELECT * FROM buy_list WHERE id = ?', (row_id,)).fetchone()
            if not row:
                raise OthersError('This card is not on the buy list any more')
            if quantity <= 0:
                self.conn.execute('DELETE FROM buy_list WHERE id = ?', (row_id,))
            elif quantity > row['most']:
                raise OthersError(f"{row['seller']} has only {row['most']} of it")
            else:
                self.conn.execute('UPDATE buy_list SET quantity = ? WHERE id = ?', (quantity, row_id))

    def clear_buy_list(self, game, seller=None):
        """Empty the buy list - or only one seller's part of it. Returns the entries removed."""
        with self._transaction():
            if seller:
                return self.conn.execute('DELETE FROM buy_list WHERE game = ? AND seller = ? COLLATE NOCASE',
                                         (game, seller)).rowcount
            return self.conn.execute('DELETE FROM buy_list WHERE game = ?', (game,)).rowcount
