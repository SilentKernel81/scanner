"""
Magic: The Gathering - Scryfall card data (database.CardDatabase, table `cards`), set code +
collector number matching (card_search.CardSearcher), regular / foil / surge foil finishes,
CSV and Moxfield exports.
"""
import csv

from card_search import CardSearcher
from database import CONFIRMED_MATCHES
from games.base import Game

COLOR_NAMES = {'W': 'White', 'U': 'Blue', 'B': 'Black', 'R': 'Red', 'G': 'Green'}

# Column order of the CSV export (also what import_csv reads back)
CSV_COLUMNS = ['Card Name', 'Set', 'Card Number', 'Rarity', 'Type', 'Mana Cost', 'Colors',
               'Color Identity', 'Price (USD)', 'Quantity', 'Condition', 'Foil', 'Surge', 'Timestamp']


def color_identity(colors):
    if not colors:
        return 'Colorless'
    if len(colors) == 1:
        return COLOR_NAMES.get(colors[0], colors[0])
    return 'Multicolor'


def write_csv(rows, file):
    writer = csv.writer(file)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([
            row['name'], row['set_name'], row['number'], row['rarity'], row['type_line'],
            row['mana_cost'], row['colors'], row['color_identity'], f"${row['price']:.2f}",
            row['quantity'], row['condition'],
            'Yes' if row['finish'] == 'foil' else 'No',
            'Yes' if row['finish'] == 'surge' else 'No',
            row['timestamp'],
        ])


def write_moxfield(rows, file):
    """Moxfield collection import: Count,Name,Edition,Condition,Language,Foil,Collector Number,..."""
    writer = csv.writer(file)
    writer.writerow(['Count', 'Name', 'Edition', 'Condition', 'Language', 'Foil',
                     'Collector Number', 'Purchase Price', 'Tag'])
    for row in rows:
        foil = {'foil': 'foil', 'surge': 'surge'}.get(row['finish'], '')
        writer.writerow([row['quantity'], row['name'], row['set_name'], row['condition'], 'English',
                         foil, row['number'], '0', ''])


class Magic(Game):
    id = 'mtg'
    label = 'Magic: The Gathering'
    finishes = {'regular': 'Regular', 'foil': 'Foil', 'surge': 'Surge foil'}
    confirmed_matches = CONFIRMED_MATCHES

    def __init__(self, database, log_callback=None):
        super().__init__(database, log_callback)
        self.searcher = CardSearcher(database, log_callback=log_callback)

    def card_count(self):
        return self.db.get_database_stats()['total_cards']

    def download(self, progress_callback=None):
        cards_data = self.db.download_scryfall_data(progress_callback)
        return self.db.populate_database(cards_data, progress_callback)

    def identify(self, name, number=None, set_code=None, ai_model=None):
        return self.searcher.search_by_name(name, number, ai_model=ai_model, set_code=set_code)

    def find_printings(self, name, number=None, treatment=None, set_code=None):
        return self.searcher.find_printings(name, number, treatment, set_code)

    def similar(self, name, limit=5):
        return [{'name': row['name'], 'set': row['set_name'],
                 'price': f"${row['price_usd']:.2f}" if row['price_usd'] else 'N/A'}
                for row in self.searcher.find_similar_cards(name, limit=limit)]

    def get_card(self, card_id):
        return self.db.get_card_by_id(card_id)

    def card_payload(self, card):
        return {
            'id': card['id'],
            'name': card['name'],
            'set': card['set'],
            'set_code': card['set_code'],
            'number': card['number'],
            'rarity': card['rarity'],
            'type': card['type_line'],
            'price': card['price'],
            'price_foil': card['price_foil'],
            'image_uri': card['image_uri'],
            'treatments': card['treatments'],
            'finishes': card['finishes'],
            # How an AI-identified card was matched (None for manual picks)
            'confirmed': self.is_confirmed(card) if card.get('match') else None,
        }

    def inventory_fields(self, card, finish):
        colors = card.get('colors') or []
        # Foil and surge foil copies are priced as foils; fall back to the other price if missing
        prices = (card.get('price_foil'), card.get('price')) if finish in ('foil', 'surge') \
            else (card.get('price'), card.get('price_foil'))
        return {
            'card_id': card.get('id'),
            'name': card['name'],
            'set_name': card.get('set') or '',
            'set_code': card.get('set_code') or '',
            'number': card.get('number') or '',
            'rarity': card.get('rarity') or '',
            'type_line': card.get('type_line') or '',
            'mana_cost': card.get('mana_cost') or '',
            'colors': ', '.join(colors) if colors else 'Colorless',
            'color_identity': color_identity(colors),
            'price': next((float(p) for p in prices if p), 0.0),
        }

    def export_formats(self):
        return {
            'csv': ('CSV', 'card_inventory_export', write_csv),
            'moxfield': ('Moxfield CSV', 'moxfield_export', write_moxfield),
        }
