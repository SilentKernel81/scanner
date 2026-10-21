"""
Magic: The Gathering - Scryfall card data (database.CardDatabase, table `cards`), set code +
collector number matching (card_search.CardSearcher), regular / foil / surge foil finishes,
Moxfield collection export and import.
"""
import csv
from datetime import datetime

from card_scanner.core.card_search import CardSearcher
from card_scanner.config import Config
from card_scanner.core.database import CONFIRMED_MATCHES, search_key
from card_scanner.games.base import Game
from card_scanner.games import mtg_decks

COLOR_NAMES = {'W': 'White', 'U': 'Blue', 'B': 'Black', 'R': 'Red', 'G': 'Green'}

def color_identity(colors):
    if not colors:
        return 'Colorless'
    if len(colors) == 1:
        return COLOR_NAMES.get(colors[0], colors[0])
    return 'Multicolor'


# Moxfield's condition names (and the short forms its import takes) -> the conditions used here
MOXFIELD_CONDITIONS = {
    'mint': 'Mint', 'm': 'Mint', 'near mint': 'Near Mint', 'nm': 'Near Mint',
    'lightly played': 'Excellent', 'lp': 'Excellent', 'good (lightly played)': 'Excellent',
    'moderately played': 'Good', 'mp': 'Good', 'played': 'Played',
    'heavily played': 'Played', 'hp': 'Played', 'damaged': 'Poor', 'poor': 'Poor', 'dmg': 'Poor',
}


def write_moxfield(rows, file):
    """
    Moxfield collection import, in the columns of Moxfield's own export (Count, Tradelist Count,
    Name, Edition, Condition, Language, Foil, Tags, Last Modified, Collector Number, Alter, Proxy,
    Purchase Price): Archidekt's "Moxfield" preset reads those 13 by position, so a shorter or
    reordered file fails there ("Row columns do not match header columns").
    Edition is the set code: Moxfield matches it against its sets and, for a value it doesn't
    know (the set name used to be written), takes the alphabetically first set of the card.
    Rows without a set code (old CSV imports) fall back to the set name.
    """
    writer = csv.writer(file)
    writer.writerow(['Count', 'Tradelist Count', 'Name', 'Edition', 'Condition', 'Language', 'Foil',
                     'Tags', 'Last Modified', 'Collector Number', 'Alter', 'Proxy', 'Purchase Price'])
    for row in rows:
        # A surge foil is the foil finish of a surge foil printing (Moxfield's finishes: foil, etched)
        foil = 'foil' if row['finish'] in ('foil', 'surge') else ''
        writer.writerow([row['quantity'], 0, row['name'], (row.get('set_code') or '').lower() or row['set_name'],
                         row['condition'], 'English', foil, '', '', row['number'], 'False', 'False', '0'])


