"""
Review queue: captures that were not added automatically (printing not confirmed, name not
found, nothing read) while cards are added automatically. Scanning goes on; the queue is
worked through at the end, with the capture next to the suggested card and a manual search.

Items live in the card database file (table `review_queue`), per game and per station - each
camera's page shows its own - with a copy of the capture in data/review/ (scanned_cards/ is
cleaned after cleanup.days) until resolved.
"""
import logging
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime

import storage
from config import Config
from pending import APPLIED_CAPTURES_TABLE, prune_applied

logger = logging.getLogger('database')

REVIEW_DIR = Config.DATA_DIR / 'review'
# count / first: every station's items. A station id narrows it to that station's and those
# that are nobody's (see _of); None to only those that are nobody's
ANY = object()


class ReviewQueue:
    def __init__(self, db_file=None):
        self.conn = storage.connect(db_file or Config.DATABASE_FILE)
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,
                    file TEXT,              -- copy of the capture in REVIEW_DIR
                    ai_name TEXT,           -- what the AI read
                    ai_number TEXT,
                    ai_set TEXT,
                    foil TEXT,              -- foil / non-foil / unknown (the star/dot marker)
                    card_id TEXT,           -- best match, if any
                    match TEXT,             -- how it was matched (Game.confirmed_matches)
                    created_at TEXT NOT NULL,
                    station TEXT,           -- stations.py id the capture came from (NULL: nobody's - see _of)
                    capture_key TEXT        -- the uid of the capture it is (pending.py); see by_capture
                )''')
            columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(review_queue)')}
            if 'station' not in columns:
                self.conn.execute('ALTER TABLE review_queue ADD COLUMN station TEXT')
            # Which capture an item is, on the item itself (2026-10-12): applied_captures knows
            # it too, but forgets after KEEP_DAYS, and an item can wait longer than that. Items
            # that are there already have none: a phone asking about them is told so
            if 'capture_key' not in columns:
                self.conn.execute('ALTER TABLE review_queue ADD COLUMN capture_key TEXT')
            self.conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_review_capture_key '
                              'ON review_queue(capture_key) WHERE capture_key IS NOT NULL')
            # Captures that were queued here already (add's capture_key; pending.py)
            self.conn.execute(APPLIED_CAPTURES_TABLE)
            prune_applied(self.conn)
            self.conn.commit()

    def add(self, game, image_path, name='', number='', set_code='', foil='unknown', card=None, station=None,
            capture_key=None):
        """Queue a capture; returns the item id. capture_key: the capture's uid (pending.py) -
        noted in the same commit; a capture queued here before is not queued again (None)."""
        if capture_key:
            with self._lock:
                if self.conn.execute('SELECT 1 FROM applied_captures WHERE key = ?', (capture_key,)).fetchone():
                    logger.warning(f"Capture {capture_key} was queued for review before (a restart read it again): not queued twice")
                    return None
        file = None
        if image_path:
            try:
                REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                file = f"{uuid.uuid4().hex}.jpg"
                shutil.copyfile(image_path, REVIEW_DIR / file)
            except OSError as e:
                logger.warning(f"Could not keep the capture for review: {e}")
                file = None
        with self._lock:
            item_id = self.conn.execute(
                'INSERT INTO review_queue (game, file, ai_name, ai_number, ai_set, foil, card_id, match, created_at, '
                'station, capture_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (game, file, name or '', number or '', set_code or '', foil or 'unknown',
                 card['id'] if card else None, card.get('match') if card else None,
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'), station, capture_key)).lastrowid
            if capture_key:
                self.conn.execute('INSERT INTO applied_captures (key, target, applied_at) VALUES (?, ?, ?)',
                                  (capture_key, f'review:{item_id}', datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            self.conn.commit()
        return item_id

    @staticmethod
    def _of(game, station):
        """
        The WHERE clause for what a page lists. A station's page shows that station's items
        and those without a station: until 2026-10-12 the server could have a camera of its
        own, and what that camera queued has station NULL - as has a capture of it that was
        still waiting to be read at the update. That camera's page is gone with it; without
        this, those items would be on no page and could never be resolved. They show on every
        camera's page instead, and are gone from all of them once one resolves them.
        """
        if station is ANY:
            return 'game = ?', (game,)
        if station is None:
            return 'game = ? AND station IS NULL', (game,)
        return 'game = ? AND (station = ? OR station IS NULL)', (game, station)

    def count(self, game, station=ANY):
        where, values = self._of(game, station)
        with self._lock:
            return self.conn.execute(f'SELECT COUNT(*) FROM review_queue WHERE {where}', values).fetchone()[0]

    def first(self, game, station=ANY):
        """The oldest item of a game (of one station), and its position info: (item dict or None, total)"""
        where, values = self._of(game, station)
        with self._lock:
            row = self.conn.execute(f'SELECT * FROM review_queue WHERE {where} ORDER BY id LIMIT 1', values).fetchone()
        return (dict(row) if row else None), self.count(game, station)

    def get(self, item_id):
        with self._lock:
            row = self.conn.execute('SELECT * FROM review_queue WHERE id = ?', (item_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def image_path(item):
        return REVIEW_DIR / item['file'] if item and item.get('file') else None

    def clear(self, game, station):
        """
        Drop every item of one station (clearing that camera's scanned cards); returns how many.
        Exactly that station's: the items that are nobody's (see _of) show on its page too, but
        clearing one camera does not throw them away - station None drops those.
        """
        with self._lock:
            rows = self.conn.execute('SELECT file FROM review_queue WHERE game = ? AND station IS ?',
                                     (game, station)).fetchall()
            self.conn.execute('DELETE FROM review_queue WHERE game = ? AND station IS ?', (game, station))
            self.conn.commit()
        for row in rows:
            if row['file']:
                (REVIEW_DIR / row['file']).unlink(missing_ok=True)
        return len(rows)

    def by_capture(self, capture_key):
        """The item a capture is waiting as (its uid, pending.py), or None: resolved - added from
        the review, skipped or cleared - or never queued"""
        with self._lock:
            row = self.conn.execute('SELECT * FROM review_queue WHERE capture_key = ?', (capture_key,)).fetchone()
        return dict(row) if row else None

    def remove(self, item_id):
        """Resolve an item (added or skipped): delete it and its capture copy"""
        with self._lock:
            row = self.conn.execute('SELECT file FROM review_queue WHERE id = ?', (item_id,)).fetchone()
            if not row:
                return False
            self.conn.execute('DELETE FROM review_queue WHERE id = ?', (item_id,))
            self.conn.commit()
        if row['file']:
            (REVIEW_DIR / row['file']).unlink(missing_ok=True)
        return True
