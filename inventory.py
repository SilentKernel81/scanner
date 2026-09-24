# ============================================================================
# FILE: inventory.py
# Inventory management using SQLite database
# ============================================================================
import csv
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from config import Config

logger = logging.getLogger('database')

# One row per card + set + number + condition + finish, per game. Rows are addressed by id.
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
        UNIQUE(game, card_name, set_name, card_number, condition, finish)
    )
'''
KEY_COLUMNS = ('game', 'card_name', 'set_name', 'card_number', 'condition', 'finish')
UPSERT = '''
    INSERT INTO inventory (game, card_id, card_name, set_name, set_code, card_number, rarity,
                           type_line, mana_cost, colors, color_identity, price_usd, quantity,
                           condition, finish, timestamp)
    VALUES (:game, :card_id, :card_name, :set_name, :set_code, :card_number, :rarity,
            :type_line, :mana_cost, :colors, :color_identity, :price_usd, :quantity,
            :condition, :finish, :timestamp)
    ON CONFLICT(game, card_name, set_name, card_number, condition, finish) DO UPDATE SET
        quantity = quantity + excluded.quantity,
        timestamp = excluded.timestamp,
        price_usd = excluded.price_usd,
        card_id = COALESCE(excluded.card_id, card_id),
        set_code = COALESCE(excluded.set_code, set_code)
