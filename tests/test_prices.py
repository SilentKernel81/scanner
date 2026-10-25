"""
Prices (docs/plans/PRICE_SOURCES_PLAN.md): Scryfall's etched and EUR prices kept by the card data import, the etched
price used for an etched foil, the collection's entries priced again from new card data, the price source chosen (phase
2a), and the shops' prices from MTGJSON (phase 2b: core/shop_prices.py, the `shop_prices` table). Everything runs on
temporary databases and hand-written files; no application data is touched and nothing is downloaded.

    venv/bin/python -m unittest discover tests
"""
import csv
import io
import json
import lzma
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core import shop_prices  # noqa: E402
from card_scanner.core.database import CardDatabase  # noqa: E402
from card_scanner.games.mtg import Magic  # noqa: E402
from test_ownership import GAME, TemporaryData  # noqa: E402


def scryfall_card(number, prices, finishes=('nonfoil', 'foil')):
    """A card as Scryfall's bulk file has it - only what the import reads"""
    return {'id': f'id-{number}', 'name': f'Card {number}', 'set': 'tst', 'set_name': 'Test', 'collector_number': str(number),
            'layout': 'normal', 'prices': prices, 'finishes': list(finishes), 'image_uris': {'normal': ''}}


# 40 plain cards: replace_table refuses an import much smaller than the table in use, and a second import must pass
FILLER = [scryfall_card(n, {'usd': '0.10'}) for n in range(100, 140)]
# An etched-only printing (a Commander Legends etched foil: Scryfall has usd_etched and nothing else), one in both
# finishes, and one without any price
ETCHED = scryfall_card(1, {'usd': None, 'usd_foil': None, 'usd_etched': '4.50', 'eur': None}, finishes=('etched',))
BOTH = scryfall_card(2, {'usd': '1.00', 'usd_foil': '3.00', 'eur': '0.90', 'eur_foil': '2.50'})
UNPRICED = scryfall_card(3, {})


class CardData(TemporaryData):
    """A temporary card database and the Magic game on it"""

    def setUp(self):
        super().setUp()
        self.cards = CardDatabase(db_file=self.tmp / 'cards.db')
        self.game = Magic(self.cards)

    def tearDown(self):
        self.cards.conn.close()
        super().tearDown()


class PricesInTheCardData(CardData):
    def test_the_import_keeps_etched_and_euro_prices(self):
        self.cards.populate_database(FILLER + [ETCHED, BOTH, UNPRICED])
        row = lambda card_id: self.cards.conn.execute(
            'SELECT price_usd, price_usd_foil, price_usd_etched, price_eur, price_eur_foil FROM cards WHERE id = ?', (card_id,)).fetchone()
        self.assertEqual(tuple(row('id-1')), (None, None, 4.5, None, None))
        self.assertEqual(tuple(row('id-2')), (1.0, 3.0, None, 0.9, 2.5))
        self.assertEqual(tuple(row('id-3')), (None, None, None, None, None))
        self.assertEqual(self.cards.get_card_by_id('id-1')['price_etched'], 4.5)

    def test_an_etched_foil_takes_the_etched_price(self):
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        etched, both = self.cards.get_card_by_id('id-1'), self.cards.get_card_by_id('id-2')
        self.assertEqual(self.game.suggested_finish(etched), 'foil')                       # an etched foil is a 'foil' copy
        self.assertEqual(self.game.inventory_fields(etched, 'foil')['price'], 4.5)          # 0 before 2026-10-23
        self.assertEqual(self.game.inventory_fields(etched, 'regular')['price'], 4.5)       # the only price there is
        self.assertEqual(self.game.inventory_fields(both, 'foil')['price'], 3.0)            # unchanged: the foil price first
        self.assertEqual(self.game.inventory_fields(both, 'regular')['price'], 1.0)
        self.assertEqual(self.game.card_payload({**etched, 'match': None})['price_etched'], 4.5)

    def test_cheapest_counts_an_etched_only_printing(self):
        self.cards.populate_database(FILLER + [ETCHED])
        self.assertEqual(self.cards.cards_by_names(['Card 1'])['card 1']['cheapest'], 4.5)

    def test_an_older_file_gets_the_columns(self):
        self.cards.conn.execute('ALTER TABLE cards DROP COLUMN price_usd_etched')
        self.cards.conn.commit()
        self.cards.conn.close()
        self.cards = CardDatabase(db_file=self.tmp / 'cards.db')                            # the start-up migration
        columns = {row['name'] for row in self.cards.conn.execute('PRAGMA table_info(cards)')}
        self.assertIn('price_usd_etched', columns)
        self.game = Magic(self.cards)
        self.assertEqual(self.cards.populate_database(FILLER + [ETCHED]), 41)
        self.assertEqual(self.cards.get_card_by_id('id-1')['price_etched'], 4.5)