class Magic(Game):
    id = 'mtg'
    label = 'Magic: The Gathering'
    source = 'Scryfall'
    has_treatments = True
    set_example = 'HOB'
    number_example = '123'
    finishes = {'regular': 'Regular', 'foil': 'Foil', 'surge': 'Surge foil'}
    confirmed_matches = CONFIRMED_MATCHES
    deck_formats = mtg_decks.DECK_FORMATS

    def __init__(self, database, log_callback=None):
        super().__init__(database, log_callback)
        self.searcher = CardSearcher(database, log_callback=log_callback)

    def card_count(self):
        return self.db.get_database_stats()['total_cards']

    def download(self, progress_callback=None):
        cards_data = self.db.download_scryfall_data(progress_callback)
        return self.db.populate_database(cards_data, progress_callback)

    def check_for_update(self):
        # Scryfall republishes every day (prices); new sets are what matters, so the local
        # copy only counts as outdated after update_after_days
        remote = self.db.fetch_scryfall_info().get('updated_at') or ''
        local = (self.db.get_data_info(self.id) or {}).get('source_updated')
        if not local:
            return f"Scryfall has card data from {remote[:10]}; when yours was downloaded is not recorded"
        age = (datetime.fromisoformat(remote) - datetime.fromisoformat(local)).days
        if age >= Config.DATABASE_UPDATE_AFTER_DAYS:
            return f"Scryfall has card data from {remote[:10]}; yours is from {local[:10]}"
        return None

    def identify(self, name, number=None, set_code=None, ai_model=None):
        return self.searcher.search_by_name(name, number, ai_model=ai_model, set_code=set_code)

    def confirmed_card(self, name, number=None, set_code=None):
        # confirmed_read's look-up (no log lines of identify), keeping the card: every confirmed match needs the collector number
        card = self.db.search_card_exact(name, number, set_code) if number else None
        return card if self.is_confirmed(card) else None

    def card_by_printing(self, printing_id):
        # Not a search: the printing was chosen by its picture (printing_check.live_read) and is fetched by its id. Looking it up again by
        # name and number cannot be trusted for it - for about one printing in seven of the card data (promos, 'CMR-77', '245p', '185★')
        # that lookup returns another printing or an unconfirmed one (scripts/experiments/image_match/verify_roundtrip.py)
        card = self.db.get_card_by_id(printing_id)
        if card:
            card['match'] = 'picture'
        return card

    def confirmed_read(self, name, number=None, set_code=None):
        # Every confirmed match needs the collector number
        return bool(number) and self.is_confirmed(self.db.search_card_exact(name, number, set_code))

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

    def suggested_finish(self, card, foil='unknown'):
        # Printings that only exist in one finish are certain; otherwise the ★/• marker
        finishes = card.get('finishes') or []
        has_foil = 'foil' in finishes or 'etched' in finishes
        has_nonfoil = 'nonfoil' in finishes
        foil_kind = 'surge' if 'Surge Foil' in (card.get('treatments') or []) else 'foil'
        if has_foil and not has_nonfoil:
            return foil_kind
        if has_nonfoil and not has_foil:
            return 'regular'
        return foil_kind if foil == 'foil' else 'regular'

    def finish_known(self, card, foil='unknown'):
        """
        Whether the finish is known: a printing that exists in one finish only, or a ★ / • the reading read (foil 'foil' / 'non-foil'). A printing made in both
        with the marker unread is NOT known (since 2026-10-21, the owner's choice): it goes to review with its printing, instead of being added as regular -
        suggested_finish's guess, which put a foil the marker reading missed in as regular (a confetti foil on the Pixel, the same day)
        """
        finishes = card.get('finishes') or []
        has_foil = 'foil' in finishes or 'etched' in finishes
        has_nonfoil = 'nonfoil' in finishes
        return not (has_foil and has_nonfoil) or foil in ('foil', 'non-foil')

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

    def card_details(self, card_ids):
        return {card_id: {'image_uri': card['image_uri'], 'cmc': card['cmc'],
                          'set_code': card['set_code'], 'number': card['number'],
                          'identity': card['color_identity'] if card['oracle_id'] else card['colors']}
                for card_id, card in self.db.cards_by_ids(card_ids).items()}

    # -- Decks ---------------------------------------------------------------

    def has_deck_data(self):
        return self.db.has_deck_data()

    def search_cards(self, **filters):
        return self.db.search_cards(**filters)

    def cards_by_names(self, names):
        return self.db.cards_by_names(names)

    def printings(self, name):
        # Straight from the card data: the manual search (find_printings) writes to the activity log
        _resolved, cards = self.db.find_printings(name)
        return [{key: card[key] for key in ('id', 'set', 'set_code', 'number', 'image_uri', 'price',
                                            'price_foil', 'finishes', 'treatments')} for card in cards]

    def deck_card_payload(self, card):
        return {
            'id': card['id'],
            'name': card['name'],
            'type_line': card['type_line'] or '',
            'mana_cost': card['mana_cost'],
            'cmc': card['cmc'],
            'colors': card['colors'],
            'identity': card['color_identity'],
            'rarity': card['rarity'],
            'oracle_text': card['oracle_text'] or '',
            'image_uri': card['image_uri'],
            # The cheapest printing: what completing a deck costs at least
            'price': card.get('cheapest') or card['price'] or card['price_foil'],
        }

    def check_deck(self, deck_format, entries):
        return mtg_decks.check_deck(deck_format, entries)

    def can_be_commander(self, card):
        return mtg_decks.can_be_commander(card)

    def parse_decklist(self, text, deck_format=None):
        return mtg_decks.parse_decklist(
            text, commander_format=bool(mtg_decks.DECK_FORMATS.get(deck_format, {}).get('commander')))

    def format_decklist(self, entries, deck_format=None):
        return mtg_decks.format_decklist(entries, deck_format)

    def arena_decklist(self, entries, deck_format=None):
        """A deck as text for MTG Arena's Import button: (text, [names it leaves out]) - mtg_decks.format_arena"""
        by_key = self.db.cards_by_names([entry['name'] for entry in entries])
        cards = {entry['name']: by_key.get(search_key(entry['name'])) for entry in entries}
        return mtg_decks.format_arena(entries, deck_format, cards)

    def export_formats(self):
        return {'moxfield': ('Moxfield', 'moxfield_export', write_moxfield), **super().export_formats()}

    import_formats = ('Moxfield', 'ManaBox')

    def import_rows(self, columns, rows):
        # Moxfield's collection CSV: Count, Name, Edition (set code), Condition, Foil
        # (foil / etched / empty), Collector Number, Tags
        if {'count', 'name', 'edition'} <= columns:
            return self._collection_rows('Moxfield', rows, count='count', edition='edition', number='collector number')
        # ManaBox's: Name, Set code, Set name, Collector number, Foil (normal / foil / etched),
        # Rarity, Quantity, ManaBox ID, Scryfall ID, ..., Condition (near_mint, lightly_played ...).
        # Written from what its exports are known to look like - not tried on a file of the app
        if {'name', 'set code', 'collector number', 'quantity'} <= columns and ('scryfall id' in columns or 'foil' in columns):
            return self._collection_rows('ManaBox', rows, count='quantity', edition='set code', number='collector number')
        return None

    def _collection_rows(self, format_name, rows, count, edition, number):
        """The entries of another app's collection file, its columns named; the printing by the
        file's Scryfall id when it has one (ManaBox), else by set code + collector number, else by name"""
        # `unnamed`: rows that hold something but no card name - cannot be matched, so a
        # replacement must not go ahead without them (see import_inventory)
        result = {'format': format_name, 'entries': [], 'by_name': 0, 'not_found': [], 'unnamed': 0}
        front = lambda card_name: search_key(card_name.split(' // ')[0])
        for row in rows:
            name = (row.get('name') or '').strip()
            if not name:
                if any((value or '').strip() for value in row.values()):
                    result['unnamed'] += 1
                continue
            card = None
            scryfall_id = (row.get('scryfall id') or '').strip()
            if scryfall_id:
                card = self.db.get_card_by_id(scryfall_id)
                if card and front(card['name']) != front(name):
                    card = None  # an id that belongs to another card is not trusted
            # The printing by set code + collector number; a card the database has under
            # another number (or a row without one) is still taken, by its name
            set_code, collector = (row.get(edition) or '').strip(), (row.get(number) or '').strip()
            if not card:
                card = self.db.get_card_by_set_number(set_code, collector, exact=True)
                if card and front(card['name']) != front(name):
                    # A set and number that belong to another card: never taken on the numbers alone
                    card = None
            if not card:
                # Written another way ("007" for 7) - only when it is the card the row names
                card = self.db.get_card_by_set_number(set_code, collector)
                if card and front(card['name']) != front(name):
                    card = None
            if not card:
                card = self.db.search_card(name, fuzzy=False)
                if not card:
                    result['not_found'].append(name)
                    continue
                result['by_name'] += 1
            try:
                quantity = max(1, int(float(row.get(count) or 1)))
            except ValueError:
                quantity = 1
            # Moxfield has no surge foil: it is the foil of a surge foil printing (write_moxfield);
            # ManaBox says "normal" for a card that is not foil
            foil = (row.get('foil') or '').strip().lower()
            finish = 'regular' if foil in ('', 'normal', 'nonfoil', 'false') \
                else 'surge' if 'Surge Foil' in (card.get('treatments') or []) else 'foil'
            condition = (row.get('condition') or '').strip().lower().replace('_', ' ')
            result['entries'].append({
                'fields': self.inventory_fields(card, finish), 'finish': finish, 'quantity': quantity,
                'condition': MOXFIELD_CONDITIONS.get(condition, 'Near Mint'),
                'tags': row.get('tags') or row.get('tag') or '',
            })
        return result
