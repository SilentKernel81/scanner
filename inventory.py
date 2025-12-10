# ============================================================================
# FILE: inventory.py
# Inventory management using SQLite database
# ============================================================================
import sqlite3
import csv
import logging
import threading
from datetime import datetime
from pathlib import Path
from config import Config
from utils import normalize_text

# Create database logger
logger = logging.getLogger('database')


class InventoryManager:
    """Manages card inventory using SQLite database"""

    def __init__(self, db_file=None, log_callback=None):
        self.db_file = db_file or Config.DATABASE_FILE
        self.log_callback = log_callback
        self.conn = None
        self._lock = threading.RLock()  # Thread-safe inventory access
        self.initialize_connection()

    def log(self, message, level="info"):
        """Send log message to both file logger and UI callback"""
        # Log to file
        log_method = getattr(logger, level, logger.info)
        log_method(message)

        # Send to UI callback if provided
        if self.log_callback:
            self.log_callback(message, level)

    def initialize_connection(self):
        """Initialize database connection"""
        self.conn = sqlite3.connect(
            str(self.db_file),
            check_same_thread=False,
            isolation_level='DEFERRED',  # Consistent with database.py
            timeout=10.0  # Add timeout for lock waits
        )
        # Enable WAL mode for better concurrency
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.row_factory = sqlite3.Row  # Enable dict-like access

    def add_card(self, card_info, image_path=None, condition='Near Mint', is_foil=False, is_surge=False, quantity=1):
        """
        Add a card to the inventory with quantity.
        If card already exists (same name, set, number, condition, foil, surge), increment quantity.

        Args:
            card_info: Dict with card details (name, set, number, etc.)
            image_path: Path to the scanned image (not stored in inventory, kept for compatibility)
            condition: Card condition (default: 'Near Mint')
            is_foil: Whether the card is foil (default: False)
            is_surge: Whether the card is surge foil (default: False)
            quantity: Number of cards to add (default: 1)
        """
        # Validate and normalize card_info fields to prevent None values
        card_name = str(card_info.get('name', '')) if card_info.get('name') is not None else ''
        set_name = str(card_info.get('set', '')) if card_info.get('set') is not None else ''
        card_number = str(card_info.get('number', '')) if card_info.get('number') is not None else ''
        rarity = str(card_info.get('rarity', '')) if card_info.get('rarity') is not None else ''
        type_line = str(card_info.get('type_line', '')) if card_info.get('type_line') is not None else ''
        mana_cost = str(card_info.get('mana_cost', '')) if card_info.get('mana_cost') is not None else ''

        # Validate price fields
        price_foil = card_info.get('price_foil', 0)
        price_normal = card_info.get('price', 0)
        price = float(price_foil if is_foil else price_normal) if (price_foil if is_foil else price_normal) is not None else 0.0

        # Get colors and format them
        colors = card_info.get('colors', [])
        if colors is None:
            colors = []
        color_identity = self._get_color_identity(colors)
        colors_str = ', '.join(colors) if colors else 'Colorless'

        # Validate quantity
        quantity = int(quantity) if quantity is not None else 1

        # Use atomic INSERT ... ON CONFLICT to prevent race conditions
        # This eliminates the need for check-then-update pattern
        with self._lock:
            cursor = self.conn.cursor()

            # Atomic insert-or-update operation
            cursor.execute('''
                INSERT INTO inventory (
                    card_name, set_name, card_number, rarity, type_line,
                    mana_cost, colors, color_identity, price_usd, quantity,
                    condition, foil, surge, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_name, set_name, card_number, condition, foil, surge)
                DO UPDATE SET
                    quantity = quantity + excluded.quantity,
                    timestamp = excluded.timestamp,
                    price_usd = excluded.price_usd
            ''', (
                card_name,
                set_name,
                card_number,
                rarity,
                type_line,
                mana_cost,
                colors_str,
                color_identity,
                price,
                quantity,
                condition,
                1 if is_foil else 0,
                1 if is_surge else 0,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))

            self.conn.commit()

            # Get the final quantity to display
            cursor.execute('''
                SELECT quantity FROM inventory
                WHERE card_name = ? AND set_name = ? AND card_number = ?
                  AND condition = ? AND foil = ? AND surge = ?
            ''', (card_name, set_name, card_number, condition, 1 if is_foil else 0, 1 if is_surge else 0))

            row = cursor.fetchone()
            final_quantity = row[0] if row else quantity
            total_value = price * final_quantity
            self.log(f"Added to inventory: {quantity}x {card_name} ({color_identity}) - ${total_value:.2f}", level="success")

    def _find_existing_card(self, card_name, set_name, card_number, condition, is_foil, is_surge=False):
        """
        Find existing card in inventory using accent-insensitive matching

        Strategy:
        1. Find all cards matching the name (accent-insensitive)
        2. Filter by collector number, condition, foil status, and surge status
        3. Return best match

        Returns:
            Dict with card data if found, None otherwise
        """
        cursor = self.conn.cursor()

        # Step 1: Find all cards with matching name (accent-insensitive)
        # Optimization: Use SQL WHERE to reduce result set first, then normalize accents
        normalized_search_name = normalize_text(card_name).lower()

        # Use case-insensitive SQL filter first (catches 99% of cases)
        cursor.execute('''
            SELECT * FROM inventory
            WHERE LOWER(card_name) = LOWER(?)
        ''', (card_name,))

        # Then do accent normalization on the smaller result set
        matching_cards = []
        for row in cursor.fetchall():
            db_name = row['card_name']
            if normalize_text(db_name).lower() == normalized_search_name:
                matching_cards.append(row)

        if not matching_cards:
            return None

        # Step 2: Filter by collector number, condition, foil, and surge
        for row in matching_cards:
            # Get surge value (default to 0 for backwards compatibility)
            try:
                row_surge = row['surge']
            except (KeyError, IndexError):
                row_surge = 0

            # Check if all criteria match
            if (row['set_name'] == set_name and
                row['card_number'] == card_number and
                row['condition'] == condition and
                row['foil'] == (1 if is_foil else 0) and
                row_surge == (1 if is_surge else 0)):
                logger.info(f"Found existing inventory entry: {row['card_name']} #{row['card_number']} (qty: {row['quantity']})")
                return dict(row)

        # No exact match found
        return None

    def _get_color_identity(self, colors):
        """
        Get color identity string

        Args:
            colors: List of color codes (e.g., ['W', 'U', 'B', 'R', 'G'])

        Returns:
            String representing color identity
        """
        if not colors or len(colors) == 0:
            return 'Colorless'
        elif len(colors) == 1:
            color_names = {
                'W': 'White',
                'U': 'Blue',
                'B': 'Black',
                'R': 'Red',
                'G': 'Green'
            }
            return color_names.get(colors[0], colors[0])
        else:
            return 'Multicolor'

    def get_all_cards(self):
        """
        Get all cards from inventory

        Returns:
            List of dicts with card data (CSV-compatible format)
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT * FROM inventory
                ORDER BY timestamp DESC
            ''')

            cards = []
            for row in cursor.fetchall():
                # Get surge value (default to 0 for backwards compatibility)
                try:
                    surge = row['surge']
                except (KeyError, IndexError):
                    surge = 0

                cards.append({
                    'Card Name': row['card_name'],
                    'Set': row['set_name'],
                    'Card Number': row['card_number'],
                    'Rarity': row['rarity'],
                    'Type': row['type_line'],
                    'Mana Cost': row['mana_cost'],
                    'Colors': row['colors'],
                    'Color Identity': row['color_identity'],
                    'Price (USD)': f"${row['price_usd']:.2f}",
                    'Quantity': row['quantity'],
                    'Condition': row['condition'],
                    'Foil': 'Yes' if row['foil'] else 'No',
                    'Surge': 'Yes' if surge else 'No',
                    'Timestamp': row['timestamp']
                })

            return cards

    def get_summary(self):
        """Get inventory statistics"""
        try:
            cursor = self.conn.cursor()

            # Get totals
            cursor.execute('''
                SELECT
                    SUM(quantity) as total_cards,
                    COUNT(*) as unique_cards,
                    SUM(price_usd * quantity) as total_value
                FROM inventory
            ''')

            row = cursor.fetchone()
            total_cards = row['total_cards'] or 0
            unique_cards = row['unique_cards'] or 0
            total_value = row['total_value'] or 0.0

            # Get recent cards
            cursor.execute('''
                SELECT * FROM inventory
                ORDER BY timestamp DESC
                LIMIT 5
            ''')

            recent_cards = []
            for row in cursor.fetchall():
                # Get surge value (default to 0 for backwards compatibility)
                try:
                    surge = row['surge']
                except (KeyError, IndexError):
                    surge = 0

                recent_cards.append({
                    'Card Name': row['card_name'],
                    'Set': row['set_name'],
                    'Card Number': row['card_number'],
                    'Rarity': row['rarity'],
                    'Type': row['type_line'],
                    'Mana Cost': row['mana_cost'],
                    'Colors': row['colors'],
                    'Color Identity': row['color_identity'],
                    'Price (USD)': f"${row['price_usd']:.2f}",
                    'Quantity': row['quantity'],
                    'Condition': row['condition'],
                    'Foil': 'Yes' if row['foil'] else 'No',
                    'Surge': 'Yes' if surge else 'No',
                    'Timestamp': row['timestamp']
                })

            return {
                'total_cards': total_cards,
                'unique_cards': unique_cards,
                'total_value': total_value,
                'recent_cards': recent_cards
            }
        except Exception as e:
            self.log(f"Error reading inventory: {e}", level="error")
            return {
                'total_cards': 0,
                'unique_cards': 0,
                'total_value': 0.0,
                'recent_cards': []
            }

    def export_csv(self, output_path=None):
        """
        Export inventory to CSV file

        Args:
            output_path: Optional custom output path. If None, uses default location.

        Returns:
            Path to exported file
        """
        if output_path is None:
            # Use default: data/card_inventory_export_TIMESTAMP.csv
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = Config.DATA_DIR / f'card_inventory_export_{timestamp}.csv'
        else:
            output_path = Path(output_path)

        try:
            cards = self.get_all_cards()

            with open(output_path, 'w', newline='') as f:
                if cards:
                    writer = csv.DictWriter(f, fieldnames=cards[0].keys())
                    writer.writeheader()
                    writer.writerows(cards)
                else:
                    # Write empty file with headers
                    writer = csv.writer(f)
                    writer.writerow([
                        'Card Name', 'Set', 'Card Number', 'Rarity', 'Type',
                        'Mana Cost', 'Colors', 'Color Identity',
                        'Price (USD)', 'Quantity', 'Condition', 'Foil', 'Surge', 'Timestamp'
                    ])

            self.log(f"Inventory exported to: {output_path}", level="success")
            return output_path

        except Exception as e:
            self.log(f"Export failed: {e}", level="error")
            return None

    def export_moxfield_csv(self, output_path=None):
        """
        Export inventory to Moxfield-compatible CSV format

        Moxfield format:
        Count,Name,Edition,Condition,Language,Foil,Collector Number,Purchase Price,Tag

        Args:
            output_path: Optional custom output path. If None, uses default location.

        Returns:
            Path to exported file
        """
        if output_path is None:
            # Use default: data/moxfield_export_TIMESTAMP.csv
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = Config.DATA_DIR / f'moxfield_export_{timestamp}.csv'
        else:
            output_path = Path(output_path)

        try:
            cards = self.get_all_cards()

            # Write Moxfield format
            with open(output_path, 'w', newline='') as f:
                writer = csv.writer(f)

                # Moxfield header
                writer.writerow([
                    'Count', 'Name', 'Edition', 'Condition', 'Language',
                    'Foil', 'Collector Number', 'Purchase Price', 'Tag'
                ])

                # Convert each card to Moxfield format
                for card in cards:
                    count = card.get('Quantity', '1')
                    name = card.get('Card Name', '')
                    edition = card.get('Set', '')
                    condition = card.get('Condition', 'Near Mint')
                    language = 'English'  # Default to English

                    # Determine foil type: surge takes precedence over regular foil
                    if card.get('Surge', 'No') == 'Yes':
                        foil = 'surge'
                    elif card.get('Foil', 'No') == 'Yes':
                        foil = 'foil'
                    else:
                        foil = ''

                    collector_number = card.get('Card Number', '')
                    purchase_price = '0'  # Set to 0 as requested
                    tag = ''  # Empty tag by default

                    writer.writerow([
                        count, name, edition, condition, language,
                        foil, collector_number, purchase_price, tag
                    ])

            self.log(f"Moxfield inventory exported to: {output_path}", level="success")
            return output_path

        except Exception as e:
            self.log(f"Moxfield export failed: {e}", level="error")
            return None

    def import_csv(self, csv_file_path, replace_existing=False):
        """
        Import inventory from CSV file

        Args:
            csv_file_path: Path to CSV file to import
            replace_existing: If True, clear existing inventory before import (default: False)

        Returns:
            Dict with import statistics (added, updated, skipped, errors)
        """
        csv_file_path = Path(csv_file_path)

        if not csv_file_path.exists():
            self.log(f"Import failed: File not found: {csv_file_path}", level="error")
            return {
                'success': False,
                'error': 'File not found',
                'added': 0,
                'updated': 0,
                'skipped': 0,
                'errors': 0
            }

        stats = {
            'success': True,
            'added': 0,
            'updated': 0,
            'skipped': 0,
            'errors': 0
        }

        try:
            # Clear existing inventory if requested
            if replace_existing:
                cursor = self.conn.cursor()
                cursor.execute('DELETE FROM inventory')
                self.conn.commit()
                self.log("Cleared existing inventory for replacement", level="info")

            # Read CSV file
            with open(csv_file_path, 'r', newline='') as f:
                reader = csv.DictReader(f)

                for row_num, row in enumerate(reader, start=2):  # Start at 2 (1 is header)
                    try:
                        # Parse CSV row
                        card_name = row.get('Card Name', '').strip()
                        set_name = row.get('Set', '').strip()
                        card_number = row.get('Card Number', '').strip()

                        if not card_name or not set_name:
                            self.log(f"Row {row_num}: Skipping - missing name or set", level="warning")
                            stats['skipped'] += 1
                            continue

                        # Parse other fields
                        rarity = row.get('Rarity', '').strip()
                        type_line = row.get('Type', '').strip()
                        mana_cost = row.get('Mana Cost', '').strip()
                        colors = row.get('Colors', '').strip()
                        color_identity = row.get('Color Identity', 'Colorless').strip()

                        # Parse price (remove $ sign if present)
                        price_str = row.get('Price (USD)', '0').strip()
                        price_str = price_str.replace('$', '').replace(',', '')
                        try:
                            price = float(price_str)
                        except ValueError:
                            price = 0.0

                        # Parse quantity
                        try:
                            quantity = int(row.get('Quantity', '1'))
                            if quantity < 1:
                                quantity = 1
                        except ValueError:
                            quantity = 1

                        # Parse condition
                        condition = row.get('Condition', 'Near Mint').strip()

                        # Parse foil status
                        foil_str = row.get('Foil', 'No').strip().lower()
                        is_foil = foil_str in ['yes', 'true', '1']

                        # Parse surge status (optional field for backwards compatibility)
                        surge_str = row.get('Surge', 'No').strip().lower()
                        is_surge = surge_str in ['yes', 'true', '1']

                        # Check if card already exists
                        existing = self._find_existing_card(
                            card_name, set_name, card_number,
                            condition, is_foil, is_surge
                        )

                        cursor = self.conn.cursor()

                        if existing and not replace_existing:
                            # Update existing entry - add quantities together
                            new_quantity = existing['quantity'] + quantity
                            cursor.execute('''
                                UPDATE inventory
                                SET quantity = ?,
                                    timestamp = ?
                                WHERE id = ?
                            ''', (new_quantity, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), existing['id']))
                            self.conn.commit()
                            stats['updated'] += 1

                        else:
                            # Insert new entry
                            cursor.execute('''
                                INSERT INTO inventory (
                                    card_name, set_name, card_number, rarity, type_line,
                                    mana_cost, colors, color_identity, price_usd, quantity,
                                    condition, foil, surge, timestamp
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                card_name, set_name, card_number, rarity, type_line,
                                mana_cost, colors, color_identity, price, quantity,
                                condition, 1 if is_foil else 0, 1 if is_surge else 0,
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            ))
                            self.conn.commit()
                            stats['added'] += 1

                    except Exception as e:
                        self.log(f"Row {row_num}: Error importing card - {e}", level="error")
                        stats['errors'] += 1
                        continue

            # Log summary
            total_processed = stats['added'] + stats['updated']
            self.log(
                f"Import complete: {total_processed} cards imported "
                f"({stats['added']} added, {stats['updated']} updated, "
                f"{stats['skipped']} skipped, {stats['errors']} errors)",
                level="success"
            )

            return stats

        except Exception as e:
            self.log(f"Import failed: {e}", level="error")
            return {
                'success': False,
                'error': str(e),
                'added': 0,
                'updated': 0,
                'skipped': 0,
                'errors': 0
            }

    def get_color_breakdown(self):
        """Get breakdown of cards by color identity"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT color_identity, SUM(quantity) as count
                FROM inventory
                GROUP BY color_identity
            ''')

            color_counts = {}
            for row in cursor.fetchall():
                color_counts[row['color_identity']] = row['count']

            return color_counts

        except Exception as e:
            self.log(f"Error analyzing colors: {e}", level="error")
            return {}

    def get_detailed_stats(self):
        """Get detailed inventory statistics including color breakdown"""
        try:
            cursor = self.conn.cursor()

            # Get totals
            cursor.execute('''
                SELECT
                    SUM(quantity) as total_cards,
                    COUNT(*) as unique_cards,
                    SUM(price_usd * quantity) as total_value
                FROM inventory
            ''')

            row = cursor.fetchone()
            total_cards = row['total_cards'] or 0
            unique_cards = row['unique_cards'] or 0
            total_value = row['total_value'] or 0.0

            # Color breakdown
            cursor.execute('''
                SELECT color_identity, SUM(quantity) as count
                FROM inventory
                GROUP BY color_identity
            ''')
            color_breakdown = {}
            for row in cursor.fetchall():
                color_breakdown[row['color_identity']] = row['count']

            # Rarity breakdown
            cursor.execute('''
                SELECT rarity, SUM(quantity) as count
                FROM inventory
                GROUP BY rarity
            ''')
            rarity_breakdown = {}
            for row in cursor.fetchall():
                rarity_breakdown[row['rarity'] or 'Unknown'] = row['count']

            # Foil count
            cursor.execute('''
                SELECT SUM(quantity) as count
                FROM inventory
                WHERE foil = 1
            ''')
            foil_count = cursor.fetchone()['count'] or 0

            non_foil_count = total_cards - foil_count

            return {
                'total_cards': total_cards,
                'unique_cards': unique_cards,
                'total_value': total_value,
                'color_breakdown': color_breakdown,
                'rarity_breakdown': rarity_breakdown,
                'foil_count': foil_count,
                'non_foil_count': non_foil_count
            }

        except Exception as e:
            self.log(f"Error generating stats: {e}", level="error")
            return {
                'total_cards': 0,
                'unique_cards': 0,
                'total_value': 0.0,
                'color_breakdown': {},
                'rarity_breakdown': {},
                'foil_count': 0,
                'non_foil_count': 0
            }

    def delete_card(self, index):
        """
        Delete a card from inventory by index

        Args:
            index: Row index (0-based, from get_all_cards())

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            cards = self.get_all_cards()

            if index < 0 or index >= len(cards):
                self.log(f"Invalid index: {index}", level="error")
                return False

            # Get the card at this index
            card = cards[index]
            card_name = card['Card Name']

            # Find and delete in database
            cursor = self.conn.cursor()
            cursor.execute('''
                DELETE FROM inventory
                WHERE card_name = ?
                  AND set_name = ?
                  AND card_number = ?
                  AND condition = ?
                  AND foil = ?
                  AND surge = ?
                LIMIT 1
            ''', (
                card['Card Name'],
                card['Set'],
                card['Card Number'],
                card['Condition'],
                1 if card['Foil'] == 'Yes' else 0,
                1 if card.get('Surge', 'No') == 'Yes' else 0
            ))

            self.conn.commit()

            self.log(f"Deleted from inventory: {card_name}", level="success")
            return True

        except Exception as e:
            self.log(f"Error deleting card: {e}", level="error")
            return False

    # NOTE: Quantity updates now use update_card() method instead
    # The update_quantity() method was removed as update_card() provides more comprehensive functionality

    def update_card(self, index, quantity=None, condition=None, is_foil=None, is_surge=None, split_quantity=None):
        """
        Update a card in inventory, with automatic splitting if foil type changes and quantity > 1

        Args:
            index: Row index (0-based, from get_all_cards())
            quantity: New quantity (optional)
            condition: New condition (optional)
            is_foil: New foil status (optional)
            is_surge: New surge status (optional)
            split_quantity: Number of cards to split with new foil type (optional, defaults to 1 if foil changed)

        Returns:
            dict: {'success': bool, 'split': bool, 'message': str}
        """
        with self._lock:
            try:
                cards = self.get_all_cards()

                if index < 0 or index >= len(cards):
                    self.log(f"Invalid index: {index}", level="error")
                    return {'success': False, 'split': False, 'message': 'Invalid index'}

                # Get the card at this index
                card = cards[index]
                card_name = card['Card Name']
                old_quantity = int(card['Quantity'])
                old_foil = (card['Foil'] == 'Yes')
                old_surge = (card.get('Surge', 'No') == 'Yes')

                # Use old values if new ones not provided
                new_quantity = int(quantity) if quantity is not None else old_quantity
                new_condition = condition if condition is not None else card['Condition']
                new_foil = is_foil if is_foil is not None else old_foil
                new_surge = is_surge if is_surge is not None else old_surge

                # Check if foil type changed
                foil_type_changed = (new_foil != old_foil) or (new_surge != old_surge)

                # If foil type changed and quantity > 1, split the entry
                if foil_type_changed and old_quantity > 1:
                    # Use provided split_quantity or default to 1
                    qty_to_split = split_quantity if split_quantity is not None else 1

                    # Validate split quantity
                    if qty_to_split < 1 or qty_to_split > old_quantity:
                        self.log(f"Invalid split quantity: {qty_to_split} (must be 1-{old_quantity})", level="error")
                        return {'success': False, 'split': False, 'message': f'Invalid split quantity (must be 1-{old_quantity})'}

                    cursor = self.conn.cursor()

                    # Reduce original entry quantity by split_quantity
                    remaining_quantity = old_quantity - qty_to_split

                    if remaining_quantity > 0:
                        # Update original entry with reduced quantity
                        cursor.execute('''
                            UPDATE inventory
                            SET quantity = ?
                            WHERE card_name = ?
                              AND set_name = ?
                              AND card_number = ?
                              AND condition = ?
                              AND foil = ?
                              AND surge = ?
                        ''', (
                            remaining_quantity,
                            card['Card Name'],
                            card['Set'],
                            card['Card Number'],
                            card['Condition'],
                            1 if old_foil else 0,
                            1 if old_surge else 0
                        ))
                    else:
                        # Delete original entry if all cards moved to new foil type
                        cursor.execute('''
                            DELETE FROM inventory
                            WHERE card_name = ?
                              AND set_name = ?
                              AND card_number = ?
                              AND condition = ?
                              AND foil = ?
                              AND surge = ?
                        ''', (
                            card['Card Name'],
                            card['Set'],
                            card['Card Number'],
                            card['Condition'],
                            1 if old_foil else 0,
                            1 if old_surge else 0
                        ))

                    # Create new entry with new foil type and split_quantity
                    # (Use add_card logic with duplicate detection)
                    cursor.execute('''
                        INSERT INTO inventory (
                            card_name, set_name, card_number, rarity, type_line,
                            mana_cost, colors, color_identity, price_usd,
                            quantity, condition, foil, surge, timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(card_name, set_name, card_number, condition, foil, surge)
                        DO UPDATE SET quantity = quantity + ?
                    ''', (
                        card['Card Name'],
                        card['Set'],
                        card['Card Number'],
                        card['Rarity'],
                        card['Type'],
                        card.get('Mana Cost', ''),
                        card.get('Colors', ''),
                        card.get('Color Identity', ''),
                        float(card['Price (USD)'].replace('$', '')) if card.get('Price (USD)') else 0.0,
                        qty_to_split,  # quantity from split
                        new_condition,
                        1 if new_foil else 0,
                        1 if new_surge else 0,
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        qty_to_split  # For ON CONFLICT DO UPDATE
                    ))

                    self.conn.commit()

                    if remaining_quantity > 0:
                        self.log(f"Split {card_name}: {remaining_quantity} ({'surge foil' if old_surge else 'foil' if old_foil else 'non-foil'}) + {qty_to_split} ({'surge foil' if new_surge else 'foil' if new_foil else 'non-foil'})", level="success")
                    else:
                        self.log(f"Converted all {qty_to_split} {card_name} to {'surge foil' if new_surge else 'foil' if new_foil else 'non-foil'}", level="success")

                    return {'success': True, 'split': True, 'message': 'Card updated and split'}

                else:
                    # No foil type change, or quantity is 1
                    cursor = self.conn.cursor()

                    # If foil type changed (but quantity is 1), check for duplicates
                    if foil_type_changed:
                        # Check if target combination already exists
                        cursor.execute('''
                            SELECT quantity FROM inventory
                            WHERE card_name = ?
                              AND set_name = ?
                              AND card_number = ?
                              AND condition = ?
                              AND foil = ?
                              AND surge = ?
                        ''', (
                            card['Card Name'],
                            card['Set'],
                            card['Card Number'],
                            new_condition,
                            1 if new_foil else 0,
                            1 if new_surge else 0
                        ))

                        existing = cursor.fetchone()

                        if existing:
                            # Target already exists - merge quantities and delete old entry
                            existing_quantity = existing[0]

                            # Update the existing entry with merged quantity
                            cursor.execute('''
                                UPDATE inventory
                                SET quantity = ?
                                WHERE card_name = ?
                                  AND set_name = ?
                                  AND card_number = ?
                                  AND condition = ?
                                  AND foil = ?
                                  AND surge = ?
                            ''', (
                                existing_quantity + new_quantity,
                                card['Card Name'],
                                card['Set'],
                                card['Card Number'],
                                new_condition,
                                1 if new_foil else 0,
                                1 if new_surge else 0
                            ))

                            # Delete the old entry
                            cursor.execute('''
                                DELETE FROM inventory
                                WHERE card_name = ?
                                  AND set_name = ?
                                  AND card_number = ?
                                  AND condition = ?
                                  AND foil = ?
                                  AND surge = ?
                            ''', (
                                card['Card Name'],
                                card['Set'],
                                card['Card Number'],
                                card['Condition'],
                                1 if old_foil else 0,
                                1 if old_surge else 0
                            ))

                            self.conn.commit()

                            self.log(f"Merged {card_name}: changed foil status and merged with existing entry (total: {existing_quantity + new_quantity})", level="success")
                            return {'success': True, 'split': False, 'message': 'Card merged with existing entry'}
                        # else: Target doesn't exist, fall through to UPDATE

                    # No duplicate issue - just update the entry
                    cursor.execute('''
                        UPDATE inventory
                        SET quantity = ?, condition = ?, foil = ?, surge = ?
                        WHERE card_name = ?
                          AND set_name = ?
                          AND card_number = ?
                          AND condition = ?
                          AND foil = ?
                          AND surge = ?
                    ''', (
                        new_quantity,
                        new_condition,
                        1 if new_foil else 0,
                        1 if new_surge else 0,
                        card['Card Name'],
                        card['Set'],
                        card['Card Number'],
                        card['Condition'],
                        1 if old_foil else 0,
                        1 if old_surge else 0
                    ))

                    self.conn.commit()

                    self.log(f"Updated {card_name}: quantity={new_quantity}, condition={new_condition}", level="success")
                    return {'success': True, 'split': False, 'message': 'Card updated'}

            except Exception as e:
                self.log(f"Error updating card: {e}", level="error")
                return {'success': False, 'split': False, 'message': str(e)}

    def clear_inventory(self):
        """
        Clear all cards from inventory

        Returns:
            Dict with success status and count of deleted cards
        """
        try:
            cursor = self.conn.cursor()

            # Get count before deletion
            cursor.execute('SELECT COUNT(*) as count FROM inventory')
            count_before = cursor.fetchone()['count']

            # Delete all records
            cursor.execute('DELETE FROM inventory')
            self.conn.commit()

            self.log(f"Inventory cleared: {count_before} entries removed", level="success")

            return {
                'success': True,
                'deleted': count_before
            }

        except Exception as e:
            self.log(f"Failed to clear inventory: {e}", level="error")
            return {
                'success': False,
                'error': str(e),
                'deleted': 0
            }

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
