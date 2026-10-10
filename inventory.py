# ============================================================================
# FILE: inventory.py
# Inventory management using SQLite database
# ============================================================================
import csv
import re
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from config import Config
from database import search_key
from storage import Transactional, upgrade

logger = logging.getLogger('database')

# Small copies of the captures behind inventory entries, shown when hovering an entry. Kept
# apart from scanned_cards/ (cleaned after cleanup.days) until their entry is deleted
CAPTURES_DIR = Config.DATA_DIR / 'captures'
CAPTURE_HEIGHT = 400  # px - ~25 KB per card

# One row per card + set + number + condition + finish + location, per game. Rows are
# addressed by id. tags: "trade, keep" - not part of the key (see clean_tags). timestamp: when
# the card was scanned; added_at: when copies last came into this inventory. Which copies came
# with which "Add to collection" (import, scan) is kept in inventory_batches - added_quantity
# held that for the latest one only, until the batches table (it is still written, not read)
INVENTORY_TABLE = '''
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game TEXT NOT NULL DEFAULT 'mtg',
        card_id TEXT,
        card_name TEXT NOT NULL,
        set_name TEXT NOT NULL,
        set_code TEXT,
        card_number TEXT NOT NULL DEFAULT '',
        rarity TEXT,
        type_line TEXT,
        mana_cost TEXT,
        colors TEXT,
        color_identity TEXT,
        price_usd REAL,
        quantity INTEGER NOT NULL DEFAULT 1,
        condition TEXT NOT NULL DEFAULT 'Near Mint',
        finish TEXT NOT NULL DEFAULT 'regular',
        timestamp TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '',
        added_at TEXT,
        added_quantity INTEGER,
        UNIQUE(game, card_name, set_name, card_number, condition, finish, location)
    )
'''
KEY_COLUMNS = ('game', 'card_name', 'set_name', 'card_number', 'condition', 'finish', 'location')
UPSERT = '''
    INSERT INTO inventory (game, card_id, card_name, set_name, set_code, card_number, rarity,
                           type_line, mana_cost, colors, color_identity, price_usd, quantity,
                           condition, finish, timestamp, location, tags, added_at,
                           added_quantity)
    VALUES (:game, :card_id, :card_name, :set_name, :set_code, :card_number, :rarity,
            :type_line, :mana_cost, :colors, :color_identity, :price_usd, :quantity,
            :condition, :finish, :timestamp, :location, :tags, :added_at, :quantity)
    ON CONFLICT(game, card_name, set_name, card_number, condition, finish, location) DO UPDATE SET
        quantity = quantity + excluded.quantity,
        added_at = excluded.added_at,
        added_quantity = CASE WHEN added_at = excluded.added_at  -- the same batch again: it brought both
                              THEN COALESCE(added_quantity, quantity) + excluded.quantity
                              ELSE excluded.quantity END,
        tags = CASE WHEN tags = '' THEN excluded.tags ELSE tags END,
        timestamp = excluded.timestamp,
        price_usd = excluded.price_usd,
        card_id = COALESCE(excluded.card_id, card_id),
        set_code = COALESCE(excluded.set_code, set_code)
'''


# A move to the collection that is under way (take_from): noted in the inventory it moves from,
# and - in the commit that brings the cards - in the one it moves to
PENDING_MOVES_TABLE = ('CREATE TABLE IF NOT EXISTS pending_moves '
                       '(game TEXT PRIMARY KEY, move_id TEXT NOT NULL, added_at TEXT NOT NULL)')
ARRIVED_MOVES_TABLE = 'CREATE TABLE IF NOT EXISTS arrived_moves (move_id TEXT PRIMARY KEY)'


# Which copies of an entry came together: one row per batch (an "Add to collection", an import,
# a scanned card) and entry. `batch` is unique for each time cards came in - two in the same
# second are still two - and added_at is when. An entry's rows never add up to more than its
# quantity; copies no row accounts for were there before the oldest batch known ("old:..."
# batches are what the single added_at / added_quantity of an entry said before this table).
BATCHES_TABLE = '''
    CREATE TABLE IF NOT EXISTS inventory_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch TEXT NOT NULL,
        inventory_id INTEGER NOT NULL,
        added_at TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        UNIQUE(batch, inventory_id)
    )
'''
BACKFILL_BATCHES = '''
    INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity)
    SELECT 'old:' || COALESCE(added_at, timestamp), id, COALESCE(added_at, timestamp),
           MIN(COALESCE(added_quantity, quantity), quantity)
    FROM inventory WHERE MIN(COALESCE(added_quantity, quantity), quantity) > 0
'''


def _child_rules(conn, table):
    """Rows of `table` belong to an inventory entry: none without one, and they go with it"""
    for event in ('INSERT', 'UPDATE'):
        conn.execute(f'''
            CREATE TRIGGER IF NOT EXISTS {table}_entry_{event.lower()} BEFORE {event} ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table}: no such inventory entry')
                    WHERE NOT EXISTS (SELECT 1 FROM inventory WHERE id = NEW.inventory_id);
            END''')
    conn.execute(f'''
        CREATE TRIGGER IF NOT EXISTS inventory_takes_{table} AFTER DELETE ON inventory
        BEGIN
            DELETE FROM {table} WHERE inventory_id = OLD.id;
        END''')


def _quantity_rule(conn, table):
    for event in ('INSERT', 'UPDATE'):
        conn.execute(f'''
            CREATE TRIGGER IF NOT EXISTS {table}_quantity_{event.lower()} BEFORE {event} ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table}: the quantity must be a positive whole number')
                    WHERE typeof(NEW.quantity) != 'integer' OR NEW.quantity <= 0;
            END''')


def _add_rules(conn):
    """
    Version 1: the database itself refuses what only the code used to prevent - an entry with
    no or a negative quantity, a capture without its entry - and a deleted entry takes its
    capture rows along (their files are the code's to delete: _delete_entries). Triggers, not
    foreign keys and CHECKs: those would need the tables rebuilt and a PRAGMA on every
    connection that ever writes here.
    """
    _quantity_rule(conn, 'inventory')
    _child_rules(conn, 'inventory_captures')


