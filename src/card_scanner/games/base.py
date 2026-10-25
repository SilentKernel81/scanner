"""
What every card game provides to the scanner. The web app, AI worker and inventory only talk
to the active game through this interface; each game module (games/mtg.py, ...) implements it
on top of its own card data.
"""
import csv

# The app's own CSV: every column an inventory entry stores, so a file written here comes back
# as it was (inventory.import_csv) - a copy of the collection that any spreadsheet reads.
# Capture thumbnails and decks are not in it (the backups have those)
COLLECTION_COLUMNS = ['Card Name', 'Set', 'Set Code', 'Card Number', 'Finish', 'Quantity', 'Condition',
                      'Location', 'Tags', 'Price (USD)', 'Rarity', 'Type', 'Mana Cost', 'Colors',
                      'Color Identity', 'Card ID', 'Timestamp']


def write_collection_csv(rows, file, currency='USD', symbol='$'):
    # The price column says its currency (since 2026-10-24: Settings > Card data > Prices). In dollars the file is what it
    # always was - the Android app writes the same file byte for byte (its ExportParityTest, with the defaults)
    writer = csv.writer(file)
    writer.writerow([f'Price ({currency})' if column == 'Price (USD)' else column for column in COLLECTION_COLUMNS])
    for row in rows:
        writer.writerow([
            row['name'], row['set_name'], row['set_code'], row['number'], row['finish'], row['quantity'],
            row['condition'], row.get('location', ''), ', '.join(row.get('tags') or []),
            f"{symbol}{row['price']:.2f}", row['rarity'], row['type_line'], row['mana_cost'], row['colors'],
            row['color_identity'], row['card_id'] or '', row['timestamp'],
        ])


