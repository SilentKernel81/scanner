# ============================================================================
# FILE: inventory.py
# An inventory in a SQLite file: entries (one per printing + condition + finish +
# location), the captures behind them, and which station scanned how many copies.
# The server has two: the collection (in the card database file) and the scanned
# cards (scan_inventory.db), which move into the collection with take_from.
# ============================================================================
import csv
import os
import functools
import re
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from card_scanner.core import storage
from card_scanner.config import Config
from card_scanner.core.database import search_key
from card_scanner.core.pending import APPLIED_CAPTURES_TABLE, prune_applied

logger = logging.getLogger('database')

# Small copies of the captures behind inventory entries, shown on the lists and when hovering
# an entry. Kept apart from scanned_cards/ (cleaned after cleanup.days) until their entry is
# deleted.
CAPTURES_DIR = Config.DATA_DIR / 'captures'
CAPTURE_HEIGHT = 400  # px - ~25 KB per card
# A large copy beside each of them (same file name, other folder): the only full picture of a
# card that outlives the clean-up - 400 px is too coarse to train a reader or detector on later.
# The pages never load these (a list row would 6x its size); they are in the data folder, hence
# in scripts/backup.sh's archive, but not in the collection backups (data/backups/).
FULL_CAPTURES_DIR = Config.DATA_DIR / 'captures_full'
# The sizes a person can choose (Settings on the scanner page, `capture_copy_size` in
# data/settings.json): the height in px a large copy is shrunk to. A computer station's capture
# is ~1150 px tall, a phone's more: "large" keeps them about as they came. Measured on 40
# captures at JPEG 85: ~56 KB, ~100 KB and ~145 KB each. "off": no large copy, thumbnails only
FULL_CAPTURE_SIZES = {'off': 0, 'small': 600, 'medium': 900, 'large': 1200}
FULL_CAPTURE_DEFAULT = 'large'
FULL_CAPTURE_HEIGHT = FULL_CAPTURE_SIZES[FULL_CAPTURE_DEFAULT]  # px; 0: no large copy (set_full_capture_size)
FULL_CAPTURE_QUALITY = 85  # JPEG quality


def set_full_capture_size(name):
    """Choose how large the large copies of new captures are (a key of FULL_CAPTURE_SIZES; an unknown one is the default)"""
    global FULL_CAPTURE_HEIGHT
    if name not in FULL_CAPTURE_SIZES:
        name = FULL_CAPTURE_DEFAULT
    FULL_CAPTURE_HEIGHT = FULL_CAPTURE_SIZES[name]
    return name


def full_capture_usage():
    """(number of large copies, bytes they take) - a look at the folder, for the settings page"""
    count = size = 0
    try:
        with os.scandir(FULL_CAPTURES_DIR) as entries:
            for entry in entries:
                if entry.is_file():
                    count += 1
                    size += entry.stat().st_size
    except OSError:
        pass  # no folder yet: nothing kept
    return count, size

# One row per card + set + number + condition + finish + location, per game. Rows are
# addressed by id. tags: "trade, keep" - not part of the key (see clean_tags). timestamp: when
# the card was scanned; added_at: when copies last came into this inventory. Which copies came
# with which "Add to collection" (an import, a scan) is kept in inventory_batches - added_quantity
# held that for the latest one only, until that table (it is still written, not read)
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
        quantity INTEGER NOT NULL DEFAULT 1 CHECK (typeof(quantity) = 'integer' AND quantity > 0),
        condition TEXT NOT NULL DEFAULT 'Near Mint',
        finish TEXT NOT NULL DEFAULT 'regular',
        timestamp TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '',
        added_at TEXT,
        added_quantity INTEGER CHECK (added_quantity IS NULL OR (typeof(added_quantity) = 'integer' AND added_quantity >= 0)),
        UNIQUE(game, card_name, set_name, card_number, condition, finish, location)
    )
'''
# Beside the entries, and gone with them (ON DELETE CASCADE - on connections from
# storage.connect, which enforce it): one row per captured copy with its thumbnail file, and how
# many of an entry's copies each station added. An entry with no copies left is deleted, never
# kept at 0: lower a quantity to nothing by deleting the row (_delete_entries), not by UPDATE.
CAPTURES_TABLE = '''
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_id INTEGER NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
        file TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        station TEXT,
        capture_key TEXT    -- the uid of the capture this copy came from (pending.py): see captured_copy
    )
'''
# A capture whose card was added without a photo row (the thumbnail could not be made): there
# is then no row above that says where its copy is, so nothing can say whether it is still in
# the scanned cards. Noted here in the commit that adds the card, so that a phone asking about
# it is told "cannot be followed" instead of "gone" (captured_copy). Not beside an entry and
# in no backup: it only ever makes an answer more careful.
UNFOLLOWED_CAPTURES_TABLE = ('CREATE TABLE IF NOT EXISTS unfollowed_captures '
                             '(key TEXT PRIMARY KEY, station TEXT, noted_at TEXT NOT NULL)')
SOURCES_TABLE = '''
    CREATE TABLE {table} (
        inventory_id INTEGER NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
        station TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
        PRIMARY KEY (inventory_id, station)
    )
'''
# Which copies of an entry came together: one row per batch (an "Add to collection", an import, a
# scanned card) and entry. `batch` is unique for each time cards came in - two in the same
# second are still two - and added_at is when. An entry's rows never add up to more than its
# quantity; copies no row accounts for were there before the oldest batch known ("old:..."
# batches are what the single added_at / added_quantity of an entry said before this table).
BATCHES_TABLE = '''
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch TEXT NOT NULL,
        inventory_id INTEGER NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
        added_at TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
        UNIQUE(batch, inventory_id)
    )
'''
BATCHES_INDEX = 'CREATE INDEX IF NOT EXISTS idx_batches_entry ON inventory_batches(inventory_id)'
BATCHES_INDEXES = (BATCHES_INDEX,)
# The standalone program (until 2026-10-14) kept the rules above as triggers on tables without
# CHECK or REFERENCES. A file it made has them: they are dropped in the migration that gives
# the tables the rules themselves - one that names `inventory` makes the rename of the new
# table fail, which left the file unchanged and the server unable to start on it
LEGACY_TRIGGERS = tuple(f'DROP TRIGGER IF EXISTS {name}' for name in (
    'inventory_quantity_insert', 'inventory_quantity_update',
    'inventory_captures_entry_insert', 'inventory_captures_entry_update', 'inventory_takes_inventory_captures',
    'inventory_batches_entry_insert', 'inventory_batches_entry_update',
    'inventory_batches_quantity_insert', 'inventory_batches_quantity_update', 'inventory_takes_inventory_batches'))
BATCHES_MIGRATION = 'inventory_batches_1'
BACKFILL_BATCHES = '''
    INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity)
    SELECT 'old:' || COALESCE(added_at, timestamp), id, COALESCE(added_at, timestamp),
           MIN(COALESCE(CAST(added_quantity AS INTEGER), CAST(quantity AS INTEGER)), CAST(quantity AS INTEGER))
    FROM inventory
    WHERE MIN(COALESCE(CAST(added_quantity AS INTEGER), CAST(quantity AS INTEGER)), CAST(quantity AS INTEGER)) > 0
'''
# Trades (trades.py; used in the collection only): cards set aside for one stay in the
# collection until it is confirmed. An open trade's rows name the entry and how many of its
# copies are set aside; a confirmed one ('done') keeps the cards as they were (card: the row
# as JSON) and no entry. The triggers keep what an open trade holds: such an entry is not
# deleted and not lowered below that, whatever part of the program tries - the copies are
# taken out of the trade first. (Made again at every start: a rebuilt table loses its triggers.)
TRADES_TABLE = '''
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
        created_at TEXT NOT NULL,
        closed_at TEXT
    )