def _add_batches(conn):
    """Version 2: inventory_batches, filled with the one batch each entry knew of"""
    conn.execute(BATCHES_TABLE)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_batches_entry ON inventory_batches(inventory_id)')
    conn.execute(BACKFILL_BATCHES)
    _quantity_rule(conn, 'inventory_batches')
    _child_rules(conn, 'inventory_batches')


SCHEMA_STEPS = [_add_rules, _add_batches]


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')


def clean_tags(tags):
    """Tags as a list without blanks or repeats (case-insensitive), from a list or "a, b" text"""
    if isinstance(tags, str):
        tags = tags.split(',')
    result, seen = [], set()
    for tag in tags or []:
        tag = str(tag).strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result


def _row_dict(row, batches=()):
    """
    Inventory row as sent to the web page and the exporters. batches: the entry's
    inventory_batches rows, newest first - 'batches' lists them ([{'batch', 'added_at',
    'quantity'}]); 'added_at' / 'added_quantity' are the newest one's (0 copies: what the
    entry holds was there before every batch known)
    """
    batches = [{'batch': batch['batch'], 'added_at': batch['added_at'], 'quantity': batch['quantity']}
               for batch in batches]
    return {
        'id': row['id'],
        'game': row['game'],
        'card_id': row['card_id'],
        'name': row['card_name'],
        'set_name': row['set_name'],
        'set_code': row['set_code'] or '',
        'number': row['card_number'],
        'rarity': row['rarity'] or '',
        'type_line': row['type_line'] or '',
        'mana_cost': row['mana_cost'] or '',
        'colors': row['colors'] or '',
        'color_identity': row['color_identity'] or '',
        'price': row['price_usd'] or 0.0,
        'quantity': row['quantity'],
        'condition': row['condition'],
        'finish': row['finish'],
        'timestamp': row['timestamp'],
        'location': row['location'],
        'tags': clean_tags(row['tags']),
        'added_at': batches[0]['added_at'] if batches else row['added_at'] or row['timestamp'],
        'added_quantity': batches[0]['quantity'] if batches else 0,
        'batches': batches,
    }


