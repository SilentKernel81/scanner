"""
Prices (docs/plans/PRICE_SOURCES_PLAN.md, phase 1): Scryfall's etched and EUR prices kept by the card data import, the
etched price used for an etched foil, and the collection's entries priced again from new card data. Everything runs on
temporary databases; no application data is touched.

    venv/bin/python -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

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


if __name__ == '__main__':
    unittest.main()