'''
TRADE_CARDS_TABLE = '''
    CREATE TABLE IF NOT EXISTS trade_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
        inventory_id INTEGER REFERENCES inventory(id),
        quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
        card TEXT,
        CHECK (inventory_id IS NOT NULL OR card IS NOT NULL),
        UNIQUE (trade_id, inventory_id)
    )
'''
TRADE_HELD = 'These copies are set aside for a trade: take them out of the trade first'
TRADE_TRIGGERS = (
    f'''CREATE TRIGGER IF NOT EXISTS trade_keeps_entry BEFORE DELETE ON inventory
        WHEN EXISTS (SELECT 1 FROM trade_cards WHERE inventory_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, '{TRADE_HELD}'); END''',
    f'''CREATE TRIGGER IF NOT EXISTS trade_keeps_copies BEFORE UPDATE OF quantity ON inventory
        WHEN NEW.quantity < OLD.quantity
             AND NEW.quantity < (SELECT COALESCE(SUM(quantity), 0) FROM trade_cards WHERE inventory_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, '{TRADE_HELD}'); END''',
)
INVENTORY_INDEXES = ('CREATE INDEX IF NOT EXISTS idx_inventory_game_name ON inventory(game, card_name COLLATE NOCASE)',
                     'CREATE INDEX IF NOT EXISTS idx_inventory_timestamp ON inventory(timestamp DESC)')
# One row per capture: the database refuses a second copy under the same capture, whatever
# the code that asks first does. (In this list, so a rebuilt table gets the index again.)
CAPTURES_INDEXES = ('CREATE INDEX IF NOT EXISTS idx_captures_entry ON inventory_captures(inventory_id)',
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_captures_key ON inventory_captures(capture_key) '
                    'WHERE capture_key IS NOT NULL')
CONSTRAINTS_MIGRATION = 'inventory_constraints_1'
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
                       '(game TEXT PRIMARY KEY, move_id TEXT NOT NULL, added_at TEXT NOT NULL, station TEXT)')
ARRIVED_MOVES_TABLE = 'CREATE TABLE IF NOT EXISTS arrived_moves (move_id TEXT PRIMARY KEY)'


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
    batches = [{'batch': part['batch'], 'added_at': part['added_at'], 'quantity': part['quantity']}
               for part in batches]
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


class _Columns(dict):
    """A CSV row whose columns are found whatever the case and the spaces around their names"""

    def __init__(self, row):
        super().__init__({(key or '').strip().lower(): value for key, value in row.items() if isinstance(value, str)})

    def get(self, column, default=None):
        return super().get(column.strip().lower(), default)


def _writes(method):
    """
    A method that changes the inventory: one transaction (Transactional) - all of it, or when it
    fails none of it, whatever other writing methods it calls on the way.
    """
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._transaction():
            return method(self, *args, **kwargs)
    return wrapped


class InventoryManager(storage.Transactional):
    """
    Card inventory (table `inventory` in the card database file), for every game. A method that
    changes it is `@_writes`: one transaction, committed by the outermost one - where one calls
    another, the whole is one edit.
    """

    def __init__(self, db_file=None, log_callback=None):
        self.db_file = db_file or Config.DATABASE_FILE
        self.log_callback = log_callback
        self._lock = threading.RLock()
        # Most recent add_card of each source, for undo: {source: (row id, quantity, capture id)}.
        # source None is the scanner page; a station (stations.py) is its id
        self.last_added = {}
        self._doomed = []  # thumbnails of captures deleted in the open transaction (_commit)
        self.conn = storage.connect(self.db_file)
        self._initialize_table()
        self._add_constraints()
        with self._lock:
            for trigger in TRADE_TRIGGERS:
                self.conn.execute(trigger)
            self._commit()

    def _commit(self):
        """
        Commit - unless inside an edit (`_writes`, `_transaction`), whose outermost block does
        it: a nested method that commits would make the first half of an edit permanent. The
        `_commit()` calls in the methods are for the code that runs outside one (start-up,
        take_from's two files).
        """
        if self._transaction_depth:
            return
        self.conn.commit()
        self._committed()

    def _committed(self):
        """Only after a commit the thumbnails of the captures that went with it are deleted:
        rolled back, their rows are there again and still need the files"""
        doomed, self._doomed = self._doomed, []
        for file in doomed:
            (CAPTURES_DIR / file).unlink(missing_ok=True)
            (FULL_CAPTURES_DIR / file).unlink(missing_ok=True)

    def _rollback(self):
        self.conn.rollback()
        self._rolled_back()

    def _rolled_back(self):
        self._doomed = []

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
            new_file = not columns
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
            for index in INVENTORY_INDEXES:
                self.conn.execute(index)
            # One row per captured copy (its entry, its thumbnail file, the station it came
            # from), and how many of an entry's copies each station added (the scanned cards'
            # camera filter): entries merge across cameras, so this is kept beside them
            tables = {row['name'] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if 'inventory_captures' not in tables:
                self.conn.execute(CAPTURES_TABLE.format(table='inventory_captures'))
            elif 'station' not in {row['name'] for row in self.conn.execute('PRAGMA table_info(inventory_captures)')}:
                self.conn.execute('ALTER TABLE inventory_captures ADD COLUMN station TEXT')
            # Which capture each photographed copy came from (2026-10-12; captured_copy). A new
            # column that may be empty: added in place, no rebuild - the copies that are there
            # already have none and cannot be followed (the phone is told so)
            if 'capture_key' not in {row['name'] for row in self.conn.execute('PRAGMA table_info(inventory_captures)')}:
                self.conn.execute('ALTER TABLE inventory_captures ADD COLUMN capture_key TEXT')
            self.conn.execute(UNFOLLOWED_CAPTURES_TABLE)
            for index in CAPTURES_INDEXES:
                self.conn.execute(index)
            if 'inventory_sources' not in tables:
                self.conn.execute(SOURCES_TABLE.format(table='inventory_sources'))
            if new_file:
                # A new file is made with the constraints: nothing to migrate
                storage.record(self.conn, CONSTRAINTS_MIGRATION, 'new file')
            # Batches (2026-10-14): a table beside the entries, filled once with the one batch
            # each entry knew of (added_at / added_quantity). Nothing existing is changed, so
            # no backup or rebuild - the table is new and everything else stays as it is
            if 'inventory_batches' not in tables:
                self.conn.execute(BATCHES_TABLE.format(table='inventory_batches'))
            self.conn.execute(BATCHES_INDEX)
            if not storage.applied(self.conn, BATCHES_MIGRATION):
                # Not filled again where the table has rows already: the standalone program had
                # it (made by the same rule) before the two were one code base
                if not new_file and not self.conn.execute('SELECT 1 FROM inventory_batches LIMIT 1').fetchone():
                    self.conn.execute(BACKFILL_BATCHES)
                storage.record(self.conn, BATCHES_MIGRATION, 'new file' if new_file else 'from added_at / added_quantity')
            self.conn.execute(TRADES_TABLE)
            self.conn.execute(TRADE_CARDS_TABLE)
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_trade_cards_entry ON trade_cards(inventory_id)')
            self.conn.execute(PENDING_MOVES_TABLE)
            if 'station' not in {row['name'] for row in self.conn.execute('PRAGMA table_info(pending_moves)')}:
                self.conn.execute('ALTER TABLE pending_moves ADD COLUMN station TEXT')
            # Captures whose card is in here already (add_card's capture_key; pending.py)
            self.conn.execute(APPLIED_CAPTURES_TABLE)
            prune_applied(self.conn)
            # Left by versions that cleared these while reading (and did not always get to it)
            self.conn.execute('DELETE FROM inventory_sources WHERE inventory_id NOT IN (SELECT id FROM inventory)')
            # Entries whose stations claim more copies between them than there are (quantities
            # lowered before _trim_sources): the same rule as for a quantity lowered now -
            # nothing is given to a station that did not have it
            for row in self.conn.execute('''
                    SELECT i.id, i.card_name, i.quantity, SUM(s.quantity) AS claimed FROM inventory i
                    JOIN inventory_sources s ON s.inventory_id = i.id GROUP BY i.id HAVING claimed > i.quantity''').fetchall():
                self._trim_sources(row['id'], row['quantity'])
                logger.warning(f"{row['card_name']} (entry {row['id']}): its cameras claimed {row['claimed']} of "
                               f"{row['quantity']} copies - lowered to {row['quantity']}")
            self._commit()

    def _add_constraints(self):
        """
        Files from before the constraints (see CAPTURES_TABLE): the three tables are rebuilt
        with them, once (storage.rebuild: backup, one transaction, checked). Left out are only
        rows that mean nothing - entries with no copies, captures and station rows of entries
        that are gone or of no copies - and the totals of everything else must be unchanged.
        """
        with self._lock:
            self._commit()
            valid = 'inventory_id IN (SELECT id FROM inventory WHERE CAST(quantity AS INTEGER) > 0)'

            def facts(conn):
                return (tuple(conn.execute('SELECT COUNT(*), COALESCE(SUM(CAST(quantity AS INTEGER)), 0), COALESCE(MAX(id), 0) '
                                           'FROM inventory WHERE CAST(quantity AS INTEGER) > 0').fetchone()),
                        tuple(conn.execute(f'SELECT COUNT(*), COALESCE(MAX(id), 0) FROM inventory_captures WHERE {valid}').fetchone()),
                        tuple(conn.execute('SELECT COUNT(*), COALESCE(SUM(CAST(quantity AS INTEGER)), 0) FROM inventory_sources '
                                           f'WHERE {valid} AND CAST(quantity AS INTEGER) > 0').fetchone()),
                        tuple(conn.execute('SELECT COUNT(*), COALESCE(SUM(CAST(quantity AS INTEGER)), 0) FROM inventory_batches '
                                           f'WHERE {valid} AND CAST(quantity AS INTEGER) > 0').fetchone()))
            columns = [row['name'] for row in self.conn.execute('PRAGMA table_info(inventory)')]
            entry = ', '.join('CAST(quantity AS INTEGER)' if column == 'quantity'
                              else 'CASE WHEN added_quantity IS NULL THEN NULL ELSE MAX(0, CAST(added_quantity AS INTEGER)) END'
                              if column == 'added_quantity' else column for column in columns)
            storage.rebuild(self.db_file, CONSTRAINTS_MIGRATION, [
                ('inventory', INVENTORY_TABLE, f'SELECT {entry} FROM inventory WHERE CAST(quantity AS INTEGER) > 0',
                 INVENTORY_INDEXES),
                ('inventory_captures', CAPTURES_TABLE,
                 f'SELECT id, inventory_id, file, captured_at, station, capture_key FROM inventory_captures WHERE {valid}',
                 CAPTURES_INDEXES),
                ('inventory_sources', SOURCES_TABLE,
                 'SELECT inventory_id, station, CAST(quantity AS INTEGER) FROM inventory_sources '
                 f'WHERE {valid} AND CAST(quantity AS INTEGER) > 0', ()),
                # Made by the standalone program without the rules (this program makes it with them)
                ('inventory_batches', BATCHES_TABLE,
                 'SELECT id, batch, inventory_id, added_at, CAST(quantity AS INTEGER) FROM inventory_batches '
                 f'WHERE {valid} AND CAST(quantity AS INTEGER) > 0', BATCHES_INDEXES),
            ], facts, prepare=LEGACY_TRIGGERS)

    def _backup_table(self, label):
        """Copy the inventory table to data/backups/ before rebuilding it; returns the file and
        (entries, cards) to compare afterwards"""
        backup_dir = Config.DATA_DIR / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"inventory_before_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        self.conn.execute('ATTACH DATABASE ? AS backup', (str(backup_file),))
        self.conn.execute('CREATE TABLE backup.inventory AS SELECT * FROM main.inventory')
        self._commit()
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
                 f"backup: data/backups/{backup_file.name})", level="success")

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
                 f"backup: data/backups/{backup_file.name})", level="success")

    # ------------------------------------------------------------------------
    # Adding and undo
    # ------------------------------------------------------------------------

    # ------------------------------------------------------------------------
    # Captures (thumbnails of the copies behind an entry)
    # ------------------------------------------------------------------------

    @staticmethod
    def _make_thumbnail(image_path):
        """JPEG copies of a capture: the small one in CAPTURES_DIR and a large one in
        FULL_CAPTURES_DIR under the same name; that name, or None (no small copy, no capture)"""
        try:
            import cv2
            image = cv2.imread(str(image_path))
            if image is None:
                return None
            name = f"{uuid.uuid4().hex}.jpg"
            # The large copy first and without failing the capture: it is the extra one
            # (none at all when the person chose "off")
            if FULL_CAPTURE_HEIGHT > 0:
                try:
                    FULL_CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
                    large = image
                    if large.shape[0] > FULL_CAPTURE_HEIGHT:
                        large = cv2.resize(large, (round(large.shape[1] * FULL_CAPTURE_HEIGHT / large.shape[0]),
                                                   FULL_CAPTURE_HEIGHT), interpolation=cv2.INTER_AREA)
                    cv2.imwrite(str(FULL_CAPTURES_DIR / name), large, [cv2.IMWRITE_JPEG_QUALITY, FULL_CAPTURE_QUALITY])
                except Exception as e:
                    logger.warning(f"Could not keep the large copy of {image_path}: {e}")
            height, width = image.shape[:2]
            if height > CAPTURE_HEIGHT:
                image = cv2.resize(image, (round(width * CAPTURE_HEIGHT / height), CAPTURE_HEIGHT),
                                   interpolation=cv2.INTER_AREA)
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(CAPTURES_DIR / name), image, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return name
        except Exception as e:
            logger.warning(f"Could not keep the capture {image_path}: {e}")
            return None

    def _delete_captures(self, where, params=()):
        """Delete capture rows (and their files) matching a condition on inventory_captures"""
        rows = self.conn.execute(f'SELECT id, file FROM inventory_captures WHERE {where}', params).fetchall()
        for row in rows:
            self.conn.execute('DELETE FROM inventory_captures WHERE id = ?', (row['id'],))
            self._doomed.append(row['file'])  # the file goes when this is committed

    def _delete_entries(self, where, params=()):
        """
        Delete the entries matching a condition on `inventory`, with what stands beside them:
        their captures (thumbnails included - the foreign key would take the rows along, but
        not the files) and which station scanned them. Returns how many entries went.
        """
        ids = f'inventory_id IN (SELECT id FROM inventory WHERE {where})'
        self._delete_captures(ids, params)
        self.conn.execute(f'DELETE FROM inventory_sources WHERE {ids}', params)
        return self.conn.execute(f'DELETE FROM inventory WHERE {where}', params).rowcount

    def _take_copies(self, row_id, copies, batch=None):
        """An entry loses copies: deleted when none are left - a row is never kept at 0. They
        leave its batches newest first (`batch`, when given, before the rest) - the copies
        that no batch accounts for are the oldest, so they are the last to go"""
        row = self._get_row(row_id)
        if not row:
            return
        if copies >= row['quantity']:
            self._delete_entries('id = ?', (row_id,))
        else:
            self.conn.execute('UPDATE inventory SET quantity = quantity - ? WHERE id = ?', (copies, row_id))
            if batch:
                copies -= self._take_from_batch(row_id, batch, copies)
            self._take_from_batches(row_id, copies)

    def _set_source(self, row_id, station, quantity):
        """How many of an entry's copies a station added - no row for none"""
        if quantity > 0:
            self.conn.execute('''INSERT INTO inventory_sources (inventory_id, station, quantity) VALUES (?, ?, ?)
                                 ON CONFLICT(inventory_id, station) DO UPDATE SET quantity = excluded.quantity''',
                              (row_id, station, quantity))
        else:
            self.conn.execute('DELETE FROM inventory_sources WHERE inventory_id = ? AND station = ?', (row_id, station))

    def _station_rows(self, game, station):
        """
        [(inventory row, copies)] of a game's entries a station added copies of: as many as it
        added, at most what the entry still has (copies removed since are nobody's). Only reads:
        a DELETE of leftovers here once left every filtered list holding a write transaction,
        and another connection's write failed with "database is locked".
        """
        return [(row, min(row['station_copies'], row['quantity'])) for row in self.conn.execute(
            '''SELECT i.*, s.quantity AS station_copies FROM inventory i
               JOIN inventory_sources s ON s.inventory_id = i.id
               WHERE i.game = ? AND s.station = ? AND s.quantity > 0 AND i.quantity > 0
               ORDER BY i.timestamp DESC, i.id DESC''', (game, station)).fetchall()]

    def stations_by_entry(self, game):
        """{inventory id: {station id: copies it added}} of a game's entries"""
        result = {}
        with self._lock:
            for row in self.conn.execute(
                    '''SELECT s.inventory_id, s.station, MIN(s.quantity, i.quantity) AS copies FROM inventory_sources s
                       JOIN inventory i ON i.id = s.inventory_id WHERE i.game = ? AND s.quantity > 0''', (game,)):
                result.setdefault(row['inventory_id'], {})[row['station']] = row['copies']
        return result

    # ------------------------------------------------------------------------
    # Batches (which copies came in together): inventory_batches
    # ------------------------------------------------------------------------

    @staticmethod
    def new_batch():
        """(batch id, added_at) for cards that come in together: add_card(batch=...)"""
        return uuid.uuid4().hex, now()

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

    def _take_from_batch(self, row_id, batch, count):
        """Up to `count` copies leave one batch of an entry; returns how many did"""
        row = self.conn.execute('SELECT id, quantity FROM inventory_batches WHERE inventory_id = ? AND batch = ?',
                                (row_id, batch)).fetchone()
        if not row:
            return 0
        taken = min(count, row['quantity'])
        if taken == row['quantity']:
            self.conn.execute('DELETE FROM inventory_batches WHERE id = ?', (row['id'],))
        else:
            self.conn.execute('UPDATE inventory_batches SET quantity = quantity - ? WHERE id = ?', (taken, row['id']))
        return taken

    def _trim_batches(self, row_id, quantity):
        """An entry's batches never account for more copies than it has - for what was
        taken out of them already (copies that left are consumed with _take_from_batches:
        this only takes back what is left over, and would leave an older, untracked copy's
        batch standing)"""
        known = self.conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM inventory_batches WHERE inventory_id = ?',
                                  (row_id,)).fetchone()[0]
        if known > quantity:
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

    def rebuild_batches(self):
        """Inside an edit that put entries in place without their batches (a backup made
        before inventory_batches): the one batch each entry itself knows of"""
        self.conn.execute('DELETE FROM inventory_batches')
        self.conn.execute(BACKFILL_BATCHES)

    def _upsert(self, values, batch=None, added_at=None):
        """Put copies in (merged with the entry that has their key); with a batch, noted as
        part of it. Returns the entry's id"""
        self.conn.execute(UPSERT, values)
        row_id = self.conn.execute(
            f"SELECT id FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
            [values[c] for c in KEY_COLUMNS]).fetchone()['id']
        if batch:
            self._note_batch(row_id, batch, added_at, values['quantity'])
        return row_id

    def _move_captures(self, from_id, to_id, count=None):
        """Move the newest `count` captures (all if None) of one entry to another"""
        limit = '' if count is None else f'ORDER BY id DESC LIMIT {int(count)}'
        self.conn.execute(f'''UPDATE inventory_captures SET inventory_id = ? WHERE id IN (
            SELECT id FROM inventory_captures WHERE inventory_id = ? {limit})''', (to_id, from_id))

    def _move_sources(self, from_id, to_id, quantity=None):
        """
        Which station scanned them goes with copies that move from one entry to another (a
        split or a merge; call it before the entry's quantity and captures change). All of the
        entry's copies, or `quantity` of them: those are the newest ones, like the captures that
        move with them - so the stations of the newest captures give up a copy each; and if the
        copies that stay are fewer than the stations still claim (copies without a capture), the
        station that scanned last gives up more.
        """
        # Newest station first where no capture says which scanned last (an add without a photo)
        owned = {row['station']: row['quantity'] for row in self.conn.execute(
            'SELECT station, quantity FROM inventory_sources WHERE inventory_id = ? AND quantity > 0 '
            'ORDER BY rowid DESC', (from_id,))}
        if not owned:
            return
        if quantity is None:
            moving = dict(owned)
        else:
            moving = {station: 0 for station in owned}
            newest = [row['station'] for row in self.conn.execute(
                'SELECT station FROM inventory_captures WHERE inventory_id = ? ORDER BY id DESC', (from_id,))]
            for station in newest[:quantity]:
                if station in owned and moving[station] < owned[station]:
                    moving[station] += 1
            entry = self._get_row(from_id)
            staying = max(0, (entry['quantity'] if entry else 0) - quantity)
            too_many = sum(owned.values()) - sum(moving.values()) - staying
            # Stations by their newest capture, then the ones without any
            by_recency = list(dict.fromkeys(station for station in newest if station in owned)) \
                + [station for station in owned if station not in newest]
            for station in by_recency:
                while too_many > 0 and moving[station] < owned[station] and sum(moving.values()) < quantity:
                    moving[station] += 1
                    too_many -= 1
        for station, copies in moving.items():
            if copies:
                self._set_source(from_id, station, owned[station] - copies)
                self.conn.execute("""INSERT INTO inventory_sources (inventory_id, station, quantity) VALUES (?, ?, ?)
                                     ON CONFLICT(inventory_id, station) DO UPDATE SET quantity = quantity + excluded.quantity""",
                                  (to_id, station, copies))

    def _trim_sources(self, row_id, quantity):
        """
        An entry is about to have `quantity` copies (fewer than before; call this before its
        captures are trimmed): the stations must not claim more than that between them. The
        copies that go are the newest, like the captures that go with them (_trim_captures) -
        so copies nobody scanned (added by hand) go first only in the count, then the stations
        of the newest captures give up a copy each, then the station that scanned last gives
        up more. Each station was capped at the entry's quantity on its own before, so two
        stations could both still show the two copies left of four.
        """
        owned = {row['station']: row['quantity'] for row in self.conn.execute(
            'SELECT station, quantity FROM inventory_sources WHERE inventory_id = ? AND quantity > 0 '
            'ORDER BY rowid DESC', (row_id,))}
        too_many = sum(owned.values()) - max(0, quantity)
        if too_many <= 0:
            return
        captures = [row['station'] for row in self.conn.execute(
            'SELECT station FROM inventory_captures WHERE inventory_id = ? ORDER BY id DESC', (row_id,))]
        going = captures[:max(0, len(captures) - quantity)]  # the captures _trim_captures removes, newest first
        by_recency = list(dict.fromkeys(station for station in captures if station in owned)) \
            + [station for station in owned if station not in captures]
        for station in going + [station for station in by_recency for _ in range(owned[station])]:
            if too_many > 0 and owned.get(station, 0) > 0:
                owned[station] -= 1
                too_many -= 1
        for station, copies in owned.items():
            self._set_source(row_id, station, copies)

    def _trim_captures(self, row_id, quantity):
        """No more captures than copies: when copies are removed, the newest captures go -
        lowering a quantity is mostly taking back a card captured twice"""
        self._delete_captures('''inventory_id = ? AND id NOT IN (SELECT id FROM inventory_captures
                                 WHERE inventory_id = ? ORDER BY id LIMIT ?)''', (row_id, row_id, quantity))

    def captures_by_entry(self, game=None, station=None):
        """{inventory id: [{'url', 'captured_at'}, ...] newest first} - with a station, only its captures"""
        where, params = ('WHERE i.game = ?', (game,)) if game else ('WHERE 1', ())
        if station:
            where, params = where + ' AND c.station = ?', params + (station,)
        result = {}
        with self._lock:
            for row in self.conn.execute(f'''
                    SELECT c.inventory_id, c.file, c.captured_at FROM inventory_captures c
                    JOIN inventory i ON i.id = c.inventory_id {where} ORDER BY c.id DESC''', params):
                result.setdefault(row['inventory_id'], []).append(
                    {'url': f"/captures/{row['file']}", 'captured_at': row['captured_at']})
        return result

    @_writes
    def add_card(self, fields, game, finish, condition='Near Mint', quantity=1, capture=None, location='',
                 quiet=False, when=None, source=None, capture_key=None, batch=None):
        """
        Add copies of a printing (fields from Game.inventory_fields) - merged with an existing
        entry for the same card, set, number, condition, finish and location. capture: the
        scanned image of the card, kept as a thumbnail with the entry. when: the time the card
        was captured ('YYYY-MM-DD HH:MM:SS'), when it is added later than that - cards read by
        the AI are added after cards dropped later, and the list is in dropping order. source:
        who adds it, for undo_last_add (a station's id; None: the scanner page). capture_key:
        the uid of the capture this card comes from (pending.py) - noted in the same commit, and
        a capture that is in here already is not added again (its entry's id is returned).
        batch: from new_batch(), when several cards come in together (they can then be taken
        back out together); a card on its own is a batch of its own.
        """
        if capture_key:
            applied = self.conn.execute('SELECT target FROM applied_captures WHERE key = ?', (capture_key,)).fetchone()
            if applied:
                self.log(f"Capture {capture_key} was added before (a restart read it again): not added twice", level="warning")
                return int(applied['target'].split(':')[1])
        quantity = max(1, int(quantity or 1))
        values = {
            'game': game, 'card_id': fields.get('card_id'), 'card_name': fields['name'],
            'set_name': fields.get('set_name') or '', 'set_code': fields.get('set_code') or None,
            'card_number': fields.get('number') or '', 'rarity': fields.get('rarity'),
            'type_line': fields.get('type_line'), 'mana_cost': fields.get('mana_cost'),
            'colors': fields.get('colors'), 'color_identity': fields.get('color_identity'),
            'price_usd': float(fields.get('price') or 0), 'quantity': quantity,
            'condition': condition or 'Near Mint', 'finish': finish, 'timestamp': when or now(),
            'location': (location or '').strip(), 'tags': '',
        }
        batch, added_at = batch or (self.new_batch()[0], values['timestamp'])
        values['added_at'] = added_at
        thumbnail = self._make_thumbnail(capture) if capture else None
        with self._lock:
            self.conn.execute(UPSERT, values)
            row = self.conn.execute(
                f"SELECT id, quantity FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                [values[c] for c in KEY_COLUMNS]).fetchone()
            self._note_batch(row['id'], batch, added_at, quantity)
            capture_id = None
            if thumbnail:
                # capture_key: this row is the capture's copy from here on - it moves with the
                # copy through splits, merges and printing changes (_move_captures), and is
                # gone when the copy is (captured_copy reads it)
                capture_id = self.conn.execute(
                    'INSERT INTO inventory_captures (inventory_id, file, captured_at, station, capture_key) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (row['id'], thumbnail, values['timestamp'], source, capture_key)).lastrowid
            if source:
                # Beside the entry, not in it: the same printing scanned by two cameras is one entry
                self.conn.execute('''INSERT INTO inventory_sources (inventory_id, station, quantity) VALUES (?, ?, ?)
                                     ON CONFLICT(inventory_id, station) DO UPDATE SET quantity = quantity + excluded.quantity''',
                                  (row['id'], source, quantity))
            if capture_key:
                if capture_id is None:
                    # No photo row to carry the capture: where its copy is cannot be followed
                    self.conn.execute('INSERT OR IGNORE INTO unfollowed_captures (key, station, noted_at) VALUES (?, ?, ?)',
                                      (capture_key, source, now()))
                self.conn.execute('INSERT INTO applied_captures (key, target, applied_at) VALUES (?, ?, ?)',
                                  (capture_key, f"inventory:{row['id']}", now()))
            self._commit()
            self.last_added[source] = (row['id'], quantity, capture_id, batch)
        if not quiet:
            self.log(f"Added to inventory: {quantity}x {values['card_name']} ({finish}) - "
                     f"${values['price_usd'] * row['quantity']:.2f} for {row['quantity']}", level="success")
        return row['id']

    @_writes
    def replace_all(self, game, entries):
        """
        Everything of a game in this inventory replaced by `entries`, as one change: either
        the old contents are gone and the new ones are in, or nothing happened. For an
        inventory that mirrors something kept elsewhere - the digital collection, which is a
        copy of what MTG Arena has (core/arena.py) - where merging would only keep what is
        no longer true.

        entries: [{'fields' (Game.inventory_fields), 'finish', 'quantity'}]; two entries for
        the same printing and finish are added up. Returns how many entries there are now.
        Not for the collection or the scanned cards: it asks nobody, keeps no undo, and
        takes captures and stations' counts with it.
        """
        batch, moment = self.new_batch()
        self._delete_entries('game = ?', (game,))
        for entry in entries:
            fields = entry['fields']
            self._upsert({
                'game': game, 'card_id': fields.get('card_id'), 'card_name': fields['name'],
                'set_name': fields.get('set_name') or '', 'set_code': fields.get('set_code') or None,
                'card_number': fields.get('number') or '', 'rarity': fields.get('rarity'),
                'type_line': fields.get('type_line'), 'mana_cost': fields.get('mana_cost'),
                'colors': fields.get('colors'), 'color_identity': fields.get('color_identity'),
                'price_usd': float(fields.get('price') or 0), 'quantity': max(1, int(entry.get('quantity') or 1)),
                'condition': 'Near Mint', 'finish': entry['finish'], 'timestamp': moment, 'added_at': moment,
                'location': '', 'tags': ''}, batch, moment)
        self._commit()
        self.last_added = {}
        return self.conn.execute('SELECT COUNT(*) FROM inventory WHERE game = ?', (game,)).fetchone()[0]

    @_writes
    def set_price(self, row_id, price):
        with self._lock:
            self.conn.execute('UPDATE inventory SET price_usd = ? WHERE id = ?', (float(price), row_id))
            self._commit()

    @_writes
    def undo_last_add(self, source=None):
        """
        Take back the most recent add_card of a source (see add_card): lower that entry's
        quantity by the amount added, deleting it if nothing is left. Returns the card name, or None.
        """
        with self._lock:
            if source not in self.last_added:
                return None
            row_id, quantity, capture_id, batch = self.last_added.pop(source)
            row = self.conn.execute('SELECT card_name FROM inventory WHERE id = ?', (row_id,)).fetchone()
            if not row:
                return None
            if source:
                had = self.conn.execute('SELECT quantity FROM inventory_sources WHERE inventory_id = ? AND station = ?',
                                        (row_id, source)).fetchone()
                self._set_source(row_id, source, (had['quantity'] if had else 0) - quantity)
            if capture_id:
                self._delete_captures('id = ?', (capture_id,))
            self._take_copies(row_id, quantity, batch=batch)  # what the add brought leaves its own batch first
            self._commit()
        self.log(f"Undid add: {quantity}x {row['card_name']}")
        return row['card_name']

    @_writes
    def capture_keys(self, game, station=None):
        """The uids of the captures that have a copy here (of one station's, with `station`)"""
        query = '''SELECT c.capture_key FROM inventory_captures c JOIN inventory i ON i.id = c.inventory_id
                   WHERE c.capture_key IS NOT NULL AND i.game = ?'''
        values = [game]
        if station:
            query += ' AND c.station = ?'
            values.append(station)
        with self._lock:
            return {row['capture_key'] for row in self.conn.execute(query, values)}

    def captured_copy(self, capture_key, station, remove=False):
        """
        Where the copy of one capture is (a phone asks about its scans; server/stations_api.py:
        station_capture_sync), and - with remove - that copy taken out. Returns (state, card name):

          'scanned'      the copy is here                            (remove=False)
          'removed'      it was here and is gone now                 (remove=True)
          'unfollowed'   it was added, but without a photo row, or its row belongs to another
                         station: nothing says where it is now - never reported as gone
          None           no copy of this capture is here: it went to the collection, was
                         removed, or was never added (the caller knows which captures exist)

        The copy is the row of inventory_captures that carries the capture's key - not "a copy
        of that card": of three identical cards scanned in a row, the one asked for goes. The
        row stays with its copy when entries are split, merged or given another printing, so
        the entry is whichever one it is in now. The collection is another file and never
        touched from here: once "Add to collection" has moved the copy, there is no row and
        nothing to remove. What the capture's upload noted (applied_captures) stays as it is, so
        a capture sent or read again after its copy was removed does not bring the card back.
        """
        with self._lock:
            row = self.conn.execute(
                '''SELECT c.id, c.inventory_id, c.station, i.card_name, i.quantity FROM inventory_captures c
                   JOIN inventory i ON i.id = c.inventory_id WHERE c.capture_key = ?''', (capture_key,)).fetchone()
            if row is None:
                unfollowed = self.conn.execute('SELECT 1 FROM unfollowed_captures WHERE key = ?', (capture_key,)).fetchone()
                return ('unfollowed' if unfollowed else None), None
            if row['station'] != station:
                return 'unfollowed', None  # not this station's to ask about or remove
            if not remove:
                return 'scanned', row['card_name']
            entry = row['inventory_id']
            self._delete_captures('id = ?', (row['id'],))
            # The station gives up the copy it scanned. After a split its row beside this
            # entry may be missing (which station owns a moved copy is worked out from the
            # newest photos, _move_sources): then the stations together must still not claim
            # more copies than are left
            had = self.conn.execute('SELECT quantity FROM inventory_sources WHERE inventory_id = ? AND station = ?',
                                    (entry, station)).fetchone()
            if had:
                self._set_source(entry, station, had['quantity'] - 1)
            self._trim_sources(entry, row['quantity'] - 1)
            self._take_copies(entry, 1)  # the entry itself goes with its last copy
            # The station's Undo would take another copy of this entry if it pointed here
            last = self.last_added.get(station)
            if last and (last[0] == entry or last[2] == row['id']):
                self.last_added.pop(station, None)
            self._commit()
        self.log(f"Removed from the scanned cards: {row['card_name']} (its capture was removed at the station)")
        return 'removed', row['card_name']

    # ------------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------------

    def get_all_cards(self, game=None, station=None):
        """
        Inventory rows (newest first), optionally only one game's. With a station (the scanned
        cards' camera filter): only what that station scanned - its copies of each entry (as
        'quantity') and its captures.
        """
        if station:
            with self._lock:
                rows = self._station_rows(game, station)
            captures = self.captures_by_entry(game, station)
            # 'shared': another camera (or a manual add) has copies in the same entry - the page
            # then offers no edit / delete, which would act on the whole entry
            batches = self.batches_by_entry(game)
            return [{**_row_dict(row, batches.get(row['id'], ())), 'quantity': copies, 'entry_quantity': row['quantity'],
                     'shared': copies != row['quantity'], 'captures': captures.get(row['id'], [])}
                    for row, copies in rows]
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

    def get_stats(self, game=None, station=None):
        """Totals for the top bar and the inventory window - with a station, of what it scanned"""
        if station:
            with self._lock:
                rows = self._station_rows(game, station)
            finishes = {}
            for row, copies in rows:
                finishes[row['finish']] = finishes.get(row['finish'], 0) + copies
            return {'total_cards': sum(copies for _, copies in rows), 'unique_cards': len(rows),
                    'total_value': sum((row['price_usd'] or 0) * copies for row, copies in rows), 'finishes': finishes}
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

    @_writes
    def delete_card(self, row_id, quiet=False):
        with self._lock:
            row = self._get_row(row_id)
            if not row:
                return False
            self._delete_entries('id = ?', (row_id,))
            self._commit()
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
        (merging), with the newest `quantity` of its captures and the stations that scanned
        them; returns the id of the entry they went to"""
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
        self._take_from_batches(row['id'], quantity, to_id=target)
        self._add_tags(target, values['tags'])
        self._move_sources(row['id'], target, quantity)
        self._move_captures(row['id'], target, quantity)
        return target

    @_writes
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
        with self._lock:
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
                self._commit()
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
                # Which station scanned them goes along (only as many as go, if the quantity was lowered too)
                self._move_sources(row_id, twin['id'], new_quantity if new_quantity < row['quantity'] else None)
                self._trim_captures(row_id, new_quantity)
                self._move_captures(row_id, twin['id'])
                self.conn.execute('UPDATE inventory SET quantity = quantity + ? WHERE id = ?', (new_quantity, twin['id']))
                if new_quantity < row['quantity']:
                    self._take_from_batches(row_id, row['quantity'] - new_quantity)  # the copies given up first
                self._take_from_batches(row_id, new_quantity, to_id=twin['id'])
                self._add_tags(twin['id'], new_tags)
                self._delete_entries('id = ?', (row_id,))  # its captures and stations went to the twin
            else:
                self.conn.execute('UPDATE inventory SET quantity = ?, condition = ?, finish = ?, price_usd = ?, '
                                  'location = ?, tags = ? WHERE id = ?',
                                  (new_quantity, new_condition, new_finish, new_price, new_location, new_tags, row_id))
                if printing:
                    self.conn.execute(f"UPDATE inventory SET {', '.join(c + ' = ?' for c in self.PRINTING_COLUMNS)} WHERE id = ?",
                                      (*(printing[c] for c in self.PRINTING_COLUMNS), row_id))
                self._trim_sources(row_id, new_quantity)
                self._trim_captures(row_id, new_quantity)
                if new_quantity < row['quantity']:
                    self._take_from_batches(row_id, row['quantity'] - new_quantity)  # the newest go, as captures do
                self._trim_batches(row_id, new_quantity)
            self._commit()
        if not quiet:
            self.log(f"Updated {row['card_name']}: {new_quantity}x {new_condition}, {new_finish}"
                     + (f", {new_location}" if new_location else '')
                     + (f", now {printing['set_name']} #{printing['card_number']}" if printing else ''), level="success")
        return {'success': True, 'split': False, 'message': 'Card updated'}

    BULK_ACTIONS = ('delete', 'condition', 'location', 'add_tag', 'remove_tag')

    @_writes
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
        with self._lock:
            # One edit: all the entries or none. What the database would refuse part way is
            # refused before anything changes - whole stacks that go or move while a trade holds
            # copies of them - so the user gets the message instead of a rolled-back error
            if action in ('delete', 'location') and row_ids and self.conn.execute(
                    f"SELECT 1 FROM trade_cards WHERE inventory_id IN ({', '.join('?' * len(row_ids))})",
                    tuple(row_ids)).fetchone():
                raise sqlite3.IntegrityError(TRADE_HELD)
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

    def take_from(self, source, game, location=None, station=None):
        """
        Move every entry of a game from another inventory (the scanner's) into this one, with
        its captures; entries that exist here already get the copies added. With a location,
        every entry arrives there, whatever location it was scanned into. With a station, only
        the copies that station scanned move (and its captures); the rest stays. Returns
        {'entries', 'cards'} moved.
        """
        batch, added_at = self.new_batch()  # one for the whole move (the collection page can filter by it)
        with self._lock, source._lock:
            if station:
                moving = source._station_rows(game, station)
            else:
                moving = [(row, row['quantity']) for row in source.conn.execute(
                    'SELECT * FROM inventory WHERE game = ? ORDER BY id', (game,)).fetchall()]
            if not moving:
                return {'entries': 0, 'cards': 0}
            # Two files, two commits: the move is noted in the source first, so a crash between
            # them is finished on the next start instead of leaving the cards in both
            # (finish_interrupted_moves)
            move_id = uuid.uuid4().hex
            source.conn.execute(PENDING_MOVES_TABLE)
            source.conn.execute('INSERT OR REPLACE INTO pending_moves (game, move_id, added_at, station) VALUES (?, ?, ?, ?)',
                                (game, move_id, added_at, station))
            source._commit()
            try:
                self.conn.execute(ARRIVED_MOVES_TABLE)
                self.conn.execute('INSERT INTO arrived_moves (move_id) VALUES (?)', (move_id,))
                for row, copies in moving:
                    values = {column: row[column] for column in row.keys() if column not in ('id', 'station_copies')}
                    values['quantity'] = copies
                    values['added_at'] = added_at
                    if location:
                        values['location'] = location
                    self.conn.execute(UPSERT, values)
                    target = self.conn.execute(
                        f"SELECT id FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                        [values[c] for c in KEY_COLUMNS]).fetchone()['id']
                    self._add_tags(target, values['tags'])
                    self._note_batch(target, batch, added_at, copies)
                    captures = ('SELECT file, captured_at FROM inventory_captures WHERE inventory_id = ? '
                                + ('AND station = ? ' if station else '') + 'ORDER BY id')
                    for capture in source.conn.execute(captures, (row['id'], station) if station else (row['id'],)):
                        self.conn.execute('INSERT INTO inventory_captures (inventory_id, file, captured_at) VALUES (?, ?, ?)',
                                          (target, capture['file'], capture['captured_at']))
                self._commit()
            except Exception:
                # Nothing arrived here: the cards stay where they were
                self._rollback()
                source.conn.execute('DELETE FROM pending_moves WHERE game = ?', (game,))
                source._commit()
                raise
            self._remove_moved(source, game, station)
            self.conn.execute('DELETE FROM arrived_moves WHERE move_id = ?', (move_id,))
            self._commit()
        moved = {'entries': len(moving), 'cards': sum(copies for _, copies in moving)}
        self.log(f"Added to the collection: {moved['cards']} cards ({moved['entries']} entries)", level="success")
        return moved

    @staticmethod
    def _remove_moved(source, game, station=None):
        """Second half of take_from: the cards are in the collection, so they go from the
        source - with the note of the move, in one commit. The capture rows go without their
        files, which moved. With a station only its copies and captures go: worked out again
        from what the station added, which nothing has changed since the first half (also not
        a crash in between - finish_interrupted_moves comes here too)"""
        if station:
            for row, copies in source._station_rows(game, station):
                source.conn.execute('DELETE FROM inventory_captures WHERE inventory_id = ? AND station = ?',
                                    (row['id'], station))
                source._set_source(row['id'], station, 0)
                source._take_copies(row['id'], copies)
        else:
            source.conn.execute('DELETE FROM inventory_captures WHERE inventory_id IN '
                                '(SELECT id FROM inventory WHERE game = ?)', (game,))
            source._delete_entries('game = ?', (game,))
        source.conn.execute('DELETE FROM pending_moves WHERE game = ?', (game,))
        source._commit()
        if station:
            source.last_added.pop(station, None)  # the other cameras keep their Undo
        else:
            source.last_added = {}

    def finish_interrupted_moves(self, source):
        """
        On startup: a take_from the app did not get through (crash, power cut). If its cards
        arrived here (the move's id is in arrived_moves - committed together with them) they
        are removed from the source; otherwise nothing was moved and only the note goes.
        Returns the games whose move was finished.
        """
        finished = []
        with self._lock, source._lock:
            source.conn.execute(PENDING_MOVES_TABLE)
            self.conn.execute(ARRIVED_MOVES_TABLE)
            for move in source.conn.execute('SELECT game, move_id, added_at, station FROM pending_moves').fetchall():
                arrived = self.conn.execute('SELECT 1 FROM arrived_moves WHERE move_id = ?', (move['move_id'],)).fetchone()
                if arrived:
                    self._remove_moved(source, move['game'], move['station'])
                    finished.append(move['game'])
                    self.log(f"Finished the interrupted \"Add to collection\" of {move['added_at']}: "
                             "the cards were in the collection already, removed from the scanned cards", level="warning")
                else:
                    source.conn.execute('DELETE FROM pending_moves WHERE game = ?', (move['game'],))
                    source._commit()
                    self.log(f"An \"Add to collection\" of {move['added_at']} was interrupted before any card "
                             "was moved: the scanned cards are still waiting", level="warning")
            self.conn.execute('DELETE FROM arrived_moves')  # nothing is under way any more
            self._commit()
        return finished

    @_writes
    def remove_batch(self, game, batch):
        """
        Take back what came into the inventory together (an "Add to collection", an import):
        each entry loses the copies that came with that batch, with their captures (the
        newest); entries left with no copies are deleted. Returns {'entries', 'cards'} removed.
        """
        with self._lock:
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
                    self._trim_sources(row['id'], left)
                    self._trim_captures(row['id'], left)
                    self._trim_batches(row['id'], left)
            self._commit()
            self.last_added = {}
        removed = {'entries': len(rows), 'cards': sum(row['added'] for row in rows)}
        if rows:
            self.log(f"Removed the cards added {rows[0]['added_at']}: {removed['cards']} cards "
                     f"({removed['entries']} entries)", level="success")
        return removed

    def clear_inventory(self, game=None, station=None):
        """
        Delete every entry (of one game, if given). With a station (the scanned cards of one
        camera): only the copies that station scanned, with its captures - an entry another
        camera also scanned keeps the other's copies. 'deleted' is then the number of cards.
        """
        if station:
            try:
                with self._lock:
                    rows = self._station_rows(game, station)
                    for row, copies in rows:
                        self._delete_captures('inventory_id = ? AND station = ?', (row['id'], station))
                        self._set_source(row['id'], station, 0)
                        self._take_copies(row['id'], copies)
                    self._commit()
                    self.last_added.pop(station, None)
                deleted = sum(copies for _, copies in rows)
                self.log(f"Scanned cards of {station} cleared: {deleted} cards removed", level="success")
                return {'success': True, 'deleted': deleted}
            except Exception as e:
                self._rollback()
                self.log(f"Failed to clear the scanned cards of {station}: {e}", level="error")
                return {'success': False, 'error': str(e), 'deleted': 0}
        where, params = ('WHERE game = ?', (game,)) if game else ('', ())
        try:
            with self._lock:
                deleted = self._delete_entries('game = ?' if game else '1', params)
                self._commit()
                self.last_added = {}
            self.log(f"Inventory cleared: {deleted} entries removed", level="success")
            return {'success': True, 'deleted': deleted}
        except Exception as e:
            self._rollback()
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

    @_writes
    def import_entries(self, entries, game, replace_existing=False):
        """
        Add the cards of another app's collection file (entries from Game.import_rows: matched
        to their printings) to one game's inventory, or replace it with them.

        Returns:
            dict: success, added (new entries), updated (copies added to an existing entry)
        """
        stats = {'success': True, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
        batch, timestamp = self.new_batch()  # the whole file is one batch
        if replace_existing and not entries:
            # Nothing to put in its place: the inventory is not emptied for a file without cards
            return {**stats, 'success': False, 'error': 'No card of the file could be imported - the inventory was not replaced'}
        with self._lock:
            if replace_existing:
                self._delete_entries('game = ?', (game,))
                self.last_added = {}
            for entry in entries:
                fields = entry['fields']
                values = {
                    'game': game, 'card_id': fields.get('card_id'), 'card_name': fields['name'],
                    'set_name': fields.get('set_name') or '', 'set_code': fields.get('set_code') or None,
                    'card_number': fields.get('number') or '', 'rarity': fields.get('rarity'),
                    'type_line': fields.get('type_line'), 'mana_cost': fields.get('mana_cost'),
                    'colors': fields.get('colors'), 'color_identity': fields.get('color_identity'),
                    'price_usd': float(fields.get('price') or 0), 'quantity': entry['quantity'],
                    'condition': entry.get('condition') or 'Near Mint', 'finish': entry['finish'],
                    'timestamp': timestamp, 'added_at': timestamp, 'location': '',
                    'tags': ', '.join(clean_tags(entry.get('tags') or '')),
                }
                exists = self.conn.execute(
                    f"SELECT 1 FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                    [values[c] for c in KEY_COLUMNS]).fetchone()
                self._upsert(values, batch, timestamp)
                stats['updated' if exists else 'added'] += 1
            self._commit()
        self.log(f"Import complete: {stats['added']} added, {stats['updated']} updated", level="success")
        return stats

    @_writes
    def import_csv(self, csv_file_path, game, finishes, replace_existing=False):
        """
        Import a CSV in the app's own columns (games.base.write_collection_csv - with Card ID,
        Set Code and Timestamp an entry comes back as it was - and the CSVs written before
        that, which lack them: Card Name, Set, Card Number, ..., Quantity,
        Condition, Finish or the older Foil / Surge columns, and Location / Tags when present)
        into one game's inventory.

        Args:
            finishes: the game's finish keys - the first is used when a row has none

        Returns:
            dict: success, added, updated, skipped, errors
        """
        csv_file_path = Path(csv_file_path)
        stats = {'success': True, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
        if not csv_file_path.exists():
            return {**stats, 'success': False, 'error': 'File not found'}

        # The whole file is read before anything is changed: replacing the inventory with a
        # file none of whose rows can be used once left it empty
        batch, imported_at = self.new_batch()  # the whole file is one batch
        accepted = []
        with open(csv_file_path, newline='') as f:
            for row_number, row in enumerate(csv.DictReader(f), start=2):
                try:
                    # Headers as the format check reads them: "card name " is "Card Name"
                    row = _Columns(row)
                    name = (row.get('Card Name') or '').strip()
                    set_name = (row.get('Set') or '').strip()
                    if not name or not set_name:
                        self.log(f"Row {row_number}: skipped - missing name or set", level="warning")
                        stats['skipped'] += 1
                        continue
                    finish = (row.get('Finish') or '').strip().lower()
                    if not finish:
                        yes = lambda column: (row.get(column) or '').strip().lower() in ('yes', 'true', '1')
                        finish = 'surge' if yes('Surge') else 'foil' if yes('Foil') else finishes[0]
                    if finish not in finishes:
                        self.log(f"Row {row_number}: unknown finish '{finish}'", level="warning")
                        stats['errors'] += 1
                        continue
                    try:
                        quantity = max(1, int(row.get('Quantity') or 1))
                    except ValueError:
                        quantity = 1
                    try:
                        price = float((row.get('Price (USD)') or '0').replace('$', '').replace(',', ''))
                    except ValueError:
                        price = 0.0
                    timestamp = (row.get('Timestamp') or '').strip()
                    accepted.append({
                        'game': game, 'card_id': (row.get('Card ID') or '').strip() or None,
                        'card_name': name, 'set_name': set_name,
                        'set_code': (row.get('Set Code') or '').strip() or None, 'card_number': (row.get('Card Number') or '').strip(),
                        'rarity': (row.get('Rarity') or '').strip(), 'type_line': (row.get('Type') or '').strip(),
                        'mana_cost': (row.get('Mana Cost') or '').strip(), 'colors': (row.get('Colors') or '').strip(),
                        'color_identity': (row.get('Color Identity') or '').strip(), 'price_usd': price,
                        'quantity': quantity, 'condition': (row.get('Condition') or '').strip() or 'Near Mint',
                        'finish': finish,
                        'timestamp': timestamp if TIMESTAMP.match(timestamp) else now(),
                        'location': (row.get('Location') or '').strip(),
                        'tags': ', '.join(clean_tags(row.get('Tags') or '')),
                        'added_at': imported_at,
                    })
                except Exception as e:
                    self.log(f"Row {row_number}: error importing card - {e}", level="error")
                    stats['errors'] += 1
        if replace_existing and not accepted:
            return {**stats, 'success': False, 'error': 'No row of the file could be imported - the inventory was not replaced'}
        if replace_existing and (stats['skipped'] or stats['errors']):
            # Added to what is there, rows that cannot be used are only left out; put in its
            # place, they would be cards lost without a word
            unusable = stats['skipped'] + stats['errors']
            return {**stats, 'success': False,
                    'error': f"{unusable} row{'s' if unusable != 1 else ''} of the file cannot be imported (missing "
                             'name or set, unknown finish) - the inventory was not replaced. Correct the file, or add it '
                             'instead of replacing.'}

        with self._lock:
            if replace_existing:
                self._delete_entries('game = ?', (game,))
                self.last_added = {}
            for values in accepted:
                exists = self.conn.execute(
                    f"SELECT 1 FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                    [values[c] for c in KEY_COLUMNS]).fetchone()
                self._upsert(values, batch, imported_at)
                stats['updated' if exists else 'added'] += 1
            self._commit()

        self.log(f"Import complete: {stats['added']} added, {stats['updated']} updated, "
                 f"{stats['skipped']} skipped, {stats['errors']} errors", level="success")
        return stats

    def close(self):
        if self.conn:
            self.conn.close()
