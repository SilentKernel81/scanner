"""
What every card game provides to the scanner. The web app, AI worker and inventory only talk
to the active game through this interface; each game module (games/mtg.py, ...) implements it
on top of its own card data.
"""


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

    def with_prices(self, card):
        """The card with current prices (fetches_prices games: may make a network request)"""
        return card

    def card_payload(self, card):
        """Card fields sent to the web page"""
        raise NotImplementedError

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

    # Export key -> (label, file name prefix, writer(rows, file)); rows come from
    # InventoryManager.get_all_cards
    def export_formats(self):
        return {}