class PricedAgain(CardData):
    """The entries follow the card data (card_data.reprice_collections does this for both inventories after an update)"""

    def add(self, store, card_id, finish, **kwargs):
        return store.add_card(self.game.inventory_fields(self.cards.get_card_by_id(card_id), finish), GAME, finish, quiet=True, **kwargs)

    def update(self, *cards):
        """A new download of the card data, then what reprice_collections does with each inventory"""
        self.cards.populate_database(FILLER + list(cards))
        return sum(store.reprice(self.game.current_prices(store.priced_entries(GAME))) for store in (self.collection, self.scan))

    def price(self, store, row_id):
        return store.get_entry(row_id)['price_usd']

    def test_entries_take_the_new_price(self):
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        regular, foil = self.add(self.collection, 'id-2', 'regular'), self.add(self.collection, 'id-2', 'foil')
        scanned = self.add(self.scan, 'id-2', 'foil')
        self.assertEqual((self.price(self.collection, regular), self.price(self.collection, foil)), (1.0, 3.0))
        changed = self.update(ETCHED, {**BOTH, 'prices': {'usd': '1.25', 'usd_foil': '2.00'}})
        self.assertEqual(changed, 3)
        self.assertEqual((self.price(self.collection, regular), self.price(self.collection, foil)), (1.25, 2.0))
        self.assertEqual(self.price(self.scan, scanned), 2.0)
        self.assertEqual(self.update(ETCHED, {**BOTH, 'prices': {'usd': '1.25', 'usd_foil': '2.00'}}), 0)   # nothing changed: nothing written

    def test_an_entry_added_at_0_gets_its_etched_price(self):
        # What an etched foil was added at before the etched price was kept
        self.cards.populate_database(FILLER + [ETCHED])
        row_id = self.add(self.collection, 'id-1', 'foil')
        self.collection.conn.execute('UPDATE inventory SET price_usd = 0 WHERE id = ?', (row_id,))
        self.collection.conn.commit()
        self.assertEqual(self.update(ETCHED), 1)
        self.assertEqual(self.price(self.collection, row_id), 4.5)

    def test_no_price_today_keeps_the_old_one(self):
        self.cards.populate_database(FILLER + [BOTH])
        row_id = self.add(self.collection, 'id-2', 'regular')
        self.update({**BOTH, 'prices': {}})                                    # Scryfall has no price for it today
        self.assertEqual(self.price(self.collection, row_id), 1.0)
        self.update(ETCHED)                                                     # the printing is gone from the card data
        self.assertEqual(self.price(self.collection, row_id), 1.0)

    def test_an_entry_without_a_printing_is_left_alone(self):
        self.cards.populate_database(FILLER + [BOTH])
        row_id = self.collection.add_card({'card_id': None, 'name': 'Card 2', 'set_name': 'Test', 'set_code': 'tst', 'number': '2',
                                           'price': 7.0}, GAME, 'regular', quiet=True)   # a CSV import of an unknown printing
        self.assertEqual(self.collection.priced_entries(GAME), [])
        self.update({**BOTH, 'prices': {'usd': '1.25'}})
        self.assertEqual(self.price(self.collection, row_id), 7.0)

    def test_an_edit_made_meanwhile_is_not_overwritten(self):
        # The prices are worked out before reprice writes them: an entry changed in between (here: made foil) keeps its own
        self.cards.populate_database(FILLER + [BOTH])
        row_id = self.add(self.collection, 'id-2', 'regular')
        prices = self.game.current_prices(self.collection.priced_entries(GAME))
        self.collection.conn.execute("UPDATE inventory SET finish = 'foil', price_usd = 3.0 WHERE id = ?", (row_id,))
        self.collection.conn.commit()
        self.assertEqual(self.collection.reprice(prices), 0)
        self.assertEqual(self.price(self.collection, row_id), 3.0)

    def test_copies_a_trade_holds_are_priced_too(self):
        # The trade triggers refuse deletes and lower quantities, not a new price
        from card_scanner.core.trades import Trades
        self.cards.populate_database(FILLER + [BOTH])
        row_id = self.add(self.collection, 'id-2', 'regular')
        Trades(self.collection).add(GAME, 'Jo', [row_id])
        self.update({**BOTH, 'prices': {'usd': '1.25'}})
        self.assertEqual(self.price(self.collection, row_id), 1.25)


