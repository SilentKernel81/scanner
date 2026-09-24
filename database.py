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
from difflib import get_close_matches
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
                    card.get('released_at')
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
    
    def search_card_exact(self, card_name, collector_number=None):
        """
        Search for an exact card by name and optionally collector number

        Strategy:
        1. Try exact case-insensitive match with SQL (uses index)
        2. If collector number provided, filter by it
        3. Fall back to prefix search for accent-insensitive matching
        4. Fall back to fuzzy search as last resort

        Args:
            card_name: The card name
            collector_number: Optional collector number (e.g., "123", "0330", "123s")

        Returns:
            Card dict if found, None otherwise
        """
        with self._lock:
            logger.info(f"Searching for card: '{card_name}'" + (f" #{collector_number}" if collector_number else ""))
            cursor = self.conn.cursor()

            # Step 1: Try exact case-insensitive match with collector number (fastest, uses index)
            number_variants = collector_number_variants(collector_number)
            if number_variants:
                placeholders = ', '.join('?' * len(number_variants))
                cursor.execute(f'''
                    SELECT * FROM cards
                    WHERE (LOWER(name) = LOWER(?) OR LOWER(flavor_name) = LOWER(?))
                    AND collector_number IN ({placeholders})
                    LIMIT 1
                ''', (card_name, card_name, *number_variants))

                result = cursor.fetchone()
                if result:
                    logger.info(f"Found exact match with collector number: {result['name']} #{result['collector_number']}")
                    return self._format_card_result(result)

            # Step 2: Try exact case-insensitive match without collector number (uses index, check both name and flavor_name)
            cursor.execute('''
                SELECT * FROM cards
                WHERE LOWER(name) = LOWER(?) OR LOWER(flavor_name) = LOWER(?)
                LIMIT 1
            ''', (card_name, card_name))

            result = cursor.fetchone()
            if result:
                logger.info(f"Found exact case-insensitive match: {result['name']}")
                return self._format_card_result(result)

            # Step 3: Try accent-insensitive match with prefix search (optimized, not full table scan)
            normalized_search_name = normalize_text(card_name).lower()

            # Use first 3 characters as prefix to limit search space
            prefix = card_name[:3].lower() if len(card_name) >= 3 else card_name.lower()

            cursor.execute('''
                SELECT * FROM cards
                WHERE LOWER(name) LIKE ? OR LOWER(flavor_name) LIKE ?
                LIMIT 200
            ''', (prefix + '%', prefix + '%'))

            # Filter results in Python (but only limited rows)
            matching_cards = []
            for result in cursor.fetchall():
                db_name = result['name']
                db_flavor_name = result['flavor_name']
                if normalize_text(db_name).lower() == normalized_search_name:
                    matching_cards.append(result)
                elif db_flavor_name and normalize_text(db_flavor_name).lower() == normalized_search_name:
                    matching_cards.append(result)

            if matching_cards:
                logger.info(f"Found {len(matching_cards)} cards via accent-insensitive match")

                # If collector number provided, try to find matching card
                for card in matching_cards:
                    if card['collector_number'] in number_variants:
                        logger.info(f"Found match with collector number: {card['name']} #{card['collector_number']}")
                        return self._format_card_result(card)

                # Return first match if no collector number or no match found
                return self._format_card_result(matching_cards[0])

            # Step 4: Fallback to fuzzy search
            logger.info(f"No exact match found for '{card_name}', trying fuzzy search")
            return self.search_card(card_name, fuzzy=True)

    def search_card(self, card_name, fuzzy=True):
        """Search for a card by name or flavor name"""
        with self._lock:
            cursor = self.conn.cursor()

            # Try exact case-insensitive match first (uses index, check both name and flavor_name)
            cursor.execute('''
                SELECT * FROM cards
                WHERE LOWER(name) = LOWER(?) OR LOWER(flavor_name) = LOWER(?)
                LIMIT 1
            ''', (card_name, card_name))

            result = cursor.fetchone()

            if result:
                return self._format_card_result(result)

            # Try accent-insensitive match with prefix search (optimized)
            normalized_search_name = normalize_text(card_name).lower()

            # Use first 3 characters as prefix to limit search space
            prefix = card_name[:3].lower() if len(card_name) >= 3 else card_name.lower()

            cursor.execute('''
                SELECT * FROM cards
                WHERE LOWER(name) LIKE ? OR LOWER(flavor_name) LIKE ?
                LIMIT 200
            ''', (prefix + '%', prefix + '%'))

            # Filter results in Python (but only limited rows)
            for result in cursor.fetchall():
                db_name = result['name']
                db_flavor_name = result['flavor_name']
                if normalize_text(db_name).lower() == normalized_search_name:
                    logger.info(f"Found card via accent-insensitive name match: {db_name}")
                    return self._format_card_result(result)
                elif db_flavor_name and normalize_text(db_flavor_name).lower() == normalized_search_name:
                    logger.info(f"Found card via accent-insensitive flavor name match: {db_flavor_name} (Oracle: {db_name})")
                    return self._format_card_result(result)

            # Fuzzy matching fallback (optimized to use prefix search)
            if fuzzy:
                # Get limited set of names with prefix match (check both name and flavor_name)
                cursor.execute('''
                    SELECT DISTINCT name, flavor_name FROM cards
                    WHERE LOWER(name) LIKE ? OR LOWER(flavor_name) LIKE ?
                    LIMIT 500
                ''', (prefix + '%', prefix + '%'))

                # Build candidate names from both name and flavor_name
                candidate_names = []
                name_to_card = {}  # Map normalized name to original card name for lookup
                for row in cursor.fetchall():
                    card_name = row[0]
                    flavor_name = row[1]
                    candidate_names.append(card_name)
                    name_to_card[card_name] = card_name
                    if flavor_name:
                        candidate_names.append(flavor_name)
                        name_to_card[flavor_name] = card_name  # Map flavor name to card name for lookup

                # If prefix search returns too few results, expand search
                if len(candidate_names) < 50:
                    cursor.execute('SELECT DISTINCT name, flavor_name FROM cards LIMIT 1000')
                    for row in cursor.fetchall():
                        card_name = row[0]
                        flavor_name = row[1]
                        if card_name not in candidate_names:
                            candidate_names.append(card_name)
                            name_to_card[card_name] = card_name
                        if flavor_name and flavor_name not in candidate_names:
                            candidate_names.append(flavor_name)
                            name_to_card[flavor_name] = card_name

                # Try fuzzy matching on normalized names
                normalized_names = {normalize_text(name).lower(): name for name in candidate_names}
                matches = get_close_matches(normalized_search_name, normalized_names.keys(), n=1, cutoff=0.6)

                if matches:
                    original_name = normalized_names[matches[0]]
                    lookup_name = name_to_card.get(original_name, original_name)
                    cursor.execute('''
                        SELECT * FROM cards
                        WHERE name = ?
                        LIMIT 1
                    ''', (lookup_name,))

                    result = cursor.fetchone()
                    if result:
                        logger.info(f"Found card via fuzzy match: {original_name} -> {lookup_name}")
                        return self._format_card_result(result)

            return None
    
    def search_cards_by_partial_name(self, partial_name, limit=10):
        """Search for cards with partial name match (searches both name and flavor_name)"""
        cursor = self.conn.cursor()

        cursor.execute('''
            SELECT name, set_name, price_usd FROM cards
            WHERE LOWER(name) LIKE LOWER(?) OR LOWER(flavor_name) LIKE LOWER(?)
            ORDER BY name
            LIMIT ?
        ''', (f'%{partial_name}%', f'%{partial_name}%', limit))

        return cursor.fetchall()
    
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
            name_condition = "(LOWER(name) = LOWER(?) OR LOWER(flavor_name) = LOWER(?))"
            params = (card_name, card_name)

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