class Game:
    """One card game: its card data, how AI reads are matched, finishes and exports"""

    id = None          # stored in inventory rows, settings and data/prompts.json
    label = None
    source = None      # where the card data comes from ("Scryfall")
    # Manual search: whether the treatment filter applies, and example set code / number
    has_treatments = False
    set_example = ''
    number_example = ''
    card_ratio = 88 / 63   # height / width of the card (outline detection)
    # Finish key -> label, in display order; the first one is the default
    finishes = {}
    # Match tags (card['match']) that identify the exact printing - only these are added
    # to the inventory without review
    confirmed_matches = set()
    # Prices fetched per card (a network request, with_prices) instead of coming with the data
    fetches_prices = False
    # Deck builder: format id -> {'label', 'commander' (has a commander; its 'side' board is a
    # list of cards being considered), ...}, in display order. Empty: no deck builder
    deck_formats = {}

    def __init__(self, database, log_callback=None):
        self.db = database
        self.log_callback = log_callback

    def log(self, message, level='info'):
        if self.log_callback:
            self.log_callback(message, level)

    def is_confirmed(self, card):
        return bool(card) and card.get('match') in self.confirmed_matches

    @property
    def default_finish(self):
        return next(iter(self.finishes))

    # -- Card data -----------------------------------------------------------

    def card_count(self):
        """Number of cards in the local card data (0 = not downloaded yet)"""
        raise NotImplementedError

    def download(self, progress_callback=None):
        """Download / refresh the card data; returns the number of cards imported"""
        raise NotImplementedError

    def check_for_update(self):
        """
        Whether newer card data is available (a network request): a short message saying
        why, or None when the local data is current
        """
        return None

    # -- Matching and search -------------------------------------------------

    def identify(self, name, number=None, set_code=None, ai_model=None):
        """
        Best printing for what the AI read, tagged with card['match'] (see
        confirmed_matches), or None
        """
        raise NotImplementedError

    def confirmed_card(self, name, number=None, set_code=None):
        """
        The card a read confirms (is_confirmed), or None: confirmed_read's yes / no with the card itself, from ONE look-up - for the reading that
        needs the printing the OCR read, not only whether it is certain. confirmed_read stays what it was.
        """
        card = self.identify(name, number, set_code) if number else None
        return card if self.is_confirmed(card) else None

    def card_by_printing(self, printing_id):
        """The card of a printing the picture and the strip decided on, tagged as a confirmed match, or None (a game without one answers None)"""
        return None

    def confirmed_read(self, name, number=None, set_code=None):
        """
        Whether a read identifies an exact printing (is_confirmed) - asked about the OCR read
        to decide if the vision AI is needed. Games override it to look the card up without
        the log lines of identify, which follows for the read that is used
        """
        return self.is_confirmed(self.identify(name, number, set_code))

    def find_printings(self, name, number=None, treatment=None, set_code=None):
        """Manual search: (resolved name or None, list of printings)"""
        raise NotImplementedError

    def similar(self, name, limit=5):
        """Cards with a similar name: [{'name', 'set', 'price'}]"""
        raise NotImplementedError

    def get_card(self, card_id):
        raise NotImplementedError

    def suggested_finish(self, card, foil='unknown'):
        """
        Finish key the copy in hand most likely has (automatic adds; the page shows the same
        suggestion - scanner.js suggestedFinish). foil: the ★/• marker the AI read
        """
        return self.default_finish

    def finish_known(self, card, foil='unknown'):
        """
        Whether the finish of a copy of `card` is known - from the card itself or the marker read (foil) - so that it may be added automatically. A game
        without finishes to tell apart always knows it
        """
        return True

    def with_prices(self, card):
        """The card with current prices (fetches_prices games: may make a network request)"""
        return card

    def card_payload(self, card):
        """Card fields sent to the web page"""
        raise NotImplementedError

    def current_prices(self, entries):
        """
        Today's price of inventory entries ([{'id', 'card_id', 'finish'}], InventoryManager.priced_entries) from the card
        data: {row id: (card_id, finish, price)} - InventoryManager.reprice's argument. An entry whose printing has no price
        now is left out (it keeps the one it has). A game whose card data holds no prices (fetches_prices) has nothing here.
        """
        return {}

    def current_price(self, card_id, finish):
        """A printing's price in a finish in the card data as it is now (InventoryManager.current_price, through the
        server), or None: no card data prices (fetches_prices games), or no such printing - the price given is kept"""
        return None

    def price_changes(self, store, new_source=False, new_currency=False):
        """
        What pricing an inventory again changes (server/card_data.py: reprice_collections): (prices, cleared) - prices for
        InventoryManager.reprice (current_prices of the entries that know their printing), and the entries that lose their
        price, for InventoryManager.clear_prices: {row id: (card_id, finish)}. An entry is worth what the chosen source says
        (its market value), so:
        - the same source, a new day without a price for it: it keeps the one it has (no price today is not "worthless");
        - new_source: an entry the new source has no price for loses it - keeping it would add up one shop's number with
          another's (a round trip TCGplayer -> Mana Pool -> TCGplayer left Mana Pool's on 23 entries, 2026-10-24);
        - new_currency: also the entries that do not know their printing (no source can price them; the price they came
          with is kept as long as the currency is the same) - a number in the old currency would be shown in the new one.
        """
        entries = store.priced_entries(self.id)
        prices = self.current_prices(entries)
        cleared = {}
        if new_source or new_currency:
            cleared = {entry['id']: (entry['card_id'], entry['finish']) for entry in entries if entry['id'] not in prices}
        if new_currency:
            cleared.update({entry['id']: (entry['card_id'], entry['finish']) for entry in store.unpriceable_entries(self.id)})
        return prices, cleared

    # -- Inventory -----------------------------------------------------------

    def inventory_fields(self, card, finish):
        """
        Inventory columns for a printing in a finish: card_id, name, set_name, set_code,
        number, rarity, type_line, price, and (optional) mana_cost, colors, color_identity
        """
        raise NotImplementedError

    def card_details(self, card_ids):
        """
        {printing id: {'image_uri', ...}} for the collection page: what the inventory rows of
        these printings are shown and filtered with, beyond the columns they store
        """
        return {}

    def buylist_prices(self, entries):
        """
        {entry id: what a shop pays for one copy} for inventory rows (dicts with 'id', 'card_id', 'finish') - only the
        entries it has a price for. Shown beside the value, never counted in it; a game without a buylist has none
        """
        return {}

    # -- Decks (games with deck_formats) --------------------------------------

    def has_deck_data(self):
        """Whether the card data has what deck building needs (older downloads may not)"""
        return False

    def search_cards(self, **filters):
        """Deck builder search: (cards - one per name, whether there are more)"""
        raise NotImplementedError

    def cards_by_names(self, names):
        """{search key of the name: card} - one printing per card name"""
        raise NotImplementedError

    def printings(self, name):
        """Every printing of a card, newest first, for the deck builder's printing picker:
        [{'id', 'set', 'set_code', 'number', 'image_uri', 'price', 'price_foil', 'price_etched', 'finishes', 'treatments'}]"""
        raise NotImplementedError

    def deck_card_payload(self, card):
        """Card fields the deck builder shows and counts with"""
        raise NotImplementedError

    def check_deck(self, deck_format, entries):
        """Issues of a deck: [{'level', 'message', 'cards'}]; entries: [{'name', 'quantity', 'board', 'card'}]"""
        return []

    def parse_decklist(self, text, deck_format=None):
        """Pasted decklist -> [{'name', 'quantity', 'board'}]"""
        raise NotImplementedError

    def format_decklist(self, entries, deck_format=None):
        raise NotImplementedError

    # A game with a digital client that reads decklists gives the deck as that client wants
    # it: (text, [names left out]). None: the game has none, and the page shows no button.
    arena_decklist = None

    # Export key -> (label, file name prefix, writer(rows, file)); rows come from
    # InventoryManager.get_all_cards
    def export_formats(self):
        currency = self.price_currency()
        return {'csv': ('Card Scanner (everything)', f'{self.id}_collection',
                        lambda rows, file: write_collection_csv(rows, file, *currency))}

    def price_currency(self):
        """(currency code, symbol) of the prices the game shows and the entries hold"""
        return 'USD', '$'

    # Names of the other apps' collection files the game reads (the import dialog lists them)
    import_formats = ()

    def import_rows(self, columns, rows):
        """
        A collection file of another app -> inventory entries. columns: the file's column
        names (lower case); rows: its rows as dicts with those names.

        Returns None when the columns are not a format the game reads, else
        {'format': name, 'entries': [{'fields' (inventory_fields), 'finish', 'condition',
        'quantity', 'tags'}], 'by_name': entries matched by name only (another printing may
        have been taken), 'not_found': [names]}
        """
        return None