class PriceSources(CardData):
    """The chosen source in the shown columns (core/prices.py, CardDatabase.set_price_source; since 2026-10-24)"""

    def shown(self, card_id):
        card = self.cards.get_card_by_id(card_id)
        return card['price'], card['price_foil'], card['price_etched']

    def test_the_default_shows_scryfalls_dollars(self):
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        self.assertEqual(self.cards.price_source, 'scryfall_usd')
        self.assertEqual(self.shown('id-2'), (1.0, 3.0, 0.0))
        self.assertEqual(self.shown('id-1'), (0.0, 0.0, 4.5))
        self.assertEqual(self.cards.price_currency(), ('USD', '$'))

    def test_euros_and_back(self):
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        self.assertEqual(self.cards.set_price_source('scryfall_eur'), 'scryfall_eur')
        self.assertEqual(self.shown('id-2'), (0.9, 2.5, 0.0))
        # No euro price: nothing, never the dollar one standing in (the etched-only printing has none in euros)
        self.assertEqual(self.shown('id-1'), (0.0, 0.0, 0.0))
        self.assertEqual(self.cards.price_currency(), ('EUR', '€'))
        self.assertEqual(self.cards.cards_by_names(['Card 2'])['card 2']['cheapest'], 0.9)
        self.cards.set_price_source('scryfall_usd')
        self.assertEqual(self.shown('id-2'), (1.0, 3.0, 0.0))                  # Scryfall's own columns were never overwritten

    def test_how_many_printings_a_source_prices(self):
        # Card data from before the EUR prices were kept has none: the route refuses that source (server/card_data.py)
        self.cards.populate_database(FILLER + [ETCHED, BOTH, UNPRICED])
        self.assertEqual(self.cards.priced_printings('scryfall_usd'), 42)    # the 40 fillers, the etched one, BOTH
        self.assertEqual(self.cards.priced_printings('scryfall_eur'), 1)
        self.cards.conn.execute('UPDATE cards SET price_eur = NULL, price_eur_foil = NULL')
        self.assertEqual(self.cards.priced_printings('scryfall_eur'), 0)

    def test_an_unknown_source_is_the_default(self):
        self.cards.populate_database(FILLER + [BOTH])
        self.assertEqual(self.cards.set_price_source('nowhere'), 'scryfall_usd')
        self.assertEqual(self.shown('id-2'), (1.0, 3.0, 0.0))

    def test_new_card_data_comes_in_the_chosen_source(self):
        # Applied to the import table before the swap: the new cards are never shown in another currency
        self.cards.populate_database(FILLER + [BOTH])
        self.cards.set_price_source('scryfall_eur')
        self.cards.populate_database(FILLER + [{**BOTH, 'prices': {'usd': '1.50', 'eur': '1.20'}}])
        self.assertEqual(self.shown('id-2')[0], 1.2)

    def test_a_switch_during_an_import_is_kept(self):
        # The choice changes while the new cards are filled: the swapped-in table gets the new choice too
        self.cards.populate_database(FILLER + [BOTH])
        fill = self.cards._fill_import
        def fill_then_switch(*args):
            inserted = fill(*args)
            self.cards.set_price_source('scryfall_eur')          # the old table gets it ...
            return inserted
        self.cards._fill_import = fill_then_switch
        self.cards.populate_database(FILLER + [BOTH])            # ... and the new one, after the swap
        self.assertEqual(self.shown('id-2')[0], 0.9)

    def test_the_entries_follow_a_switch(self):
        self.cards.populate_database(FILLER + [BOTH])
        row_id = self.collection.add_card(self.game.inventory_fields(self.cards.get_card_by_id('id-2'), 'foil'), GAME, 'foil', quiet=True)
        self.cards.set_price_source('scryfall_eur')
        self.collection.reprice(self.game.current_prices(self.collection.priced_entries(GAME)))
        self.assertEqual(self.collection.get_entry(row_id)['price_usd'], 2.5)

    def test_an_older_file_gets_the_shown_columns_filled(self):
        self.cards.populate_database(FILLER + [BOTH])
        for column in ('price', 'price_foil', 'price_etched'):
            self.cards.conn.execute(f'ALTER TABLE cards DROP COLUMN {column}')
        self.cards.conn.commit()
        self.cards.conn.close()
        self.cards = CardDatabase(db_file=self.tmp / 'cards.db')
        self.assertEqual(self.shown('id-2'), (1.0, 3.0, 0.0))

    def test_the_csv_says_its_currency(self):
        import io
        from card_scanner.games.base import write_collection_csv
        row = {'name': 'Card 2', 'set_name': 'Test', 'set_code': 'tst', 'number': '2', 'finish': 'regular', 'quantity': 1,
               'condition': 'Near Mint', 'price': 0.9, 'rarity': '', 'type_line': '', 'mana_cost': '', 'colors': '',
               'color_identity': '', 'card_id': 'id-2', 'timestamp': ''}
        dollars, euros = io.StringIO(), io.StringIO()
        write_collection_csv([row], dollars)                       # as it always was (the Android app's ExportParityTest)
        self.assertIn('Price (USD)', dollars.getvalue().splitlines()[0])
        self.assertIn('$0.90', dollars.getvalue())
        self.cards.populate_database(FILLER + [BOTH])
        self.cards.set_price_source('scryfall_eur')
        self.game.export_formats()['csv'][2]([row], euros)
        self.assertIn('Price (EUR)', euros.getvalue().splitlines()[0])
        self.assertIn('€0.90', euros.getvalue())