'''


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _row_dict(row):
    """Inventory row as sent to the web page and the exporters"""
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
    }


class InventoryManager:
    """Card inventory (table `inventory` in the card database file), for every game"""

    def __init__(self, db_file=None, log_callback=None):
        self.db_file = db_file or Config.DATABASE_FILE
        self.log_callback = log_callback
        self._lock = threading.RLock()
        self.last_added = None  # (row id, quantity) of the most recent add_card, for undo
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
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_game_name ON inventory(game, card_name COLLATE NOCASE)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_timestamp ON inventory(timestamp DESC)')
            self.conn.commit()

    def _migrate_to_multi_game(self, columns):
        """
        Inventories from before multi-game support had foil/surge flags and no game column.
        The UNIQUE constraint changes, so the table is rebuilt (SQLite can't alter it) -
        after copying the old table to data/backups/.
        """
        backup_dir = Config.DATA_DIR / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"inventory_before_multigame_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        self.conn.execute('ATTACH DATABASE ? AS backup', (str(backup_file),))
        self.conn.execute('CREATE TABLE backup.inventory AS SELECT * FROM main.inventory')
        self.conn.commit()
        self.conn.execute('DETACH DATABASE backup')
        before = self.conn.execute('SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM inventory').fetchone()
        logger.info(f"Inventory backed up to {backup_file} ({before[0]} rows)")

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
                ON CONFLICT(game, card_name, set_name, card_number, condition, finish)
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

    def add_card(self, fields, game, finish, condition='Near Mint', quantity=1):
        """
        Add copies of a printing (fields from Game.inventory_fields) - merged with an existing
        entry for the same card, set, number, condition and finish.
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
        }
        with self._lock:
            self.conn.execute(UPSERT, values)
            self.conn.commit()
            row = self.conn.execute(
                f"SELECT id, quantity FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                [values[c] for c in KEY_COLUMNS]).fetchone()
            self.last_added = (row['id'], quantity)
        self.log(f"Added to inventory: {quantity}x {values['card_name']} ({finish}) - "
                 f"${values['price_usd'] * row['quantity']:.2f} for {row['quantity']}", level="success")

    def undo_last_add(self):
        """
        Take back the most recent add_card: lower that entry's quantity by the amount
        added, deleting it if nothing is left. Returns the card name, or None.
        """
        with self._lock:
            if not self.last_added:
                return None
            row_id, quantity = self.last_added
            self.last_added = None
            row = self.conn.execute('SELECT card_name FROM inventory WHERE id = ?', (row_id,)).fetchone()
            if not row:
                return None
            self.conn.execute('UPDATE inventory SET quantity = quantity - ? WHERE id = ?', (quantity, row_id))
            self.conn.execute('DELETE FROM inventory WHERE id = ? AND quantity <= 0', (row_id,))
            self.conn.commit()
        self.log(f"Undid add: {quantity}x {row['card_name']}")
        return row['card_name']

    # ------------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------------

    def get_all_cards(self, game=None):
        """Inventory rows (newest first), optionally only one game's"""
        where, params = ('WHERE game = ?', (game,)) if game else ('', ())
        with self._lock:
            rows = self.conn.execute(f'SELECT * FROM inventory {where} ORDER BY timestamp DESC, id DESC', params)
            return [_row_dict(row) for row in rows]

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

    def delete_card(self, row_id):
        with self._lock:
            row = self._get_row(row_id)
            if not row:
                return False
            self.conn.execute('DELETE FROM inventory WHERE id = ?', (row_id,))
            self.conn.commit()
        self.log(f"Deleted from inventory: {row['card_name']}", level="success")
        return True

    def _copy_with(self, row, quantity, condition, finish):
        """Add `quantity` copies of an entry under another condition/finish (merging)"""
        values = dict(row)
        values.update(quantity=quantity, condition=condition, finish=finish)
        values.pop('id')
        self.conn.execute(UPSERT, values)

    def update_card(self, row_id, quantity=None, condition=None, finish=None, split_quantity=None):
        """
        Change an entry's quantity, condition or finish. Changing the finish of an entry with
        several copies moves split_quantity of them (default 1) to the new finish. An entry
        that ends up identical to another one is merged into it.

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
            if new_quantity < 1:
                return {'success': False, 'split': False, 'message': 'Quantity must be at least 1'}

            if new_finish != row['finish'] and row['quantity'] > 1:
                moved = int(split_quantity) if split_quantity is not None else 1
                if not 1 <= moved <= row['quantity']:
                    return {'success': False, 'split': False,
                            'message': f"Split quantity must be 1-{row['quantity']}"}
                remaining = row['quantity'] - moved
                if remaining:
                    self.conn.execute('UPDATE inventory SET quantity = ? WHERE id = ?', (remaining, row_id))
                else:
                    self.conn.execute('DELETE FROM inventory WHERE id = ?', (row_id,))
                self._copy_with(row, moved, new_condition, new_finish)
                self.conn.commit()
                self.log(f"{row['card_name']}: {moved} moved to {new_finish}"
                         + (f", {remaining} stay {row['finish']}" if remaining else ''), level="success")
                return {'success': True, 'split': True, 'message': 'Card updated and split'}

            # An entry that becomes identical to another one is merged into it
            twin = self.conn.execute(f'''
                SELECT id FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)} AND id != ?''',
                (row['game'], row['card_name'], row['set_name'], row['card_number'], new_condition, new_finish,
                 row_id)).fetchone()
            if twin:
                self.conn.execute('UPDATE inventory SET quantity = quantity + ? WHERE id = ?', (new_quantity, twin['id']))
                self.conn.execute('DELETE FROM inventory WHERE id = ?', (row_id,))
            else:
                self.conn.execute('UPDATE inventory SET quantity = ?, condition = ?, finish = ? WHERE id = ?',
                                  (new_quantity, new_condition, new_finish, row_id))
            self.conn.commit()
        self.log(f"Updated {row['card_name']}: {new_quantity}x {new_condition}, {new_finish}", level="success")
        return {'success': True, 'split': False, 'message': 'Card updated'}

    def clear_inventory(self, game=None):
        """Delete every entry (of one game, if given)"""
        where, params = ('WHERE game = ?', (game,)) if game else ('', ())
        try:
            with self._lock:
                deleted = self.conn.execute(f'DELETE FROM inventory {where}', params).rowcount
                self.conn.commit()
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

    def import_csv(self, csv_file_path, game, finishes, replace_existing=False):
        """
        Import a CSV written by the CSV export (Card Name, Set, Card Number, ..., Quantity,
        Condition, and Finish or the older Foil / Surge columns) into one game's inventory.

        Args:
            finishes: the game's finish keys - the first is used when a row has none

        Returns:
            dict: success, added, updated, skipped, errors
        """
        csv_file_path = Path(csv_file_path)
        stats = {'success': True, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
        if not csv_file_path.exists():
            return {**stats, 'success': False, 'error': 'File not found'}

        with self._lock:
            if replace_existing:
                self.conn.execute('DELETE FROM inventory WHERE game = ?', (game,))
                self.last_added = None
            with open(csv_file_path, newline='') as f:
                for row_number, row in enumerate(csv.DictReader(f), start=2):
                    try:
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
                        values = {
                            'game': game, 'card_id': None, 'card_name': name, 'set_name': set_name,
                            'set_code': None, 'card_number': (row.get('Card Number') or '').strip(),
                            'rarity': (row.get('Rarity') or '').strip(), 'type_line': (row.get('Type') or '').strip(),
                            'mana_cost': (row.get('Mana Cost') or '').strip(), 'colors': (row.get('Colors') or '').strip(),
                            'color_identity': (row.get('Color Identity') or '').strip(), 'price_usd': price,
                            'quantity': quantity, 'condition': (row.get('Condition') or '').strip() or 'Near Mint',
                            'finish': finish, 'timestamp': now(),
                        }
                        exists = self.conn.execute(
                            f"SELECT 1 FROM inventory WHERE {' AND '.join(c + ' = ?' for c in KEY_COLUMNS)}",
                            [values[c] for c in KEY_COLUMNS]).fetchone()
                        self.conn.execute(UPSERT, values)
                        stats['updated' if exists else 'added'] += 1
                    except Exception as e:
                        self.log(f"Row {row_number}: error importing card - {e}", level="error")
                        stats['errors'] += 1
            self.conn.commit()

        self.log(f"Import complete: {stats['added']} added, {stats['updated']} updated, "
                 f"{stats['skipped']} skipped, {stats['errors']} errors", level="success")
        return stats

    def close(self):
        if self.conn:
            self.conn.close()
