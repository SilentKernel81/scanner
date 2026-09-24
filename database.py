# ============================================================================
# FILE: database.py
# Database management for card data
# ============================================================================
import gzip
import re
import sqlite3
import json
import logging
import requests
import threading
from difflib import SequenceMatcher, get_close_matches
from config import Config
from utils import normalize_text

# Create database logger
logger = logging.getLogger('database')

# Cards table columns in canonical order (name, SQL type)
CARD_COLUMNS = [
    ('id', 'TEXT PRIMARY KEY'),
    ('name', 'TEXT NOT NULL'),
    ('flavor_name', 'TEXT'),
    ('set_code', 'TEXT'),
    ('set_name', 'TEXT'),
    ('collector_number', 'TEXT'),
    ('rarity', 'TEXT'),
    ('price_usd', 'REAL'),
    ('price_usd_foil', 'REAL'),
    ('image_uri', 'TEXT'),
    ('oracle_text', 'TEXT'),
    ('type_line', 'TEXT'),
    ('colors', 'TEXT'),
    ('mana_cost', 'TEXT'),
    # Printing treatment (from Scryfall). JSON arrays are stored as text.
    ('border_color', 'TEXT'),
    ('frame', 'TEXT'),
    ('frame_effects', 'TEXT'),
    ('full_art', 'INTEGER DEFAULT 0'),
    ('promo_types', 'TEXT'),
    ('finishes', 'TEXT'),
    ('released_at', 'TEXT'),
    # Lowercase, accent-free names for searching ("Fíli" -> "fili"), see search_key()
    ('name_search', 'TEXT'),
    ('flavor_search', 'TEXT'),
]
CARD_COLUMN_NAMES = [name for name, _ in CARD_COLUMNS]

# Manual-search treatment filters: key -> SQL condition on the cards table
TREATMENT_FILTERS = {
    'regular': ("COALESCE(border_color, '') != 'borderless' AND COALESCE(full_art, 0) = 0"
                " AND COALESCE(frame_effects, '') NOT LIKE '%\"showcase\"%'"
                " AND COALESCE(frame_effects, '') NOT LIKE '%\"extendedart\"%'"),
    'borderless': "border_color = 'borderless'",
    'showcase': "frame_effects LIKE '%\"showcase\"%'",
    'extendedart': "frame_effects LIKE '%\"extendedart\"%'",
    'fullart': "full_art = 1",
    'retro': "frame IN ('1993', '1997')",
    'etched': "finishes LIKE '%\"etched\"%'",
    'surgefoil': "promo_types LIKE '%\"surgefoil\"%'",
}

# Promo types worth showing as a treatment label
PROMO_TYPE_LABELS = {
    'surgefoil': 'Surge Foil',
    'galaxyfoil': 'Galaxy Foil',
    'textured': 'Textured',
    'serialized': 'Serialized',
}


def collector_number_variants(collector_number):
    """
    Collector numbers a scanned/typed number may correspond to in Scryfall data
    ("0330" -> {"330", "0330", "330s", "0330s"}). Returns an empty set if no digits.
    """
    number_match = re.search(r'(\d+)', collector_number or '')
    if not number_match:
        return set()
    number_str = number_match.group(1)
    base_number = str(int(number_str))  # Remove leading zeros: "0330" -> "330"
    return {base_number, number_str, base_number + 's', number_str + 's'}


def search_key(text):
    """Normalize a card name for searching: lowercase, no accents ("Fíli" -> "fili", "Æther" -> "aether")"""
    if not text:
        return None
    return normalize_text(text).lower().replace('æ', 'ae').strip()


def names_match(query, row):
    """
    Loose check that a name read from a card matches a database row: same name, a
    shortened legendary name ("Thanos" / "Thanos, the Mad Titan"), a double-faced
    card's front face, or a close spelling. Checks the flavor name too.
    """
    query_key = search_key(query)
    if not query_key:
        return False
    for full_name in (row['name'], row['flavor_name']):
        for face in (full_name or '').split(' // '):
            key = search_key(face)
            if not key:
                continue
            if key == query_key or key.startswith(query_key) or query_key.startswith(key):
                return True
            if SequenceMatcher(None, query_key, key).ratio() >= 0.6:
                return True
    return False