# ----------------------------------------------------------------------------
# The shops' prices (phase 2b)
# ----------------------------------------------------------------------------

def mtgjson_prices(**by_uuid):
    """AllPricesToday's text: {uuid: {provider: {kind: {finish: value}}}} (paper), each value dated today"""
    data = {uuid: {'paper': {provider: {'currency': 'USD', **{kind: {finish: {'2026-10-24': value} for finish, value in finishes.items()}
                                                                for kind, finishes in kinds.items()}}
                             for provider, kinds in providers.items()}}
            for uuid, providers in by_uuid.items()}
    return json.dumps({'meta': {'date': '2026-10-24'}, 'data': data}, indent=1)


def identifiers(path, pairs):
    """An identifier file as MTGJSON's (xz, a CSV with many columns): (uuid, Scryfall id) pairs"""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['cardKingdomId', 'uuid', 'name', 'scryfallId'])
    for uuid, scryfall_id in pairs:
        writer.writerow(['', uuid, 'A name, with a comma', scryfall_id])
    with lzma.open(path, 'wt', encoding='utf-8') as f:
        f.write(out.getvalue())
    return path


# The plan's cases: a double-faced card with a shop on one face only (two uuids, one Scryfall id), an etched-only
# printing, an id that is not in the card data, a uuid in neither identifier file, and shops that are not kept
PRICE_FILE = mtgjson_prices(
    front={'cardkingdom': {'retail': {'normal': 1.49, 'foil': 3.99}, 'buylist': {'normal': 0.6}}},
    back={'cardkingdom': {'retail': {'normal': 1.49}}, 'manapool': {'retail': {'normal': 1.2}}},
    etched={'cardkingdom': {'retail': {'etched': 5.99}}},
    elsewhere={'manapool': {'retail': {'normal': 9.0}}},
    unmapped={'cardkingdom': {'retail': {'normal': 2.0}}},
    others={'tcgplayer': {'retail': {'normal': 1.0}}, 'cardmarket': {'retail': {'normal': 0.8}}},
)
CARD_PAIRS = [('front', 'id-2'), ('back', 'id-2'), ('etched', 'id-1'), ('elsewhere', 'id-zzz'), ('others', 'id-3')]


class ShopPriceFile(CardData):
    """core/shop_prices.py: MTGJSON's files into one row per printing"""

    def rows(self, text=PRICE_FILE):
        cards = identifiers(self.tmp / 'cards.csv.xz', CARD_PAIRS)
        tokens = identifiers(self.tmp / 'tokens.csv.xz', [])
        return shop_prices.rows_from(text, [cards, tokens])

    def test_the_faces_of_a_card_are_one_row(self):
        rows = self.rows()
        self.assertEqual(rows['id-2'], {'cardkingdom': 1.49, 'cardkingdom_foil': 3.99, 'cardkingdom_buy': 0.6, 'manapool': 1.2})

    def test_an_etched_price_is_its_own_column(self):
        self.assertEqual(self.rows()['id-1'], {'cardkingdom_etched': 5.99})

    def test_only_the_kept_shops(self):
        rows = self.rows()
        self.assertNotIn('id-3', rows)                    # TCGplayer and Cardmarket come from Scryfall's card data
        self.assertEqual(set(rows), {'id-1', 'id-2', 'id-zzz'})    # 'unmapped' is in neither identifier file

    def test_the_latest_date_counts(self):
        self.assertEqual(shop_prices.latest({'2026-10-23': 1.0, '2026-10-24': '1.5'}), 1.5)
        self.assertIsNone(shop_prices.latest({}))
        self.assertIsNone(shop_prices.latest({'2026-10-24': 'n/a'}))

    def test_the_reader_reads_what_json_reads(self):
        # One card at a time with raw_decode (memory) must give exactly what json.loads gives for the whole file
        compact = PRICE_FILE.replace('\n', '').replace(' ', '')
        for text in (PRICE_FILE, compact):
            self.assertEqual(dict(shop_prices.each_price(text)), json.loads(text)['data'])
        self.assertEqual(list(shop_prices.each_price('{"meta": {}, "data": {}}')), [])
        with self.assertRaises(ValueError):
            list(shop_prices.each_price('{"meta": {}}'))
        with self.assertRaises(ValueError):
            list(shop_prices.each_price('{"data": {"a": {}, '))           # cut short


