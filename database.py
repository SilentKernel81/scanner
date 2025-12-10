# ============================================================================
# FILE: database.py
# Database management for card data
# ============================================================================
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


class CardDatabase:
    """Manages local card database"""

    def __init__(self, db_file=None):
        self.db_file = db_file or Config.DATABASE_FILE
        self.conn = None
        self._schema_type = None  # Will be 'new' or 'migrated'
        self._lock = threading.RLock()  # Thread-safe database access
        self.initialize_database()
        self._detect_schema()

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

        cursor = self.conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                flavor_name TEXT,
                set_code TEXT,
                set_name TEXT,
                collector_number TEXT,
                rarity TEXT,
                price_usd REAL,
                price_usd_foil REAL,
                image_uri TEXT,
                oracle_text TEXT,
                type_line TEXT,
                colors TEXT,
                mana_cost TEXT
            )
        ''')

        # Add flavor_name column to existing databases (migration)
        try:
            cursor.execute("SELECT flavor_name FROM cards LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("Adding flavor_name column to existing cards table")
            cursor.execute("ALTER TABLE cards ADD COLUMN flavor_name TEXT")
            logger.info("flavor_name column added successfully")

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_card_name
            ON cards(name COLLATE NOCASE)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_card_flavor_name
            ON cards(flavor_name COLLATE NOCASE)
        ''')

        # Composite indexes for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_card_set_number
            ON cards(set_code, collector_number)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_card_rarity
            ON cards(rarity)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_card_type
            ON cards(type_line)
        ''')

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

    def _detect_schema(self):
        """Detect whether we have new schema or migrated schema"""
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(cards)")
        columns = cursor.fetchall()

        # Check the position of flavor_name column
        # Column info format: (cid, name, type, notnull, dflt_value, pk)
        for col in columns:
            if col[1] == 'flavor_name':
                col_position = col[0]
                if col_position == 2:
                    self._schema_type = 'new'
                    logger.info("Detected new database schema (flavor_name at position 2)")
                else:
                    self._schema_type = 'migrated'
                    logger.info(f"Detected migrated database schema (flavor_name at position {col_position})")
                return

        # No flavor_name column found
        self._schema_type = 'old'
        logger.info("Detected old database schema (no flavor_name column)")

    def download_scryfall_data(self, progress_callback=None):
        """Download latest Scryfall bulk data"""
        if progress_callback:
            progress_callback("Fetching Scryfall bulk data information...")
        
        response = requests.get(Config.SCRYFALL_BULK_URL)
        if response.status_code != 200:
            raise Exception("Failed to fetch bulk data info")
        
        bulk_info = response.json()
        download_url = bulk_info['download_uri']
        file_size = bulk_info.get('size', 0) / (1024 * 1024)
        
        if progress_callback:
            progress_callback(f"Downloading card database (~{file_size:.1f} MB)...")
        
        response = requests.get(download_url, stream=True)
        total_size = int(response.headers.get('content-length', 0))
        
        json_buffer = ""
        downloaded = 0
        
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                downloaded += len(chunk)
                json_buffer += chunk.decode('utf-8', errors='ignore')
                
                if total_size > 0 and progress_callback:
                    percent = (downloaded / total_size) * 100
                    progress_callback(f"Download progress: {percent:.1f}%")
        
        if progress_callback:
            progress_callback("Parsing card data...")
        
        return json.loads(json_buffer)
    
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
                
                image_uris = card.get('image_uris', {})
                image_uri = image_uris.get('normal', '')
                
                cursor.execute('''
                    INSERT OR REPLACE INTO cards
                    (id, name, flavor_name, set_code, set_name, collector_number, rarity,
                     price_usd, price_usd_foil, image_uri, oracle_text,
                     type_line, colors, mana_cost)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    card.get('mana_cost')
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
            if collector_number:
                import re
                # Extract just the numeric part from collector number
                number_match = re.search(r'(\d+)', collector_number)
                if number_match:
                    number_str = number_match.group(1)
                    base_number = str(int(number_str))  # Remove leading zeros: "0330" -> "330"

                    # Try exact match with collector number (check both name and flavor_name)
                    cursor.execute('''
                        SELECT * FROM cards
                        WHERE (LOWER(name) = LOWER(?) OR LOWER(flavor_name) = LOWER(?))
                        AND (collector_number = ? OR collector_number = ? OR collector_number = ? OR collector_number = ?)
                        LIMIT 1
                    ''', (card_name, card_name, base_number, number_str, base_number + 's', number_str + 's'))

                    result = cursor.fetchone()
                    if result:
                        logger.info(f"Found exact match with collector number: {result[1]} #{result[5]}")
                        return self._format_card_result(result)

            # Step 2: Try exact case-insensitive match without collector number (uses index, check both name and flavor_name)
            cursor.execute('''
                SELECT * FROM cards
                WHERE LOWER(name) = LOWER(?) OR LOWER(flavor_name) = LOWER(?)
                LIMIT 1
            ''', (card_name, card_name))

            result = cursor.fetchone()
            if result:
                logger.info(f"Found exact case-insensitive match: {result[1]}")
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
                db_name = result[1]  # name is column 1
                db_flavor_name = result[2]  # flavor_name is column 2
                if normalize_text(db_name).lower() == normalized_search_name:
                    matching_cards.append(result)
                elif db_flavor_name and normalize_text(db_flavor_name).lower() == normalized_search_name:
                    matching_cards.append(result)

            if matching_cards:
                logger.info(f"Found {len(matching_cards)} cards via accent-insensitive match")

                # If collector number provided, try to find matching card
                if collector_number:
                    import re
                    number_match = re.search(r'(\d+)', collector_number)
                    if number_match:
                        number_str = number_match.group(1)
                        base_number = str(int(number_str))

                        for card in matching_cards:
                            card_number = card[5]  # collector_number is column 5 (was 4 before flavor_name added)
                            if card_number == base_number or card_number == number_str:
                                logger.info(f"Found match with collector number: {card[1]} #{card_number}")
                                return self._format_card_result(card)
                            # Also check with 's' suffix for showcase variants
                            if card_number == base_number + 's' or card_number == number_str + 's':
                                logger.info(f"Found showcase variant: {card[1]} #{card_number}")
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
            db_name = result[1]  # name is column 1
            db_flavor_name = result[2]  # flavor_name is column 2
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
        import json

        # Use detected schema type to map columns correctly
        if self._schema_type == 'new':
            # New schema: id, name, flavor_name, set_code, set_name, collector_number, ...
            colors = []
            if row[12]:
                try:
                    colors = json.loads(row[12])
                except:
                    colors = []

            return {
                'id': row[0],
                'name': row[1],
                'flavor_name': row[2],
                'set_code': row[3],
                'set': row[4],
                'number': row[5],
                'rarity': row[6],
                'price': row[7] or 0.0,
                'price_foil': row[8] or 0.0,
                'image_uri': row[9],
                'oracle_text': row[10],
                'type_line': row[11],
                'colors': colors,
                'mana_cost': row[13] or ''
            }
        else:
            # Migrated or old schema: id, name, set_code, set_name, collector_number, ..., flavor_name (at end)
            colors = []
            if row[11]:
                try:
                    colors = json.loads(row[11])
                except:
                    colors = []

            # For migrated databases, flavor_name is at position 13 (end)
            flavor_name = row[13] if len(row) == 14 else None

            return {
                'id': row[0],
                'name': row[1],
                'flavor_name': flavor_name,
                'set_code': row[2],
                'set': row[3],
                'number': row[4],
                'rarity': row[5],
                'price': row[6] or 0.0,
                'price_foil': row[7] or 0.0,
                'image_uri': row[8],
                'oracle_text': row[9],
                'type_line': row[10],
                'colors': colors,
                'mana_cost': row[12] or ''
            }
    
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
        Rebuild database with optimized schema (flavor_name at position 2).

        This function:
        1. Exports all existing card data
        2. Drops and recreates tables with proper column order
        3. Re-imports all data
        4. Rebuilds all indexes

        Benefits:
        - Eliminates schema detection overhead
        - Cleaner column ordering
        - Faster queries with proper indexes
        """
        if progress_callback:
            progress_callback("Starting database schema rebuild...")

        logger.info("Beginning database schema rebuild")

        try:
            cursor = self.conn.cursor()

            # Step 1: Export all existing card data
            if progress_callback:
                progress_callback("Exporting existing card data...")

            cursor.execute("SELECT * FROM cards")
            existing_cards = cursor.fetchall()

            # Get column names to map old schema to new schema
            cursor.execute("PRAGMA table_info(cards)")
            old_columns = {col[1]: col[0] for col in cursor.fetchall()}

            logger.info(f"Exported {len(existing_cards)} cards from existing database")

            # Step 2: Export inventory data
            if progress_callback:
                progress_callback("Exporting inventory data...")

            cursor.execute("SELECT * FROM inventory")
            existing_inventory = cursor.fetchall()
            logger.info(f"Exported {len(existing_inventory)} inventory items")

            # Step 3: Drop existing tables
            if progress_callback:
                progress_callback("Dropping old tables...")

            cursor.execute("DROP TABLE IF EXISTS cards")
            cursor.execute("DROP TABLE IF EXISTS inventory")
            logger.info("Dropped existing tables")

            # Step 4: Recreate tables with proper schema
            if progress_callback:
                progress_callback("Creating new optimized schema...")

            # Create cards table with proper column order
            cursor.execute('''
                CREATE TABLE cards (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    flavor_name TEXT,
                    set_code TEXT,
                    set_name TEXT,
                    collector_number TEXT,
                    rarity TEXT,
                    price_usd REAL,
                    price_usd_foil REAL,
                    image_uri TEXT,
                    oracle_text TEXT,
                    type_line TEXT,
                    colors TEXT,
                    mana_cost TEXT
                )
            ''')

            # Create all indexes
            cursor.execute('''
                CREATE INDEX idx_card_name
                ON cards(name COLLATE NOCASE)
            ''')

            cursor.execute('''
                CREATE INDEX idx_card_flavor_name
                ON cards(flavor_name COLLATE NOCASE)
            ''')

            cursor.execute('''
                CREATE INDEX idx_card_set_number
                ON cards(set_code, collector_number)
            ''')

            cursor.execute('''
                CREATE INDEX idx_card_rarity
                ON cards(rarity)
            ''')

            cursor.execute('''
                CREATE INDEX idx_card_type
                ON cards(type_line)
            ''')

            # Recreate inventory table
            cursor.execute('''
                CREATE TABLE inventory (
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
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(card_name, set_name, card_number, condition, foil, surge)
                )
            ''')

            cursor.execute('''
                CREATE INDEX idx_inventory_card_name
                ON inventory(card_name COLLATE NOCASE)
            ''')

            cursor.execute('''
                CREATE INDEX idx_inventory_set
                ON inventory(set_name)
            ''')

            logger.info("Created new optimized schema")

            # Step 5: Re-import card data with proper column mapping
            if progress_callback:
                progress_callback("Re-importing card data...")

            # Map old column positions to new schema
            # Old migrated schema: id, name, set_code, set_name, collector_number, ..., flavor_name (at end)
            # New schema: id, name, flavor_name, set_code, set_name, collector_number, ...

            imported = 0
            for old_row in existing_cards:
                try:
                    # Extract data based on old schema positions
                    if len(old_row) == 14:  # Migrated schema
                        new_row = (
                            old_row[0],   # id
                            old_row[1],   # name
                            old_row[13],  # flavor_name (was at end)
                            old_row[2],   # set_code
                            old_row[3],   # set_name
                            old_row[4],   # collector_number
                            old_row[5],   # rarity
                            old_row[6],   # price_usd
                            old_row[7],   # price_usd_foil
                            old_row[8],   # image_uri
                            old_row[9],   # oracle_text
                            old_row[10],  # type_line
                            old_row[11],  # colors
                            old_row[12],  # mana_cost
                        )
                    else:  # Old schema without flavor_name
                        new_row = (
                            old_row[0],   # id
                            old_row[1],   # name
                            None,         # flavor_name
                            old_row[2],   # set_code
                            old_row[3],   # set_name
                            old_row[4],   # collector_number
                            old_row[5],   # rarity
                            old_row[6],   # price_usd
                            old_row[7],   # price_usd_foil
                            old_row[8],   # image_uri
                            old_row[9],   # oracle_text
                            old_row[10],  # type_line
                            old_row[11],  # colors
                            old_row[12],  # mana_cost
                        )

                    cursor.execute('''
                        INSERT INTO cards
                        (id, name, flavor_name, set_code, set_name, collector_number, rarity,
                         price_usd, price_usd_foil, image_uri, oracle_text,
                         type_line, colors, mana_cost)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', new_row)

                    imported += 1
                    if imported % 1000 == 0 and progress_callback:
                        progress_callback(f"Imported {imported} cards...")

                except Exception as e:
                    logger.error(f"Error importing card: {e}")
                    continue

            logger.info(f"Re-imported {imported} cards")

            # Step 6: Re-import inventory data
            if progress_callback:
                progress_callback("Re-importing inventory...")

            for inv_row in existing_inventory:
                try:
                    cursor.execute('''
                        INSERT INTO inventory
                        (card_name, set_name, card_number, rarity, type_line, mana_cost,
                         colors, color_identity, price_usd, quantity, condition, foil, surge, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', inv_row[1:])  # Skip id (auto-increment)
                except Exception as e:
                    logger.error(f"Error importing inventory item: {e}")
                    continue

            logger.info(f"Re-imported {len(existing_inventory)} inventory items")

            # Step 7: Commit all changes
            self.conn.commit()

            # Step 8: Re-detect schema (should now be 'new')
            self._detect_schema()

            if progress_callback:
                progress_callback("Database rebuild complete!")

            logger.info("Database schema rebuild completed successfully")
            logger.info(f"New schema type: {self._schema_type}")

            return {
                'success': True,
                'cards_imported': imported,
                'inventory_imported': len(existing_inventory),
                'schema_type': self._schema_type
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