def _json_list(value):
    """Decode a JSON array column, tolerating NULL/invalid values"""
    if not value:
        return []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


class CardDatabase:
    """Manages local card database"""

    def __init__(self, db_file=None):
        self.db_file = db_file or Config.DATABASE_FILE
        self.conn = None
        self._lock = threading.RLock()  # Thread-safe database access
        self.initialize_database()

    def initialize_database(self):
        """Create database tables if they don't exist"""
        logger.info(f"Initializing database: {self.db_file}")
        self.conn = sqlite3.connect(
            str(self.db_file),
            check_same_thread=False,
            isolation_level='DEFERRED',  # Standardized isolation level
            timeout=10.0  # Add timeout for lock waits
        )
        # Enable WAL mode for better concurrency
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')

        self.conn.row_factory = sqlite3.Row  # Access columns by name

        cursor = self.conn.cursor()
        self._create_cards_table(cursor, if_not_exists=True)

        # Migration: add any columns missing from older databases
        existing_columns = {row['name'] for row in cursor.execute("PRAGMA table_info(cards)").fetchall()}
        for column, column_type in CARD_COLUMNS:
            if column not in existing_columns:
                logger.info(f"Adding {column} column to existing cards table")
                cursor.execute(f"ALTER TABLE cards ADD COLUMN {column} {column_type}")

        if 'name_search' not in existing_columns:
            logger.info("Filling search name columns")
            self.conn.create_function('search_key', 1, search_key, deterministic=True)
            cursor.execute("UPDATE cards SET name_search = search_key(name), flavor_search = search_key(flavor_name)")

        self._create_card_indexes(cursor)

        # Inventory table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_name TEXT NOT NULL,
                set_name TEXT NOT NULL,
                card_number TEXT,
                rarity TEXT,
                type_line TEXT,
                mana_cost TEXT,
                colors TEXT,
                color_identity TEXT,
                price_usd REAL,
                quantity INTEGER NOT NULL DEFAULT 1,
                condition TEXT DEFAULT 'Near Mint',
                foil INTEGER DEFAULT 0,
                surge INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL,
                UNIQUE(card_name, set_name, card_number, condition, foil, surge)
            )
        ''')

        # Migration: Add surge column if it doesn't exist (for existing databases)
        try:
            cursor.execute("SELECT surge FROM inventory LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("Adding surge column to existing inventory table")
            cursor.execute("ALTER TABLE inventory ADD COLUMN surge INTEGER DEFAULT 0")
            logger.info("Surge column added successfully")

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_inventory_card_name
            ON inventory(card_name COLLATE NOCASE)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_inventory_set
            ON inventory(set_name)
        ''')

        # Composite index for faster lookups by card_name + card_number
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_inventory_card_lookup
            ON inventory(card_name COLLATE NOCASE, card_number)
        ''')

        # Index on timestamp for faster ORDER BY timestamp queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_inventory_timestamp
            ON inventory(timestamp DESC)
        ''')

        self.conn.commit()
        logger.info("Database tables and indexes initialized successfully")

    def _create_cards_table(self, cursor, table='cards', if_not_exists=False):
        """Create the cards table with the canonical column order"""
        columns_sql = ',\n                '.join(f"{name} {column_type}" for name, column_type in CARD_COLUMNS)
        cursor.execute(f'''
            CREATE TABLE {'IF NOT EXISTS ' if if_not_exists else ''}{table} (
                {columns_sql}
            )
        ''')

    def _create_card_indexes(self, cursor):
        """Create performance indexes on the cards table"""
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_name ON cards(name COLLATE NOCASE)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_flavor_name ON cards(flavor_name COLLATE NOCASE)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_name_search ON cards(name_search)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_flavor_search ON cards(flavor_search)')
        # Composite index for exact version lookups
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_set_number ON cards(set_code, collector_number)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_rarity ON cards(rarity)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_type ON cards(type_line)')

    def download_scryfall_data(self, progress_callback=None):
        """Download latest Scryfall bulk data"""
        if progress_callback:
            progress_callback("Fetching Scryfall bulk data information...")
        
        # Scryfall asks API clients to send a User-Agent and Accept header
        headers = {'User-Agent': 'CardScanner/1.0', 'Accept': 'application/json'}
        response = requests.get(Config.SCRYFALL_BULK_URL, headers=headers, timeout=30)
        if response.status_code != 200:
            raise Exception("Failed to fetch bulk data info")

        bulk_info = response.json()
        # Scryfall now publishes gzipped JSON Lines (jsonl_download_uri); older API used a JSON array (download_uri)
        download_url = bulk_info.get('jsonl_download_uri') or bulk_info.get('download_uri')
        if not download_url:
            raise Exception(f"Unexpected Scryfall bulk data response: {list(bulk_info.keys())}")
        file_size = (bulk_info.get('compressed_size') or bulk_info.get('size', 0)) / (1024 * 1024)

        if progress_callback:
            progress_callback(f"Downloading card database (~{file_size:.1f} MB)...")

        response = requests.get(download_url, headers=headers, stream=True, timeout=60)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))

        # Collect raw bytes and decode once at the end - decoding per chunk
        # corrupts multi-byte UTF-8 characters split across chunk boundaries
        chunks = []
        downloaded = 0
        last_percent = -1

        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                downloaded += len(chunk)
                chunks.append(chunk)

                if total_size > 0 and progress_callback:
                    percent = int(downloaded * 100 / total_size)
                    if percent != last_percent:
                        last_percent = percent
                        progress_callback(f"Download progress: {percent}%")

        if progress_callback:
            progress_callback("Parsing card data...")

        data = b''.join(chunks)
        if data[:2] == b'\x1f\x8b':  # gzip magic bytes
            data = gzip.decompress(data)

        if '.jsonl' in download_url:
            return [json.loads(line) for line in data.splitlines() if line.strip()]
        return json.loads(data)
    
    def populate_database(self, cards_data, progress_callback=None):
        """Populate database with card data"""
        if progress_callback:
            progress_callback(f"Populating database with {len(cards_data)} cards...")
        
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM cards')
        
        inserted = 0
        for card in cards_data:
            try:
                if card.get('layout') in ['token', 'emblem', 'art_series']:
                    continue
                
                prices = card.get('prices', {})
                price_usd = prices.get('usd')
                price_usd_foil = prices.get('usd_foil')
                
                # Double-faced cards keep their images on each face
                image_uris = card.get('image_uris') or (card.get('card_faces') or [{}])[0].get('image_uris', {})
                image_uri = image_uris.get('normal', '')

                cursor.execute(f'''
                    INSERT OR REPLACE INTO cards ({', '.join(CARD_COLUMN_NAMES)})
                    VALUES ({', '.join('?' * len(CARD_COLUMN_NAMES))})
                ''', (
                    card.get('id'),
                    card.get('name'),
                    card.get('flavor_name'),
                    card.get('set'),
                    card.get('set_name'),
                    card.get('collector_number'),
                    card.get('rarity'),
                    float(price_usd) if price_usd else None,
                    float(price_usd_foil) if price_usd_foil else None,
                    image_uri,
                    card.get('oracle_text'),
                    card.get('type_line'),
                    json.dumps(card.get('colors', [])),
                    card.get('mana_cost'),
                    card.get('border_color'),
                    card.get('frame'),
                    json.dumps(card.get('frame_effects', [])),
                    1 if card.get('full_art') else 0,
                    json.dumps(card.get('promo_types', [])),
                    json.dumps(card.get('finishes', [])),
                    card.get('released_at'),
                    search_key(card.get('name')),
                    search_key(card.get('flavor_name'))
                ))
                
                inserted += 1
                if inserted % 1000 == 0 and progress_callback:
                    progress_callback(f"Inserted {inserted} cards...")
            
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Error inserting card {card.get('name')}: {e}")
                continue
        
        self.conn.commit()
        if progress_callback:
            progress_callback(f"Database populated with {inserted} cards!")
        
        return inserted
    
    def search_card_exact(self, card_name, collector_number=None, set_code=None):
        """
        Search for an exact printing by set code, collector number and name

        Strategy:
        1. Set code + collector number (unique per printing), if the name roughly matches
        2. Name (accent/case-insensitive, or shortened legendary name) + collector number
        3. Name only
        4. Fuzzy name match, then the collector number to pick the printing

        Args:
            card_name: The card name
            collector_number: Optional collector number (e.g., "123", "0330", "123s")
            set_code: Optional set code (e.g., "HOB")

        Returns:
            Card dict if found, None otherwise
        """
        with self._lock:
            logger.info(f"Searching for card: '{card_name}'" + (f" #{collector_number}" if collector_number else "")
                        + (f" [{set_code}]" if set_code else ""))
            cursor = self.conn.cursor()
            key = search_key(card_name)
            number_variants = collector_number_variants(collector_number)
            number_placeholders = ', '.join('?' * len(number_variants))

            # Step 1: Set code + collector number identify the printing exactly
            set_number_row = None
            if set_code and number_variants:
                set_number_row = self._find_by_set_number(set_code, number_variants)
                if set_number_row and names_match(card_name, set_number_row):
                    logger.info(f"Found by set + number: {set_number_row['name']} "
                                f"({set_number_row['set_code']} #{set_number_row['collector_number']})")
                    return self._format_card_result(set_number_row)
                if set_number_row:
                    logger.warning(f"{set_code} #{collector_number} is '{set_number_row['name']}', not '{card_name}' - searching by name")

            if key and number_variants:
                # Step 2: Name + collector number, then shortened name ("Thanos" for
                # "Thanos, the Mad Titan") + collector number
                for comparison, value in (('=', key), ('LIKE', key + '%')):
                    cursor.execute(f'''
                        SELECT * FROM cards
                        WHERE (name_search {comparison} ? OR flavor_search {comparison} ?)
                        AND collector_number IN ({number_placeholders})
                        LIMIT 1
                    ''', (value, value, *number_variants))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"Found name match with collector number: {result['name']} #{result['collector_number']}")
                        return self._format_card_result(result)

            # Step 3: Name only
            match = self.search_card(card_name, fuzzy=True)

            # Fuzzy matching resolves the name; use the collector number to pick the printing
            if match and number_variants:
                cursor.execute(f'''
                    SELECT * FROM cards WHERE name = ? AND collector_number IN ({number_placeholders}) LIMIT 1
                ''', (match['name'], *number_variants))
                result = cursor.fetchone()
                if result:
                    return self._format_card_result(result)

            # Number didn't match: prefer a printing of this card from the same set
            if match and set_code:
                result = cursor.execute('''
                    SELECT * FROM cards WHERE name = ? AND set_code = ?
                    ORDER BY CAST(collector_number AS INTEGER) LIMIT 1
                ''', (match['name'], set_code.strip().lower())).fetchone()
                if result:
                    return self._format_card_result(result)

            # Name not found at all (badly misread): trust the printed set + number
            if not match and set_number_row:
                logger.warning(f"No card named '{card_name}' - using {set_code} #{collector_number}: {set_number_row['name']}")
                return self._format_card_result(set_number_row)
            return match

    def _find_by_set_number(self, set_code, number_variants):
        """Row for a set code + collector number (any of the number variants), or None"""
        placeholders = ', '.join('?' * len(number_variants))
        return self.conn.execute(f'''
            SELECT * FROM cards WHERE set_code = ? AND collector_number IN ({placeholders}) LIMIT 1
        ''', (set_code.strip().lower(), *number_variants)).fetchone()

    def get_card_by_set_number(self, set_code, collector_number):
        """Card dict for a set code + collector number, or None"""
        number_variants = collector_number_variants(collector_number)
        if not set_code or not number_variants:
            return None
        with self._lock:
            row = self._find_by_set_number(set_code, number_variants)
            return self._format_card_result(row) if row else None

    def search_card(self, card_name, fuzzy=True):
        """Search for a card by name or flavor name (case- and accent-insensitive)"""
        key = search_key(card_name)
        if not key:
            return None

        with self._lock:
            cursor = self.conn.cursor()

            # Exact name or flavor name
            result = cursor.execute('''
                SELECT * FROM cards WHERE name_search = ? OR flavor_search = ? LIMIT 1
            ''', (key, key)).fetchone()
            if result:
                return self._format_card_result(result)

            # Shortened legendary name ("Thanos" -> "Thanos, the Mad Titan") - fuzzy
            # matching on whole names would prefer unrelated cards like "Thayan Evokers"
            result = cursor.execute('''
                SELECT * FROM cards WHERE name_search LIKE ? OR flavor_search LIKE ? LIMIT 1
            ''', (key + ',%', key + ',%')).fetchone()
            if result:
                logger.info(f"Found card via shortened name: {card_name} -> {result['name']}")
                return self._format_card_result(result)

            if not fuzzy:
                return None

            # Fuzzy match against names sharing the first letters (widen if there are few)
            candidates = {}
            for prefix_length in (3, 2):
                rows = cursor.execute('''
                    SELECT DISTINCT name, flavor_name, name_search, flavor_search FROM cards
                    WHERE name_search LIKE ? OR flavor_search LIKE ?
                ''', (key[:prefix_length] + '%', key[:prefix_length] + '%')).fetchall()
                for row in rows:
                    candidates[row['name_search']] = row['name']
                    if row['flavor_search']:
                        candidates[row['flavor_search']] = row['name']  # flavor name -> card name
                if len(candidates) >= 50:
                    break

            matches = get_close_matches(key, candidates.keys(), n=1, cutoff=0.6)
            if matches:
                lookup_name = candidates[matches[0]]
                result = cursor.execute('SELECT * FROM cards WHERE name = ? LIMIT 1', (lookup_name,)).fetchone()
                if result:
                    logger.info(f"Found card via fuzzy match: {card_name} -> {lookup_name}")
                    return self._format_card_result(result)

            return None

    def search_cards_by_partial_name(self, partial_name, limit=10):
        """Search for cards with partial name match (searches both name and flavor_name)"""
        key = search_key(partial_name) or ''
        with self._lock:
            return self.conn.execute('''
                SELECT name, set_name, price_usd FROM cards
                WHERE name_search LIKE ? OR flavor_search LIKE ?
                ORDER BY name
                LIMIT ?
            ''', (f'%{key}%', f'%{key}%', limit)).fetchall()

    def _format_card_result(self, row):
        """Format database row as card dict"""
        card = {
            'id': row['id'],
            'name': row['name'],
            'flavor_name': row['flavor_name'],
            'set_code': row['set_code'],
            'set': row['set_name'],
            'number': row['collector_number'],
            'rarity': row['rarity'],
            'price': row['price_usd'] or 0.0,
            'price_foil': row['price_usd_foil'] or 0.0,
            'image_uri': row['image_uri'],
            'oracle_text': row['oracle_text'],
            'type_line': row['type_line'],
            'colors': _json_list(row['colors']),
            'mana_cost': row['mana_cost'] or '',
            'border_color': row['border_color'],
            'frame': row['frame'],
            'frame_effects': _json_list(row['frame_effects']),
            'full_art': bool(row['full_art']),
            'promo_types': _json_list(row['promo_types']),
            'finishes': _json_list(row['finishes']),
            'released_at': row['released_at'],
        }
        card['treatments'] = self._treatment_labels(card)
        return card

    @staticmethod
    def _treatment_labels(card):
        """Human-readable treatment labels for a formatted card dict"""
        labels = []
        if card['border_color'] == 'borderless':
            labels.append('Borderless')
        if 'showcase' in card['frame_effects']:
            labels.append('Showcase')
        if 'extendedart' in card['frame_effects']:
            labels.append('Extended Art')
        if card['full_art']:
            labels.append('Full Art')
        if card['frame'] in ('1993', '1997'):
            labels.append('Retro Frame')
        if 'etched' in card['finishes']:
            labels.append('Etched')
        labels.extend(label for promo, label in PROMO_TYPE_LABELS.items() if promo in card['promo_types'])
        return labels

    def get_card_by_id(self, card_id):
        """Get a single printing by its Scryfall id"""
        with self._lock:
            row = self.conn.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
            return self._format_card_result(row) if row else None

    def find_printings(self, card_name, treatment=None, limit=200):
        """
        List all printings of a card, optionally filtered by treatment

        Args:
            card_name: Card name or flavor name (falls back to fuzzy match)
            treatment: Optional key from TREATMENT_FILTERS (e.g. 'borderless')
            limit: Maximum number of printings to return

        Returns:
            tuple: (resolved_name, printings) - resolved_name is None if the card
            was not found at all; printings may be empty if the treatment filter
            excluded every printing
        """
        with self._lock:
            key = search_key(card_name)
            name_condition = "(name_search = ? OR flavor_search = ?)"
            params = (key, key)

            exact = self.conn.execute(f"SELECT name FROM cards WHERE {name_condition} LIMIT 1", params).fetchone()
            if exact:
                resolved_name = exact['name']
            else:
                # Resolve typos/accents to the real card name
                match = self.search_card(card_name, fuzzy=True)
                if not match:
                    return None, []
                resolved_name = match['name']
                name_condition = "LOWER(name) = LOWER(?)"
                params = (resolved_name,)

            treatment_condition = ""
            if treatment:
                if treatment not in TREATMENT_FILTERS:
                    raise ValueError(f"Unknown treatment: {treatment}")
                treatment_condition = f" AND ({TREATMENT_FILTERS[treatment]})"

            rows = self.conn.execute(f'''
                SELECT * FROM cards
                WHERE {name_condition}{treatment_condition}
                ORDER BY released_at DESC, set_name, CAST(collector_number AS INTEGER)
                LIMIT ?
            ''', (*params, limit)).fetchall()

            return resolved_name, [self._format_card_result(row) for row in rows]

    def get_database_stats(self):
        """Get database statistics"""
        cursor = self.conn.cursor()

        # Get total cards
        try:
            cursor.execute('SELECT COUNT(*) FROM cards')
            result = cursor.fetchone()
            total_cards = result[0] if result else 0
        except Exception:
            total_cards = 0

        # Get cards with prices
        try:
            cursor.execute('SELECT COUNT(*) FROM cards WHERE price_usd IS NOT NULL')
            result = cursor.fetchone()
            cards_with_prices = result[0] if result else 0
        except Exception:
            cards_with_prices = 0

        return {
            'total_cards': total_cards,
            'cards_with_prices': cards_with_prices
        }

    def rebuild_database_schema(self, progress_callback=None):
        """
        Rebuild the cards table with the canonical column order and fresh indexes.

        Columns are copied by name, so this works for any older layout (e.g. databases
        where flavor_name or the treatment columns were appended by migration).
        The inventory table is not touched.
        """
        if progress_callback:
            progress_callback("Starting database schema rebuild...")

        logger.info("Beginning database schema rebuild")

        with self._lock:
            try:
                cursor = self.conn.cursor()
                existing_columns = {row['name'] for row in cursor.execute("PRAGMA table_info(cards)").fetchall()}
                shared_columns = ', '.join(c for c in CARD_COLUMN_NAMES if c in existing_columns)

                if progress_callback:
                    progress_callback("Copying card data into optimized table...")

                cursor.execute("DROP TABLE IF EXISTS cards_rebuild")
                self._create_cards_table(cursor, table='cards_rebuild')
                cursor.execute(f"INSERT INTO cards_rebuild ({shared_columns}) SELECT {shared_columns} FROM cards")
                imported = cursor.rowcount

                cursor.execute("DROP TABLE cards")
                cursor.execute("ALTER TABLE cards_rebuild RENAME TO cards")

                if progress_callback:
                    progress_callback("Rebuilding indexes...")
                self._create_card_indexes(cursor)

                inventory_count = cursor.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
                self.conn.commit()

                if progress_callback:
                    progress_callback("Database rebuild complete!")
                logger.info(f"Database schema rebuild completed: {imported} cards")

                return {
                    'success': True,
                    'cards_imported': imported,
                    'inventory_imported': inventory_count,
                    'schema_type': 'current'
                }

            except Exception as e:
                logger.exception(f"Database rebuild failed: {e}")
                self.conn.rollback()
                if progress_callback:
                    progress_callback(f"Rebuild failed: {str(e)}")
                return {
                    'success': False,
                    'error': str(e)
                }

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