class ShopPricesInTheCardData(CardData):
    """The `shop_prices` table: swapped in whole, kept across a card data download, a source like Scryfall's"""

    ROWS = {'id-2': {'cardkingdom': 1.49, 'cardkingdom_foil': 3.99, 'manapool': 1.2}, 'id-1': {'cardkingdom_etched': 5.99},
            'id-zzz': {'manapool': 9.0}, **{f'id-{n}': {'cardkingdom': 0.25} for n in range(100, 140)}}

    def setUp(self):
        super().setUp()
        self.cards.populate_database(FILLER + [ETCHED, BOTH])

    def shown(self, card_id):
        card = self.cards.get_card_by_id(card_id)
        return card['price'], card['price_foil'], card['price_etched']

    def test_nothing_to_choose_before_the_first_update(self):
        self.assertEqual(self.cards.priced_printings('cardkingdom_usd'), 0)
        self.assertEqual(self.cards.shop_priced_printings(), 0)

    def test_a_shop_shows_its_own_prices_only(self):
        self.assertEqual(self.cards.replace_shop_prices(self.ROWS, '2026-10-24'), 43)
        self.assertEqual(self.cards.get_data_info(shop_prices.INFO_KEY)['source_updated'], '2026-10-24')
        self.cards.set_price_source('cardkingdom_usd')
        self.assertEqual(self.shown('id-2'), (1.49, 3.99, 0.0))
        self.assertEqual(self.shown('id-1'), (0.0, 0.0, 5.99))
        self.cards.set_price_source('manapool_usd')
        self.assertEqual(self.shown('id-2'), (1.2, 0.0, 0.0))            # no Mana Pool foil price: none, never another shop's
        self.assertEqual(self.shown('id-1'), (0.0, 0.0, 0.0))
        # Counted among the card data's printings only ('id-zzz' is not in it)
        self.assertEqual(self.cards.priced_printings('manapool_usd'), 1)
        self.assertEqual(self.cards.priced_printings('cardkingdom_usd'), 42)

    def test_a_card_data_download_keeps_the_shops_prices(self):
        # The point of a table of their own: the download replaces `cards` whole
        self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        self.cards.set_price_source('cardkingdom_usd')
        self.cards.populate_database(FILLER + [ETCHED, {**BOTH, 'prices': {'usd': '9.00'}}])
        self.assertEqual(self.shown('id-2'), (1.49, 3.99, 0.0))          # applied to the new cards before their swap
        self.assertEqual(self.cards.shop_priced_printings(), 43)

    def test_new_prices_are_shown_at_once(self):
        self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        self.cards.set_price_source('cardkingdom_usd')
        self.cards.replace_shop_prices({**self.ROWS, 'id-2': {'cardkingdom': 1.99}}, '2026-10-25')
        self.assertEqual(self.shown('id-2'), (1.99, 0.0, 0.0))

    def test_a_failed_update_keeps_the_prices_in_use(self):
        self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        with self.assertRaises(RuntimeError):
            self.cards.replace_shop_prices({}, '2026-10-25')                              # nothing came
        with self.assertRaises(RuntimeError):
            self.cards.replace_shop_prices({'id-2': {'cardkingdom': 1.0}}, '2026-10-25')  # far fewer than in use
        self.assertEqual(self.cards.shop_priced_printings(), 43)
        self.assertEqual(self.cards.get_data_info(shop_prices.INFO_KEY)['source_updated'], '2026-10-24')

    def test_a_newer_version_s_column_is_added_at_start(self):
        # A shop or kind added to shop_prices.SHOPS: an existing file gets its column (empty), so every source's count works
        self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        self.cards.conn.execute('ALTER TABLE shop_prices DROP COLUMN manapool_etched')
        self.cards.conn.commit()
        self.cards.conn.close()
        self.cards = CardDatabase(db_file=self.tmp / 'cards.db')
        self.assertEqual(self.cards.priced_printings('manapool_usd'), 1)
        self.assertEqual(self.cards.shop_priced_printings(), 43)

    def test_a_refusal_says_what_was_refused(self):
        with self.assertRaisesRegex(RuntimeError, "The shops' prices were not replaced: shop_prices_import is empty"):
            self.cards.replace_shop_prices({}, '2026-10-24')

    def test_never_while_the_card_data_is_imported(self):
        self.cards._import_lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        finally:
            self.cards._import_lock.release()
        self.assertEqual(self.cards.shop_priced_printings(), 0)


