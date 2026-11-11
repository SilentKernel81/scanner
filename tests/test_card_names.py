"""
core/card_names.py: what is kept by a card's name follows its printing when the card data renames it (the reversible
printings since 2026-11-11: "Ajani Goldmane // Ajani Goldmane" is "Ajani Goldmane"). On temporary databases.

    venv/bin/python -m unittest discover tests -p test_card_names.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from card_scanner.core import card_names  # noqa: E402
from card_scanner.core.database import search_key  # noqa: E402
from card_scanner.core.others import Others  # noqa: E402
from test_ownership import CARD, GAME, TemporaryData  # noqa: E402

OLD, NEW = 'Ajani Goldmane // Ajani Goldmane', 'Ajani Goldmane'
SLD = dict(CARD, card_id='ajani-sld', name=OLD, set_name='Secret Lair Drop', set_code='sld', number='1504')
RENAMED = {'ajani-sld': NEW}


class Adopting(TemporaryData):

    def names_in(self, store):
        return sorted((row['card_name'], row['quantity']) for row in store.conn.execute('SELECT card_name, quantity FROM inventory'))

    def test_an_entry_kept_by_the_old_name_takes_the_new_one_and_is_counted_with_the_card(self):
        self.collection.add_card(SLD, GAME, 'regular', quantity=2, quiet=True)
        self.collection.add_card(dict(CARD, card_id='other', name='Shock', number='9'), GAME, 'regular', quiet=True)
        counts = card_names.adopt(RENAMED, inventories=(self.collection, self.scan))
        self.assertEqual(counts['entries'], 1)
        self.assertEqual(self.names_in(self.collection), [(NEW, 2), ('Shock', 1)])
        self.assertEqual(self.collection.owned_by_name(GAME).get(search_key(NEW)), 2)
        self.assertNotIn(search_key(OLD), self.collection.owned_by_name(GAME))
        # A second run has nothing left to do
        self.assertEqual(card_names.adopt(RENAMED, inventories=(self.collection,))['entries'], 0)

    def test_an_entry_that_becomes_another_ones_twin_is_merged_into_it(self):
        # The same printing added again since, under the new name: one entry of 3, not two
        self.collection.add_card(SLD, GAME, 'regular', quantity=2, quiet=True)
        self.collection.add_card(dict(SLD, name=NEW), GAME, 'regular', quiet=True)
        card_names.adopt(RENAMED, inventories=(self.collection,))
        self.assertEqual(self.names_in(self.collection), [(NEW, 3)])

    def test_a_printing_not_renamed_keeps_its_name(self):
        # A double-faced token named twice over on purpose ("Angel // Angel"): not in the renamed printings, untouched
        token = dict(CARD, card_id='angel-token', name='Angel // Angel', number='3')
        self.collection.add_card(token, GAME, 'regular', quiet=True)
        card_names.adopt(RENAMED, inventories=(self.collection,))
        self.assertEqual(self.names_in(self.collection), [('Angel // Angel', 1)])

    def test_deck_entries_take_the_new_name_join_its_entry_and_keep_their_cover(self):
        deck = self.decks.create(GAME, 'Ajani', 'standard')
        self.decks.set_card(deck, OLD, 'main', 2, card_id='ajani-sld')
        self.decks.set_card(deck, NEW, 'main', 1, card_id='ajani-m10')        # the deck has the card under both names
        self.decks.set_card(deck, OLD, 'side', 1, card_id='ajani-sld')
        self.decks.update(deck, cover=OLD)
        counts = card_names.adopt(RENAMED, decks=self.decks)
        self.assertEqual((counts['deck_cards'], counts['covers']), (2, 1))
        rows = sorted(tuple(row) for row in self.decks.conn.execute('SELECT card_name, board, quantity FROM deck_cards WHERE deck_id = ?', (deck,)))
        self.assertEqual(rows, [(NEW, 'main', 3), (NEW, 'side', 1)])
        self.assertEqual(self.decks.conn.execute('SELECT cover FROM decks WHERE id = ?', (deck,)).fetchone()[0], NEW)

    def test_other_peoples_cards_take_the_new_name(self):
        others = Others(db_file=self.tmp / 'others.db')
        self.addCleanup(others.conn.close)
        collection = others.conn.execute("INSERT INTO other_collections (game, name, imported_at) VALUES ('mtg', 'Friend', 'now')").lastrowid
        others.conn.execute("INSERT INTO other_cards (collection_id, key, card_id, card_name, quantity, finish) VALUES (?, 'k1', 'ajani-sld', ?, 1, 'regular')",
                            (collection, OLD))
        others.conn.commit()
        self.assertEqual(card_names.adopt(RENAMED, others=others)['others'], 1)
        self.assertEqual(others.conn.execute('SELECT card_name, key FROM other_cards').fetchone()[:], (NEW, 'k1'))

    def test_nothing_renamed_changes_nothing(self):
        self.collection.add_card(SLD, GAME, 'regular', quiet=True)
        self.assertEqual(card_names.adopt({}, inventories=(self.collection,), decks=self.decks), dict.fromkeys(
            ('entries', 'deck_cards', 'covers', 'others', 'kept'), 0))
        self.assertEqual(self.names_in(self.collection), [(OLD, 1)])


if __name__ == '__main__':
    unittest.main()