class InventoryManager(Transactional):
    """
    Card inventory (table `inventory` in the card database file), for every game. Every edit
    is one transaction (Transactional): all of it, or - when it fails - none of it.
    """

    def __init__(self, db_file=None, log_callback=None):
        self.db_file = db_file or Config.DATABASE_FILE
        self.log_callback = log_callback
        self._lock = threading.RLock()
        self.last_added = None  # (row id, quantity, capture id, batch) of the most recent add_card, for undo
        self._doomed_files = []  # capture files of rows deleted in the edit under way
        self.conn = sqlite3.connect(str(self.db_file), check_same_thread=False, timeout=10.0)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.row_factory = sqlite3.Row
        self._initialize_table()

    def log(self, message, level="info"):
        getattr(logger, level, logger.info)(message)
        if self.log_callback:
            self.log_callback(message, level)

    # ------------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------------

    def _initialize_table(self):
        with self._lock:
            columns = {row['name'] for row in self.conn.execute("PRAGMA table_info(inventory)")}
            if not columns:
                self.conn.execute(INVENTORY_TABLE.format(table='inventory'))
            elif 'finish' not in columns:
                self._migrate_to_multi_game(columns)
            elif 'location' not in columns:
                self._migrate_add_location()
            columns = {row['name'] for row in self.conn.execute("PRAGMA table_info(inventory)")}
            if 'added_at' not in columns:  # not part of the key: no rebuild needed
                self.conn.execute('ALTER TABLE inventory ADD COLUMN added_at TEXT')
            if 'added_quantity' not in columns:
                self.conn.execute('ALTER TABLE inventory ADD COLUMN added_quantity INTEGER')
            self.conn.execute('UPDATE inventory SET added_at = timestamp WHERE added_at IS NULL')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_game_name ON inventory(game, card_name COLLATE NOCASE)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_timestamp ON inventory(timestamp DESC)')
            # One row per captured copy: which entry it belongs to and its thumbnail file
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS inventory_captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inventory_id INTEGER NOT NULL,
                    file TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                )''')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_captures_entry ON inventory_captures(inventory_id)')
            self.conn.commit()
            upgrade(self.conn, 'inventory', SCHEMA_STEPS)

    def _backup_table(self, label):
        """Copy the inventory table to data/backups/ before rebuilding it; returns the file and
        (entries, cards) to compare afterwards"""
        backup_dir = Config.DATA_DIR / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"inventory_before_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        self.conn.execute('ATTACH DATABASE ? AS backup', (str(backup_file),))
        self.conn.execute('CREATE TABLE backup.inventory AS SELECT * FROM main.inventory')
        self.conn.commit()
        self.conn.execute('DETACH DATABASE backup')
        before = self.conn.execute('SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM inventory').fetchone()
        logger.info(f"Inventory backed up to {backup_file} ({before[0]} rows)")
        return backup_file, before

    def _migrate_add_location(self):
        """
        Inventories from before locations: the location joins the UNIQUE key (copies of one
        printing can be in two places), so the table is rebuilt - after a backup, keeping every
        row's id (inventory_captures point at them).
        """
        backup_file, before = self._backup_table('locations')
        old_columns = [row['name'] for row in self.conn.execute("PRAGMA table_info(inventory)")]
        columns = ', '.join(old_columns)
        conn = sqlite3.connect(str(self.db_file), isolation_level=None, timeout=10.0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DROP TABLE IF EXISTS inventory_new')
            conn.execute(INVENTORY_TABLE.format(table='inventory_new'))
            conn.execute(f'INSERT INTO inventory_new ({columns}) SELECT {columns} FROM inventory')
            after = conn.execute('SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM inventory_new').fetchone()
            if tuple(after) != tuple(before):
                raise RuntimeError(f"inventory changed ({tuple(before)} -> {tuple(after)})")
            conn.execute('DROP TABLE inventory')
            conn.execute('ALTER TABLE inventory_new RENAME TO inventory')
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            logger.exception("Inventory migration failed - the old table is unchanged")
            raise
        finally:
            conn.close()
        self.log(f"Inventory upgraded for locations and tags ({before[0]} entries, {before[1]} cards; "
                 f"backup: {Config.shown('backups', backup_file.name)})", level="success")

    def _migrate_to_multi_game(self, columns):
        """
        Inventories from before multi-game support had foil/surge flags and no game column.
        The UNIQUE constraint changes, so the table is rebuilt (SQLite can't alter it) -
        after copying the old table to data/backups/.
        """
        backup_file, before = self._backup_table('multigame')

        surge = 'surge' if 'surge' in columns else '0'
        finish = f"CASE WHEN {surge} = 1 THEN 'surge' WHEN foil = 1 THEN 'foil' ELSE 'regular' END"
        conn = sqlite3.connect(str(self.db_file), isolation_level=None, timeout=10.0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DROP TABLE IF EXISTS inventory_new')
            conn.execute(INVENTORY_TABLE.format(table='inventory_new'))
            # Rows that only differed by a NULL number/condition become duplicates: add them up
            conn.execute(f'''
                INSERT INTO inventory_new (game, card_name, set_name, card_number, rarity, type_line,
                                           mana_cost, colors, color_identity, price_usd, quantity,
                                           condition, finish, timestamp)
                SELECT 'mtg', card_name, set_name, COALESCE(card_number, ''), rarity, type_line,
                       mana_cost, colors, color_identity, price_usd, quantity,
                       COALESCE(condition, 'Near Mint'), {finish}, timestamp
                FROM inventory WHERE true ORDER BY id
                ON CONFLICT(game, card_name, set_name, card_number, condition, finish, location)
                DO UPDATE SET quantity = quantity + excluded.quantity
            ''')
            after = conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM inventory_new').fetchone()[0]
            if after != before[1]:
                raise RuntimeError(f"card count changed ({before[1]} -> {after})")
            conn.execute('DROP TABLE inventory')
            conn.execute('ALTER TABLE inventory_new RENAME TO inventory')
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            logger.exception("Inventory migration failed - the old table is unchanged")
            raise
        finally:
            conn.close()
        self.log(f"Inventory upgraded for multiple games ({before[0]} entries, {before[1]} cards; "
                 f"backup: {Config.shown('backups', backup_file.name)})", level="success")

    # ------------------------------------------------------------------------
    # Adding and undo
    # ------------------------------------------------------------------------

    # ------------------------------------------------------------------------
    # Captures (thumbnails of the copies behind an entry)
    # ------------------------------------------------------------------------

    @staticmethod
    def _make_thumbnail(image_path):
        """Small JPEG copy of a capture in CAPTURES_DIR; its file name, or None"""
        try:
            import cv2
            image = cv2.imread(str(image_path))
            if image is None:
                return None
            height, width = image.shape[:2]
            if height > CAPTURE_HEIGHT:
                image = cv2.resize(image, (round(width * CAPTURE_HEIGHT / height), CAPTURE_HEIGHT),
                                   interpolation=cv2.INTER_AREA)
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.jpg"
            cv2.imwrite(str(CAPTURES_DIR / name), image, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return name
        except Exception as e:
            logger.warning(f"Could not keep the capture {image_path}: {e}")
            return None

    def _committed(self):
        # Only now: a rollback could not have brought the files back
        doomed, self._doomed_files = self._doomed_files, []
        for file in doomed:
            (CAPTURES_DIR / file).unlink(missing_ok=True)

    def _rolled_back(self):
        self._doomed_files = []

    def _delete_captures(self, where, params=()):
        """Delete capture rows matching a condition on inventory_captures; their files go when
        the edit is committed"""
        rows = self.conn.execute(f'SELECT id, file FROM inventory_captures WHERE {where}', params).fetchall()
        for row in rows:
            self.conn.execute('DELETE FROM inventory_captures WHERE id = ?', (row['id'],))
            self._doomed_files.append(row['file'])

    def _delete_entries(self, where, params=()):
        """Delete the entries matching a condition on inventory, with their captures (and
        their batch rows, which the database removes with them); returns how many"""
        self._delete_captures(f'inventory_id IN (SELECT id FROM inventory WHERE {where})', params)
        return self.conn.execute(f'DELETE FROM inventory WHERE {where}', params).rowcount

    def _move_captures(self, from_id, to_id, count=None):
        """Move the newest `count` captures (all if None) of one entry to another"""
        limit = '' if count is None else f'ORDER BY id DESC LIMIT {int(count)}'
        self.conn.execute(f'''UPDATE inventory_captures SET inventory_id = ? WHERE id IN (
            SELECT id FROM inventory_captures WHERE inventory_id = ? {limit})''', (to_id, from_id))

    def _trim_captures(self, row_id, quantity):
        """No more captures than copies: when copies are removed, the newest captures go -
        lowering a quantity is mostly taking back a card captured twice"""
        self._delete_captures('''inventory_id = ? AND id NOT IN (SELECT id FROM inventory_captures
                                 WHERE inventory_id = ? ORDER BY id LIMIT ?)''', (row_id, row_id, quantity))

    # ------------------------------------------------------------------------
    # Batches (which copies came in together): inventory_batches
    # ------------------------------------------------------------------------

    def _note_batch(self, row_id, batch, added_at, quantity):
        """`quantity` copies of an entry came with a batch"""
        self.conn.execute('''
            INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity) VALUES (?, ?, ?, ?)
            ON CONFLICT(batch, inventory_id) DO UPDATE SET quantity = quantity + excluded.quantity''',
                          (batch, row_id, added_at, int(quantity)))

    def _take_from_batches(self, row_id, count, to_id=None):
        """
        `count` copies leave an entry's batches, the newest batch first (as the newest captures
        go first) - to another entry's when to_id is given, where they stay part of the batch
        they came with. Copies beyond what the batches account for belonged to none.
        """
        for row in self.conn.execute('SELECT id, batch, added_at, quantity FROM inventory_batches '
                                     'WHERE inventory_id = ? ORDER BY added_at DESC, id DESC', (row_id,)).fetchall():
            if count <= 0:
                break
            taken = min(count, row['quantity'])
            count -= taken
            if taken == row['quantity']:
                self.conn.execute('DELETE FROM inventory_batches WHERE id = ?', (row['id'],))
            else:
                self.conn.execute('UPDATE inventory_batches SET quantity = quantity - ? WHERE id = ?', (taken, row['id']))
            if to_id is not None:
                self._note_batch(to_id, row['batch'], row['added_at'], taken)

    def _trim_batches(self, row_id, quantity):
        """An entry's batches never account for more copies than it has"""
        known = self.conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM inventory_batches WHERE inventory_id = ?',
                                  (row_id,)).fetchone()[0]
        self._take_from_batches(row_id, known - quantity)

    def batches_by_entry(self, game=None):
        """{inventory id: its inventory_batches rows, newest first}"""
        where, params = ('WHERE i.game = ?', (game,)) if game else ('', ())
        result = {}
        with self._lock:
            for row in self.conn.execute(f'''
                    SELECT b.inventory_id, b.batch, b.added_at, b.quantity FROM inventory_batches b
                    JOIN inventory i ON i.id = b.inventory_id {where} ORDER BY b.added_at DESC, b.id DESC''', params):
                result.setdefault(row['inventory_id'], []).append(row)
        return result

    def captures_by_entry(self, game=None):
        """{inventory id: [{'url', 'captured_at'}, ...] newest first}"""
        where, params = ('WHERE i.game = ?', (game,)) if game else ('', ())
        result = {}
        with self._lock:
            for row in self.conn.execute(f'''
                    SELECT c.inventory_id, c.file, c.captured_at FROM inventory_captures c
                    JOIN inventory i ON i.id = c.inventory_id {where} ORDER BY c.id DESC''', params):
                result.setdefault(row['inventory_id'], []).append(
                    {'url': f"/captures/{row['file']}", 'captured_at': row['captured_at']})
        return result

    def add_card(self, fields, game, finish, condition='Near Mint', quantity=1, capture=None, location='',
                 quiet=False, batch=None):
        """
        Add copies of a printing (fields from Game.inventory_fields) - merged with an existing
        entry for the same card, set, number, condition, finish and location. capture: the
        scanned image of the card, kept as a thumbnail with the entry. batch: from new_batch(),
        when several cards come in together (they can then be taken back out together).
        """
        quantity = max(1, int(quantity or 1))
        values = {
            'game': game, 'card_id': fields.get('card_id'), 'card_name': fields['name'],
            'set_name': fields.get('set_name') or '', 'set_code': fields.get('set_code') or None,
            'card_number': fields.get('number') or '', 'rarity': fields.get('rarity'),
            'type_line': fields.get('type_line'), 'mana_cost': fields.get('mana_cost'),
            'colors': fields.get('colors'), 'color_identity': fields.get('color_identity'),
            'price_usd': float(fields.get('price') or 0), 'quantity': quantity,
            'condition': condition or 'Near Mint', 'finish': finish, 'timestamp': now(),
            'location': (location or '').strip(), 'tags': '',
        }
        thumbnail = self._make_thumbnail(capture) if capture else None
        batch, added_at = batch or (self.new_batch()[0], values['timestamp'])
        values['added_at'] = added_at
        try:
            with self._transaction():
                self.conn.execute(UPSERT, values)
                row = self.conn.execute(
                    f"SELECT id, quantity FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                    [values[c] for c in KEY_COLUMNS]).fetchone()
                self._note_batch(row['id'], batch, added_at, quantity)
                capture_id = None
                if thumbnail:
                    capture_id = self.conn.execute(
                        'INSERT INTO inventory_captures (inventory_id, file, captured_at) VALUES (?, ?, ?)',
                        (row['id'], thumbnail, values['timestamp'])).lastrowid
        except Exception:
            if thumbnail:  # written before the edit that did not happen
                (CAPTURES_DIR / thumbnail).unlink(missing_ok=True)
            raise
        with self._lock:
            self.last_added = (row['id'], quantity, capture_id, batch)
        if not quiet:
            self.log(f"Added to inventory: {quantity}x {values['card_name']} ({finish}) - "
                     f"${values['price_usd'] * row['quantity']:.2f} for {row['quantity']}", level="success")
        return row['id']

    @staticmethod
    def new_batch():
        """(batch id, added_at) for cards that come in together: add_card(batch=...)"""
        return uuid.uuid4().hex, now()

    def set_price(self, row_id, price):
        with self._transaction():
            self.conn.execute('UPDATE inventory SET price_usd = ? WHERE id = ?', (float(price), row_id))

    def undo_last_add(self):
        """
        Take back the most recent add_card: lower that entry's quantity by the amount
        added, deleting it if nothing is left. Returns the card name, or None.
        """
        with self._transaction():
            if not self.last_added:
                return None
            row_id, quantity, capture_id, batch = self.last_added
            self.last_added = None
            row = self.conn.execute('SELECT card_name, quantity FROM inventory WHERE id = ?', (row_id,)).fetchone()
            if not row:
                return None
            if quantity >= row['quantity']:
                self._delete_entries('id = ?', (row_id,))
            else:
                self.conn.execute('UPDATE inventory SET quantity = quantity - ? WHERE id = ?', (quantity, row_id))
                self.conn.execute('UPDATE inventory_batches SET quantity = quantity - ? '
                                  'WHERE inventory_id = ? AND batch = ? AND quantity > ?', (quantity, row_id, batch, quantity))
                if not self.conn.execute('SELECT changes()').fetchone()[0]:
                    self.conn.execute('DELETE FROM inventory_batches WHERE inventory_id = ? AND batch = ?', (row_id, batch))
                self._trim_batches(row_id, row['quantity'] - quantity)
                if capture_id:
                    self._delete_captures('id = ?', (capture_id,))
        self.log(f"Undid add: {quantity}x {row['card_name']}")
        return row['card_name']

    # ------------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------------

    def get_all_cards(self, game=None):
        """Inventory rows (newest first), optionally only one game's"""
        where, params = ('WHERE game = ?', (game,)) if game else ('', ())
        with self._lock:
            rows = self.conn.execute(f'SELECT * FROM inventory {where} ORDER BY timestamp DESC, id DESC', params).fetchall()
        captures = self.captures_by_entry(game)
        batches = self.batches_by_entry(game)
        return [{**_row_dict(row, batches.get(row['id'], ())), 'captures': captures.get(row['id'], [])} for row in rows]

    def owned_by_name(self, game):
        """{search_key(card name): copies owned} over every printing, finish and location"""
        owned = {}
        with self._lock:
            for row in self.conn.execute(
                    'SELECT card_name, SUM(quantity) AS copies FROM inventory WHERE game = ? GROUP BY card_name', (game,)):
                key = search_key(row['card_name'])
                owned[key] = owned.get(key, 0) + row['copies']
        return owned

    def owned_printing_by_name(self, game):
        """{search_key(card name): id of the printing owned} - the one with most copies (the
        newest of those), to show an owned card as it is on the shelf"""
        printings = {}
        with self._lock:
            for row in self.conn.execute(
                    '''SELECT card_name, card_id, SUM(quantity) AS copies, MAX(timestamp) AS newest FROM inventory
                       WHERE game = ? AND card_id IS NOT NULL GROUP BY card_name, card_id
                       ORDER BY copies, newest''', (game,)):
                printings[search_key(row['card_name'])] = row['card_id']  # the last one wins
        return printings

    def owned_by_printing(self, game, name):
        """{printing id: copies owned} of one card, over every finish, condition and location"""
        with self._lock:
            return {row['card_id']: row['copies'] for row in self.conn.execute(
                '''SELECT card_id, SUM(quantity) AS copies FROM inventory
                   WHERE game = ? AND card_name = ? COLLATE NOCASE AND card_id IS NOT NULL GROUP BY card_id''',
                (game, name))}

    def locations(self, game):
        """Locations in use, alphabetically"""
        with self._lock:
            return [row['location'] for row in self.conn.execute(
                "SELECT DISTINCT location FROM inventory WHERE game = ? AND location != '' "
                'ORDER BY location COLLATE NOCASE', (game,))]

    def get_stats(self, game=None):
        """Totals for the top bar and the inventory window"""
        where, params = ('WHERE game = ?', (game,)) if game else ('', ())
        with self._lock:
            row = self.conn.execute(f'''
                SELECT COALESCE(SUM(quantity), 0) AS total_cards, COUNT(*) AS unique_cards,
                       COALESCE(SUM(price_usd * quantity), 0) AS total_value
                FROM inventory {where}''', params).fetchone()
            finishes = {r['finish']: r['count'] for r in self.conn.execute(
                f'SELECT finish, SUM(quantity) AS count FROM inventory {where} GROUP BY finish', params)}
        return {'total_cards': row['total_cards'], 'unique_cards': row['unique_cards'],
                'total_value': row['total_value'], 'finishes': finishes}

    def _get_row(self, row_id):
        return self.conn.execute('SELECT * FROM inventory WHERE id = ?', (row_id,)).fetchone()

    # ------------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------------

    def delete_card(self, row_id, quiet=False):
        with self._transaction():
            row = self._get_row(row_id)
            if not row:
                return False
            self._delete_entries('id = ?', (row_id,))
        if not quiet:
            self.log(f"Deleted from inventory: {row['card_name']}", level="success")
        return True

    def get_entry(self, row_id):
        with self._lock:
            row = self._get_row(row_id)
            return dict(row) if row else None

    def _add_tags(self, row_id, tags):
        """Entries merged into another one bring their tags along"""
        row = self._get_row(row_id)
        merged = ', '.join(clean_tags(clean_tags(row['tags']) + clean_tags(tags)))
        if merged != row['tags']:
            self.conn.execute('UPDATE inventory SET tags = ? WHERE id = ?', (merged, row_id))

    # What says which printing an entry is (update_card's `printing`)
    PRINTING_COLUMNS = ('card_id', 'card_name', 'set_name', 'set_code', 'card_number', 'rarity', 'type_line',
                        'mana_cost', 'colors', 'color_identity')

    def _copy_with(self, row, quantity, condition, finish, price=None, location=None, tags=None, printing=None):
        """Add `quantity` copies of an entry under another condition/finish/location/printing
        (merging), with the newest `quantity` of its captures; returns the id of the entry they
        went to"""
        values = dict(row)
        values.update(printing or {})
        values.update(quantity=quantity, condition=condition, finish=finish)
        if location is not None:
            values['location'] = location
        if tags is not None:
            values['tags'] = tags
        if price is not None:
            values['price_usd'] = price
        values.pop('id')
        self.conn.execute(UPSERT, values)
        target = self.conn.execute(
            f"SELECT id FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
            [values[c] for c in KEY_COLUMNS]).fetchone()['id']
        self._add_tags(target, values['tags'])
        self._move_captures(row['id'], target, quantity)
        self._take_from_batches(row['id'], quantity, to_id=target)
        return target

    def update_card(self, row_id, quantity=None, condition=None, finish=None, split_quantity=None,
                    finish_price=None, location=None, tags=None, quiet=False, printing=None):
        """
        Change an entry's quantity, condition, finish, location, tags or printing. Changing the
        finish, location or printing of an entry with several copies moves split_quantity of
        them (default 1) to the new one. An entry that ends up identical to another one is
        merged into it. finish_price: the printing's price in the new finish (used when the
        finish or the printing changes; None keeps the price). location: '' = none; tags: list
        or "a, b" text. printing: the PRINTING_COLUMNS of another printing of the card (the
        photos taken of the copies stay with them).

        Returns:
            dict: {'success': bool, 'split': bool, 'message': str}
        """
        with self._transaction():
            row = self._get_row(row_id)
            if not row:
                return {'success': False, 'split': False, 'message': 'Card not found'}
            new_quantity = int(quantity) if quantity is not None else row['quantity']
            new_condition = condition or row['condition']
            new_finish = finish or row['finish']
            new_location = location.strip() if location is not None else row['location']
            new_tags = ', '.join(clean_tags(tags)) if tags is not None else row['tags']
            if printing is not None:
                printing = {column: printing.get(column) for column in self.PRINTING_COLUMNS}
                if all(printing[column] == row[column] for column in ('card_name', 'set_name', 'card_number')):
                    printing = None  # the printing it already is
            new_price = finish_price if finish_price is not None and (new_finish != row['finish'] or printing) \
                else row['price_usd']
            if new_quantity < 1:
                return {'success': False, 'split': False, 'message': 'Quantity must be at least 1'}

            if (new_finish != row['finish'] or new_location != row['location'] or printing) and row['quantity'] > 1:
                moved = int(split_quantity) if split_quantity is not None else 1
                if not 1 <= moved <= row['quantity']:
                    return {'success': False, 'split': False,
                            'message': f"Split quantity must be 1-{row['quantity']}"}
                remaining = row['quantity'] - moved
                self._copy_with(row, moved, new_condition, new_finish, new_price, new_location, new_tags, printing)
                if remaining:
                    self.conn.execute('UPDATE inventory SET quantity = ? WHERE id = ?', (remaining, row_id))
                    self._trim_captures(row_id, remaining)
                    self._trim_batches(row_id, remaining)
                else:
                    self._delete_entries('id = ?', (row_id,))
                if not quiet:
                    where = ', '.join(part for part in (
                        f"{printing['set_name']} #{printing['card_number']}" if printing else '', new_finish, new_location) if part)
                    self.log(f"{row['card_name']}: {moved} moved to {where}"
                             + (f", {remaining} stay" if remaining else ''), level="success")
                return {'success': True, 'split': bool(remaining), 'message': 'Card updated and split' if remaining else 'Card updated'}

            # An entry that becomes identical to another one is merged into it
            twin = self.conn.execute(f'''
                SELECT id FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)} AND id != ?''',
                (row['game'], *((printing or row)[column] for column in ('card_name', 'set_name', 'card_number')),
                 new_condition, new_finish, new_location, row_id)).fetchone()
            if twin:
                self._trim_captures(row_id, new_quantity)
                self._move_captures(row_id, twin['id'])
                self._trim_batches(row_id, new_quantity)
                self._take_from_batches(row_id, new_quantity, to_id=twin['id'])
                self.conn.execute('UPDATE inventory SET quantity = quantity + ? WHERE id = ?', (new_quantity, twin['id']))
                self._add_tags(twin['id'], new_tags)
                self._delete_entries('id = ?', (row_id,))
            else:
                self.conn.execute('UPDATE inventory SET quantity = ?, condition = ?, finish = ?, price_usd = ?, '
                                  'location = ?, tags = ? WHERE id = ?',
                                  (new_quantity, new_condition, new_finish, new_price, new_location, new_tags, row_id))
                if printing:
                    self.conn.execute(f"UPDATE inventory SET {', '.join(c + ' = ?' for c in self.PRINTING_COLUMNS)} WHERE id = ?",
                                      (*(printing[c] for c in self.PRINTING_COLUMNS), row_id))
                self._trim_captures(row_id, new_quantity)
                self._trim_batches(row_id, new_quantity)
        if not quiet:
            self.log(f"Updated {row['card_name']}: {new_quantity}x {new_condition}, {new_finish}"
                     + (f", {new_location}" if new_location else '')
                     + (f", now {printing['set_name']} #{printing['card_number']}" if printing else ''), level="success")
        return {'success': True, 'split': False, 'message': 'Card updated'}

    BULK_ACTIONS = ('delete', 'condition', 'location', 'add_tag', 'remove_tag')

    def bulk_update(self, row_ids, action, value=None):
        """
        One change to several entries: 'delete', 'condition' (value: the condition), 'location'
        (value: the location, '' = none - whole stacks move), 'add_tag' / 'remove_tag' (value:
        the tag). Returns the number of entries changed.
        """
        if action not in self.BULK_ACTIONS:
            raise ValueError(f"Unknown action: {action}")
        value = (value or '').strip()
        if action in ('condition', 'add_tag', 'remove_tag') and not value:
            raise ValueError(f"{action} needs a value")
        changed = 0
        with self._transaction():  # every entry, or - when one fails - none
            for row_id in row_ids:
                row = self._get_row(row_id)
                if not row:  # merged into another entry earlier in this loop, or already gone
                    continue
                if action == 'delete':
                    done = self.delete_card(row_id, quiet=True)
                elif action == 'condition':
                    done = self.update_card(row_id, condition=value, quiet=True)['success']
                elif action == 'location':
                    done = self.update_card(row_id, location=value, split_quantity=row['quantity'], quiet=True)['success']
                else:
                    tags = clean_tags(row['tags'])
                    if action == 'add_tag':
                        tags = clean_tags(tags + [value])
                    else:
                        tags = [tag for tag in tags if tag.lower() != value.lower()]
                    done = self.update_card(row_id, tags=tags, quiet=True)['success']
                changed += bool(done)
        self.log(f"Inventory: {action.replace('_', ' ')}{' ' + value if value else ''} - {changed} entries", level="success")
        return changed

    def take_from(self, source, game, location=None):
        """
        Move every entry of a game from another inventory (the scanner's) into this one, with
        its captures; entries that exist here already get the copies added. With a location,
        every entry arrives there, whatever location it was scanned into. Returns
        {'entries', 'cards'} moved.
        """
        batch, added_at = self.new_batch()  # one for the whole move (the collection page can filter by it)
        with self._lock, source._lock:
            rows = source.conn.execute('SELECT * FROM inventory WHERE game = ? ORDER BY id', (game,)).fetchall()
            if not rows:
                return {'entries': 0, 'cards': 0}
            # Two files, two commits: the move is noted in the source first, so a crash between
            # them is finished on the next start instead of leaving the cards in both
            # (finish_interrupted_moves)
            move_id = uuid.uuid4().hex
            with source._transaction():
                source.conn.execute(PENDING_MOVES_TABLE)
                source.conn.execute('INSERT OR REPLACE INTO pending_moves (game, move_id, added_at) VALUES (?, ?, ?)',
                                    (game, move_id, added_at))
            try:
                with self._transaction():
                    self.conn.execute(ARRIVED_MOVES_TABLE)
                    self.conn.execute('INSERT INTO arrived_moves (move_id) VALUES (?)', (move_id,))
                    for row in rows:
                        values = dict(row)
                        values.pop('id')
                        values['added_at'] = added_at
                        if location:
                            values['location'] = location
                        self.conn.execute(UPSERT, values)
                        target = self.conn.execute(
                            f"SELECT id FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                            [values[c] for c in KEY_COLUMNS]).fetchone()['id']
                        self._add_tags(target, values['tags'])
                        self._note_batch(target, batch, added_at, row['quantity'])
                        for capture in source.conn.execute(
                                'SELECT file, captured_at FROM inventory_captures WHERE inventory_id = ? ORDER BY id', (row['id'],)):
                            self.conn.execute('INSERT INTO inventory_captures (inventory_id, file, captured_at) VALUES (?, ?, ?)',
                                              (target, capture['file'], capture['captured_at']))
            except Exception:
                # Nothing arrived here: the cards stay where they were
                with source._transaction():
                    source.conn.execute('DELETE FROM pending_moves WHERE game = ?', (game,))
                raise
            self._remove_moved(source, game)
            with self._transaction():
                self.conn.execute('DELETE FROM arrived_moves WHERE move_id = ?', (move_id,))
        moved = {'entries': len(rows), 'cards': sum(row['quantity'] for row in rows)}
        self.log(f"Added to the collection: {moved['cards']} cards ({moved['entries']} entries)", level="success")
        return moved

    @staticmethod
    def _remove_moved(source, game):
        """Second half of take_from: the cards are in the collection, so they go from the
        source - with the note of the move, in one commit. The capture rows go without their
        files, which moved"""
        with source._transaction():
            source.conn.execute('DELETE FROM inventory_captures WHERE inventory_id IN '
                                '(SELECT id FROM inventory WHERE game = ?)', (game,))
            source.conn.execute('DELETE FROM inventory WHERE game = ?', (game,))
            source.conn.execute('DELETE FROM pending_moves WHERE game = ?', (game,))
            source.last_added = None

    def finish_interrupted_moves(self, source):
        """
        On startup: a take_from the app did not get through (crash, power cut). If its cards
        arrived here (the move's id is in arrived_moves - committed together with them) they
        are removed from the source; otherwise nothing was moved and only the note goes.
        Returns the games whose move was finished.
        """
        finished = []
        with self._lock, source._lock:
            with source._transaction():
                source.conn.execute(PENDING_MOVES_TABLE)
            with self._transaction():
                self.conn.execute(ARRIVED_MOVES_TABLE)
            for move in source.conn.execute('SELECT game, move_id, added_at FROM pending_moves').fetchall():
                arrived = self.conn.execute('SELECT 1 FROM arrived_moves WHERE move_id = ?', (move['move_id'],)).fetchone()
                if arrived:
                    self._remove_moved(source, move['game'])
                    finished.append(move['game'])
                    self.log(f"Finished the interrupted \"Add to collection\" of {move['added_at']}: "
                             "the cards were in the collection already, removed from the scanned cards", level="warning")
                else:
                    with source._transaction():
                        source.conn.execute('DELETE FROM pending_moves WHERE game = ?', (move['game'],))
                    self.log(f"An \"Add to collection\" of {move['added_at']} was interrupted before any card "
                             "was moved: the scanned cards are still waiting", level="warning")
            with self._transaction():
                self.conn.execute('DELETE FROM arrived_moves')  # nothing is under way any more
        return finished

    def forget_moves(self):
        """Inside an edit that replaces the inventory (a restore): notes of a move under way
        describe cards that are no longer there"""
        for table in ('pending_moves', 'arrived_moves'):
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                self.conn.execute(f'DELETE FROM {table}')

    def rebuild_batches(self):
        """Inside an edit that put entries in place without their batches (a backup made
        before inventory_batches): the one batch each entry itself knows of"""
        self.conn.execute('DELETE FROM inventory_batches')
        self.conn.execute(BACKFILL_BATCHES)

    def remove_batch(self, game, batch):
        """
        Take back what came into the inventory together (an "Add to collection", an import):
        each entry loses the copies that came with that batch, with their captures (the
        newest); entries left with no copies are deleted. Returns {'entries', 'cards'} removed.
        """
        with self._transaction():
            rows = self.conn.execute('''
                SELECT b.id AS batch_row, b.quantity AS added, b.added_at, i.id, i.quantity
                FROM inventory_batches b JOIN inventory i ON i.id = b.inventory_id
                WHERE i.game = ? AND b.batch = ?''', (game, batch)).fetchall()
            for row in rows:
                if row['added'] >= row['quantity']:
                    self._delete_entries('id = ?', (row['id'],))
                else:
                    left = row['quantity'] - row['added']
                    self.conn.execute('UPDATE inventory SET quantity = ? WHERE id = ?', (left, row['id']))
                    self.conn.execute('DELETE FROM inventory_batches WHERE id = ?', (row['batch_row'],))
                    self._trim_captures(row['id'], left)
                    self._trim_batches(row['id'], left)
            self.last_added = None
        removed = {'entries': len(rows), 'cards': sum(row['added'] for row in rows)}
        if rows:
            self.log(f"Removed the cards added {rows[0]['added_at']}: {removed['cards']} cards "
                     f"({removed['entries']} entries)", level="success")
        return removed

    def clear_inventory(self, game=None):
        """Delete every entry (of one game, if given)"""
        try:
            with self._transaction():
                deleted = self._delete_entries(*(('game = ?', (game,)) if game else ('1 = 1',)))
                self.last_added = None
            self.log(f"Inventory cleared: {deleted} entries removed", level="success")
            return {'success': True, 'deleted': deleted}
        except Exception as e:
            self.log(f"Failed to clear inventory: {e}", level="error")
            return {'success': False, 'error': str(e), 'deleted': 0}

    # ------------------------------------------------------------------------
    # Export / import
    # ------------------------------------------------------------------------

    def export(self, game, writer, prefix):
        """Write one game's inventory with an export writer (Game.export_formats); returns the path"""
        path = Config.DATA_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(path, 'w', newline='') as f:
            writer(self.get_all_cards(game), f)
        self.log(f"Inventory exported to: {path}", level="success")
        return path

    def _import_rows(self, rows, game, replace_existing, stats):
        """
        Put rows that were read and checked (dicts for UPSERT) into one game's inventory, or
        replace it with them - one edit and one batch: all of it, or (when it fails) nothing,
        and nothing is deleted before every row was read.
        """
        batch, added_at = self.new_batch()
        with self._transaction():
            if replace_existing:
                self._delete_entries('game = ?', (game,))
                self.last_added = None
            for values in rows:
                values['added_at'] = added_at
                key = [values[c] for c in KEY_COLUMNS]
                where = ' AND '.join(c + ' = ?' for c in KEY_COLUMNS)
                exists = self.conn.execute(f"SELECT 1 FROM inventory WHERE {where}", key).fetchone()
                self.conn.execute(UPSERT, values)
                row_id = self.conn.execute(f"SELECT id FROM inventory WHERE {where}", key).fetchone()['id']
                self._note_batch(row_id, batch, added_at, values['quantity'])
                stats['updated' if exists else 'added'] += 1
        return stats

    def import_entries(self, entries, game, replace_existing=False):
        """
        Add the cards of another app's collection file (entries from Game.import_rows: matched
        to their printings) to one game's inventory, or replace it with them.

        Returns:
            dict: success, added (new entries), updated (copies added to an existing entry)
        """
        stats = {'success': True, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
        timestamp = now()
        rows = []
        for entry in entries:
            fields = entry['fields']
            rows.append({
                'game': game, 'card_id': fields.get('card_id'), 'card_name': fields['name'],
                'set_name': fields.get('set_name') or '', 'set_code': fields.get('set_code') or None,
                'card_number': fields.get('number') or '', 'rarity': fields.get('rarity'),
                'type_line': fields.get('type_line'), 'mana_cost': fields.get('mana_cost'),
                'colors': fields.get('colors'), 'color_identity': fields.get('color_identity'),
                'price_usd': float(fields.get('price') or 0), 'quantity': max(1, int(entry['quantity'])),
                'condition': entry.get('condition') or 'Near Mint', 'finish': entry['finish'],
                'timestamp': timestamp, 'location': '',
                'tags': ', '.join(clean_tags(entry.get('tags') or '')),
            })
        if replace_existing and not rows:
            return {**stats, 'success': False, 'error': 'The file holds no card - nothing was replaced'}
        self._import_rows(rows, game, replace_existing, stats)
        self.log(f"Import complete: {stats['added']} added, {stats['updated']} updated", level="success")
        return stats

    def import_csv(self, csv_file_path, game, finishes, replace_existing=False):
        """
        Import a CSV in the app's own columns (games.base.write_collection_csv - with Card ID,
        Set Code and Timestamp an entry comes back as it was - and the CSVs written before
        that, which lack them: Card Name, Set, Card Number, ..., Quantity,
        Condition, Finish or the older Foil / Surge columns, and Location / Tags when present)
        into one game's inventory. Column names are matched whatever their case.

        The whole file is read before anything changes. Rows that cannot be used are skipped
        when adding; a file that is to replace the inventory must be usable row for row, or
        nothing is replaced.

        Args:
            finishes: the game's finish keys - the first is used when a row has none

        Returns:
            dict: success, added, updated, skipped, errors (and error when not successful)
        """
        csv_file_path = Path(csv_file_path)
        stats = {'success': True, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
        if not csv_file_path.exists():
            return {**stats, 'success': False, 'error': 'File not found'}

        rows = []
        with open(csv_file_path, newline='') as f:
            for row_number, raw in enumerate(csv.DictReader(f), start=2):
                row = {(key or '').strip().lower(): (value or '').strip()
                       for key, value in raw.items() if isinstance(value, str) or value is None}
                name, set_name = row.get('card name', ''), row.get('set', '')
                if not name or not set_name:
                    self.log(f"Row {row_number}: skipped - missing name or set", level="warning")
                    stats['skipped'] += 1
                    continue
                finish = row.get('finish', '').lower()
                if not finish:
                    yes = lambda column: row.get(column, '').lower() in ('yes', 'true', '1')
                    finish = 'surge' if yes('surge') else 'foil' if yes('foil') else finishes[0]
                if finish not in finishes:
                    self.log(f"Row {row_number}: unknown finish '{finish}'", level="warning")
                    stats['errors'] += 1
                    continue
                try:
                    quantity = int(row.get('quantity') or 1)
                    if quantity < 1:
                        raise ValueError
                except ValueError:
                    self.log(f"Row {row_number}: the quantity '{row.get('quantity')}' is not a number of cards", level="warning")
                    stats['errors'] += 1
                    continue
                try:
                    price = float((row.get('price (usd)') or '0').replace('$', '').replace(',', ''))
                except ValueError:
                    price = 0.0
                timestamp = row.get('timestamp', '')
                rows.append({
                    'game': game, 'card_id': row.get('card id') or None,
                    'card_name': name, 'set_name': set_name,
                    'set_code': row.get('set code') or None, 'card_number': row.get('card number', ''),
                    'rarity': row.get('rarity', ''), 'type_line': row.get('type', ''),
                    'mana_cost': row.get('mana cost', ''), 'colors': row.get('colors', ''),
                    'color_identity': row.get('color identity', ''), 'price_usd': price,
                    'quantity': quantity, 'condition': row.get('condition') or 'Near Mint',
                    'finish': finish,
                    'timestamp': timestamp if TIMESTAMP.match(timestamp) else now(),
                    'location': row.get('location', ''),
                    'tags': ', '.join(clean_tags(row.get('tags', ''))),
                })

        unusable = stats['skipped'] + stats['errors']
        if replace_existing and (unusable or not rows):
            reason = (f"{unusable} of its {unusable + len(rows)} rows cannot be used (see the activity log)"
                      if rows else "it holds no card that can be used")
            self.log(f"Import refused: {reason} - nothing was replaced", level="error")
            return {**stats, 'success': False, 'error': f"Nothing was replaced: {reason}"}
        self._import_rows(rows, game, replace_existing, stats)

        self.log(f"Import complete: {stats['added']} added, {stats['updated']} updated, "
                 f"{stats['skipped']} skipped, {stats['errors']} errors", level="success")
        return stats

    def close(self):
        if self.conn:
            self.conn.close()