class Response:
    """What requests.get answers, for the files of a fake MTGJSON"""

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.body)

    def iter_content(self, size):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ShopPriceImport(CardData):
    """shop_prices.import_prices: the three files of the day, and nothing downloaded while MTGJSON's date is the one in use"""

    def setUp(self):
        super().setUp()
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        self.asked = []
        self.date = '2026-10-24'
        pairs = CARD_PAIRS + [(f'filler-{n}', f'id-{n}') for n in range(100, 140)]
        # The plan's file plus a price for each filler card (replace_table refuses a table much smaller than the one in use)
        fillers = json.loads(mtgjson_prices(**{f'filler-{n}': {'cardkingdom': {'retail': {'normal': 0.25}}} for n in range(100, 140)}))
        self.data = {**json.loads(PRICE_FILE)['data'], **fillers['data']}
        self.file_date = None          # the price file's own date: None is Meta.json's (the two published apart)
        self.files = {
            'cardIdentifiers.csv.xz': identifiers(self.tmp / 'c.csv.xz', pairs).read_bytes(),
            'tokenIdentifiers.csv.xz': identifiers(self.tmp / 't.csv.xz', []).read_bytes(),
        }

    def get(self, url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        self.asked.append(name)
        if name == 'Meta.json':
            return Response(json.dumps({'meta': {'date': self.date}, 'data': {'date': self.date}}).encode())
        if name == 'AllPricesToday.json.xz':
            return Response(lzma.compress(json.dumps({'meta': {'date': self.file_date or self.date}, 'data': self.data}).encode()))
        return Response(self.files[name])

    def test_the_files_of_the_day_become_the_prices(self):
        self.assertEqual(shop_prices.import_prices(self.cards, get=self.get), ('2026-10-24', 43))
        self.cards.set_price_source('cardkingdom_usd')
        self.assertEqual(self.cards.get_card_by_id('id-2')['price_foil'], 3.99)

    def test_nothing_newer_downloads_nothing(self):
        shop_prices.import_prices(self.cards, get=self.get)
        self.asked.clear()
        with self.assertRaises(shop_prices.NothingNew):
            shop_prices.import_prices(self.cards, get=self.get)
        self.assertEqual(self.asked, ['Meta.json'])
        self.date = '2026-10-25'
        self.assertEqual(shop_prices.import_prices(self.cards, get=self.get)[0], '2026-10-25')

    def test_a_price_file_older_than_meta_says_is_not_taken_for_newer(self):
        # Meta.json already says the 25th, the price file is still the 24th's (published or cached apart): the 24th's date
        # is the one recorded - never the 25th for the 24th's prices, which made every later update "nothing new"
        # (a Codex review of 2026-10-24)
        shop_prices.import_prices(self.cards, get=self.get)                      # the 24th's, in use
        self.date, self.file_date = '2026-10-25', '2026-10-24'
        with self.assertRaises(shop_prices.NothingNew):                           # not newer than in use: nothing saved
            shop_prices.import_prices(self.cards, get=self.get)
        self.assertEqual(self.cards.get_data_info(shop_prices.INFO_KEY)['source_updated'], '2026-10-24')
        self.file_date = None                                                     # the 25th's file is out
        self.assertEqual(shop_prices.import_prices(self.cards, get=self.get)[0], '2026-10-25')

    def test_the_first_file_is_recorded_under_its_own_date(self):
        self.date, self.file_date = '2026-10-25', '2026-10-24'
        self.assertEqual(shop_prices.import_prices(self.cards, get=self.get)[0], '2026-10-24')
        self.file_date = None
        self.assertEqual(shop_prices.import_prices(self.cards, get=self.get)[0], '2026-10-25')   # Meta.json's differs: again

    def test_an_older_file_than_in_use_is_refused(self):
        self.date = '2026-10-25'
        shop_prices.import_prices(self.cards, get=self.get)
        self.date, self.file_date = '2026-10-26', '2026-10-23'
        with self.assertRaises(ValueError):
            shop_prices.import_prices(self.cards, get=self.get)
        self.assertEqual(self.cards.get_data_info(shop_prices.INFO_KEY)['source_updated'], '2026-10-25')

    def test_a_file_without_a_date_is_refused(self):
        self.assertEqual(shop_prices.file_date('{"meta":{"date":"2026-10-23","version":"5.3.0"},"data":{}}'), '2026-10-23')
        with self.assertRaises(ValueError):
            shop_prices.file_date('{"meta":{},"data":{}}')


class ShopPriceApplicationFails(ShopPriceImport):
    """Applying a shop's new prices fails: the table and its date stay as they were, so the next update tries again
    (a Codex review of 2026-10-24: they were swapped in first, and the next update found "today's prices are in use")"""

    def test_the_swap_and_the_application_are_one(self):
        self.cards.set_price_source('cardkingdom_usd')
        shop_prices.import_prices(self.cards, get=self.get)
        self.date = '2026-10-25'
        self.data = {
            **{f'filler-{n}': {'paper': {'cardkingdom': {'retail': {'normal': {'d': 0.5}}}}} for n in range(100, 140)},
            'front': {'paper': {'cardkingdom': {'retail': {'normal': {'d': 2.49}}}}}}
        real = self.cards._reapply_in
        def failing(cursor, *args, **kwargs):
            real(cursor, *args, **kwargs)
            raise OSError('disk full')                     # after the UPDATE: it must be rolled back with the swap
        self.cards._reapply_in = failing
        with self.assertRaises(OSError):
            shop_prices.import_prices(self.cards, get=self.get)
        self.assertEqual(self.cards.get_data_info(shop_prices.INFO_KEY)['source_updated'], '2026-10-24')
        self.assertEqual(self.cards.get_card_by_id('id-2')['price'], 1.49)          # yesterday's, as shown before
        self.cards._reapply_in = real
        self.assertEqual(shop_prices.import_prices(self.cards, get=self.get)[0], '2026-10-25')   # not "nothing new"
        self.assertEqual(self.cards.get_card_by_id('id-2')['price'], 2.49)


class CurrencyChange(PricedAgain):
    """Game.price_changes: a change of currency takes the price away from what the new source cannot price"""

    def test_another_currency_leaves_no_old_number_behind(self):
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        both = self.add(self.collection, 'id-2', 'regular')            # has a euro price
        etched = self.add(self.collection, 'id-1', 'foil')             # has none (Scryfall gives no euro etched price)
        csv_entry = self.collection.add_card({'card_id': None, 'name': 'Card 9', 'set_name': 'Test', 'set_code': 'tst',
                                              'number': '9', 'price': 7.0}, GAME, 'regular', quiet=True)
        self.cards.set_price_source('scryfall_eur')
        new_prices, cleared = self.game.price_changes(self.collection, new_source=True, new_currency=True)
        self.assertEqual(set(cleared), {etched, csv_entry})
        self.collection.reprice(new_prices)
        self.assertEqual(self.collection.clear_prices(cleared), 2)
        self.assertEqual(self.price(self.collection, both), 0.9)
        self.assertIsNone(self.price(self.collection, etched))
        self.assertIsNone(self.price(self.collection, csv_entry))
        self.assertEqual(self.collection.get_stats(GAME)['total_value'], 0.9)

    def test_another_shop_in_the_same_currency_clears_only_what_knows_its_printing(self):
        # A shop without a price for a printing: none, never the previous shop's (market value); a card imported without
        # its printing keeps what it came with - no shop can price it, and its dollars are still dollars
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        etched = self.add(self.collection, 'id-1', 'foil')
        both = self.add(self.collection, 'id-2', 'regular')
        csv_entry = self.collection.add_card({'card_id': None, 'name': 'Card 9', 'set_name': 'Test', 'set_code': 'tst',
                                              'number': '9', 'price': 7.0}, GAME, 'regular', quiet=True)
        self.cards.replace_shop_prices({'id-2': {'manapool': 0.8}, **{f'id-{n}': {'manapool': 0.1} for n in range(100, 140)}}, '2026-10-24')
        self.cards.set_price_source('manapool_usd')
        new_prices, cleared = self.game.price_changes(self.collection, new_source=True)
        self.assertEqual(set(cleared), {etched})
        self.collection.reprice(new_prices)
        self.collection.clear_prices(cleared)
        self.assertEqual((self.price(self.collection, both), self.price(self.collection, etched), self.price(self.collection, csv_entry)),
                         (0.8, None, 7.0))

    def test_a_new_day_of_the_same_source_keeps_them(self):
        # No change of source: a printing without a price today keeps its own (test_no_price_today_keeps_the_old_one)
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        self.add(self.collection, 'id-1', 'foil')
        self.cards.set_price_source('scryfall_eur')
        self.assertEqual(self.game.price_changes(self.collection)[1], {})

    def test_an_edit_made_meanwhile_keeps_its_price(self):
        self.cards.populate_database(FILLER + [ETCHED, BOTH])
        etched = self.add(self.collection, 'id-1', 'foil')
        self.cards.set_price_source('scryfall_eur')
        cleared = self.game.price_changes(self.collection, new_source=True, new_currency=True)[1]
        self.collection.conn.execute("UPDATE inventory SET finish = 'regular' WHERE id = ?", (etched,))
        self.collection.conn.commit()
        self.assertEqual(self.collection.clear_prices(cleared), 0)


class ActivityLog(CardData):
    def test_the_added_line_says_the_currency(self):
        lines = []
        self.collection.log_callback = lambda message, level='info': lines.append(message)
        self.collection.price_symbol = lambda: '€'
        self.collection.add_card({'card_id': 'x', 'name': 'Card 2', 'set_name': 'Test', 'set_code': 'tst', 'number': '2',
                                  'price': 0.9}, GAME, 'regular')
        self.assertTrue(any('€0.90 for 1' in line for line in lines), lines)


if __name__ == '__main__':
    unittest.main()


# ----------------------------------------------------------------------------
# What a shop would pay (phase 4): Card Kingdom's buylist beside the value
# ----------------------------------------------------------------------------

class TheBuylist(CardData):
    """Game.buylist_prices: the finish picks the buy price, never the other finish's; nothing when there is none"""

    # 'id-2' is bought in both finishes, 'id-1' (etched only) only as an etched foil, 'id-3' only as a regular copy
    ROWS = {'id-2': {'cardkingdom': 1.49, 'cardkingdom_buy': 0.60, 'cardkingdom_buy_foil': 1.80},
            'id-1': {'cardkingdom_buy_etched': 2.25}, 'id-3': {'cardkingdom_buy': 0.05},
            **{f'id-{n}': {'cardkingdom': 0.25} for n in range(100, 140)}}

    def setUp(self):
        super().setUp()
        self.cards.populate_database(FILLER + [ETCHED, BOTH, UNPRICED])

    def entry(self, number, card_id, finish):
        return {'id': number, 'card_id': card_id, 'finish': finish}

    def test_nothing_before_the_first_price_update(self):
        self.assertEqual(self.game.buylist_prices([self.entry(1, 'id-2', 'regular')]), {})

    def test_each_finish_takes_its_own_buy_price(self):
        self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        found = self.game.buylist_prices([
            self.entry(1, 'id-2', 'regular'), self.entry(2, 'id-2', 'foil'), self.entry(3, 'id-2', 'surge'),
            self.entry(4, 'id-1', 'foil'),        # an etched foil is a 'foil' copy: the etched buy price
            self.entry(5, 'id-3', 'foil'),        # bought only as a regular copy: no offer for a foil, never the regular one
            self.entry(6, 'id-1', 'regular'),     # no regular buy price for an etched-only printing
            self.entry(7, 'id-100', 'regular'),   # a shop sells it, nobody buys it: no offer
            self.entry(8, None, 'regular'),       # an entry imported without its printing
            self.entry(9, 'id-gone', 'regular'),  # a printing that left the card data
        ])
        self.assertEqual(found, {1: 0.60, 2: 1.80, 3: 1.80, 4: 2.25})

    def test_the_offers_say_their_day(self):
        # Kept until the next price update replaces them: the page needs their date, never "today"
        self.assertIsNone(self.game.buylist_date())
        self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        self.assertEqual(self.game.buylist_date(), '2026-10-24')
        self.cards.replace_shop_prices(self.ROWS, '2026-10-25')
        self.assertEqual(self.game.buylist_date(), '2026-10-25')

    def test_it_does_not_follow_the_price_source(self):
        # Not a price source: what Card Kingdom pays is the same whichever value the collection is shown in
        self.cards.replace_shop_prices(self.ROWS, '2026-10-24')
        self.cards.set_price_source('scryfall_eur')
        self.assertEqual(self.game.buylist_prices([self.entry(1, 'id-2', 'foil')]), {1: 1.80})
