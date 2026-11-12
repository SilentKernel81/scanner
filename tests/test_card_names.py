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
from card_scanner.core import inventory as inv  # noqa: E402
from card_scanner.core.others import Others  # noqa: E402
from card_scanner.core.trades import Trades  # noqa: E402
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

    def test_an_edit_made_meanwhile_is_not_undone(self):
        # The person gives the entry another printing between the run's first look and its change - one copy, so the
        # entry keeps its row: the change reads it again under the lock, sees it is no longer the renamed printing, and
        # leaves it. Before, the printing read at the first look was written back over the person's
        self.collection.add_card(SLD, GAME, 'regular', quantity=1, quiet=True)
        row_id = self.collection.conn.execute('SELECT id FROM inventory').fetchone()[0]
        real_stale = card_names._stale

        def stale_then_an_edit(conn, table, names, columns):
            found = real_stale(conn, table, names, columns)
            if table == 'inventory':
                other = {c: None for c in self.collection.PRINTING_COLUMNS}
                other.update(card_id='ajani-m10', card_name=NEW, set_name='Magic 2010', set_code='m10', card_number='1')
                self.collection.update_card(row_id, printing=other, quiet=True)
            return found
        from unittest import mock
        with mock.patch.object(card_names, '_stale', stale_then_an_edit):
            counts = card_names.adopt(RENAMED, inventories=(self.collection,))
        self.assertEqual(counts['entries'], 0)
        rows = [tuple(r) for r in self.collection.conn.execute('SELECT id, card_id, set_name, quantity FROM inventory')]
        self.assertEqual(rows, [(row_id, 'ajani-m10', 'Magic 2010', 1)])    # the person's edit stands

    def test_an_entry_a_trade_holds_is_renamed_in_place(self):
        # Its row stays (the name changes in it): the trade's hold on its copies is not in the way
        self.collection.add_card(SLD, GAME, 'regular', quantity=3, quiet=True)
        row_id = self.collection.conn.execute('SELECT id FROM inventory').fetchone()[0]
        self.collection.conn.execute("CREATE TRIGGER no_delete BEFORE DELETE ON inventory BEGIN SELECT RAISE(ABORT, 'held'); END")
        counts = card_names.adopt(RENAMED, inventories=(self.collection,))
        self.assertEqual((counts['entries'], counts['waiting']), (1, []))
        self.assertEqual(tuple(self.collection.conn.execute('SELECT id, card_name, quantity FROM inventory').fetchone()), (row_id, NEW, 3))

    def test_a_held_entry_that_would_be_merged_waits_and_the_others_still_change(self):
        # Its twin exists (merging deletes it, which the hold refuses): it waits, said in 'waiting'; another entry changes
        self.collection.add_card(SLD, GAME, 'regular', quantity=2, quiet=True)
        self.collection.add_card(dict(SLD, name=NEW), GAME, 'regular', quiet=True)
        self.collection.add_card(dict(SLD, number='9'), GAME, 'foil', quiet=True)       # another entry of the printing
        self.collection.conn.execute(f"CREATE TRIGGER no_delete BEFORE DELETE ON inventory WHEN OLD.card_name = '{OLD}' "
                                     "AND OLD.finish = 'regular' BEGIN SELECT RAISE(ABORT, 'set aside for a trade'); END")
        counts = card_names.adopt(RENAMED, inventories=(self.collection,))
        self.assertEqual((counts['entries'], counts['kept']), (1, 1))
        self.assertEqual([(w['name'], w['new_name']) for w in counts['waiting']], [(OLD, NEW)])
        self.assertIn('set aside for a trade', counts['waiting'][0]['why'])
        self.assertEqual(self.names_in(self.collection), [(NEW, 1), (NEW, 1), (OLD, 2)])   # nothing half done
        # The trade lets go: the next try merges it
        self.collection.conn.execute('DROP TRIGGER no_delete')
        self.assertEqual(card_names.adopt(RENAMED, inventories=(self.collection,))['waiting'], [])
        self.assertEqual(self.names_in(self.collection), [(NEW, 1), (NEW, 3)])

    def test_one_part_failing_keeps_neither_the_rest_from_changing_nor_what_waits_from_being_said(self):
        # The decks fail (their file, a table): the collection's entry still changes, the held one is still said as
        # waiting, the scanned cards and the others are still done - and the failure is in 'errors' for the caller to retry
        self.collection.add_card(SLD, GAME, 'regular', quantity=2, quiet=True)
        self.collection.add_card(dict(SLD, name=NEW), GAME, 'regular', quiet=True)
        self.collection.conn.execute("CREATE TRIGGER no_delete BEFORE DELETE ON inventory BEGIN SELECT RAISE(ABORT, 'held'); END")
        self.scan.add_card(SLD, GAME, 'regular', quiet=True)
        deck = self.decks.create(GAME, 'Ajani', 'standard')
        self.decks.set_card(deck, OLD, 'main', 1, card_id='ajani-sld')
        self.decks.conn.execute("CREATE TRIGGER decks_fail BEFORE UPDATE ON deck_cards BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END")
        counts = card_names.adopt(RENAMED, inventories=(self.collection, self.scan), decks=self.decks)
        self.assertEqual((counts['entries'], counts['deck_cards']), (1, 0))                # the scanned card changed
        self.assertEqual([(w['inventory'], w['name']) for w in counts['waiting']], [('collection.db', OLD)])
        self.assertEqual(len(counts['errors']), 1)
        self.assertIn('decks: disk I/O error', counts['errors'][0])
        self.assertEqual(self.names_in(self.scan), [(NEW, 1)])
        # The deck's entry is as it was (its transaction went back whole)
        self.assertEqual(self.decks.conn.execute('SELECT card_name FROM deck_cards').fetchone()[0], OLD)

    def test_with_a_real_trade_the_held_entry_is_renamed_in_place_and_a_twin_waits_for_the_trade(self):
        # The trades' own triggers (TRADE_HELD), not a stand-in: three copies set aside for Jo
        trades = Trades(self.collection)
        held = self.collection.add_card(SLD, GAME, 'regular', quantity=3, quiet=True)
        trade = trades.add(GAME, 'Jo', [held])['trade']
        counts = card_names.adopt(RENAMED, inventories=(self.collection,))
        self.assertEqual((counts['entries'], counts['waiting']), (1, []))
        self.assertEqual(self.names_in(self.collection), [(NEW, 3)])
        self.assertEqual(trades.by_entry(GAME)[held], [{'id': trade, 'name': 'Jo', 'quantity': 3}])   # still Jo's, same entry
        # Now an entry that would have to be merged into its twin while the trade holds it: it waits, nothing half done
        self.collection.conn.execute('UPDATE inventory SET card_name = ? WHERE id = ?', (OLD, held))
        self.collection.conn.commit()
        self.collection.add_card(dict(SLD, name=NEW), GAME, 'regular', quiet=True)
        counts = card_names.adopt(RENAMED, inventories=(self.collection,))
        self.assertEqual((counts['entries'], len(counts['waiting'])), (0, 1))
        self.assertIn(inv.TRADE_HELD, counts['waiting'][0]['why'])
        self.assertFalse(self.collection.conn.in_transaction)
        self.assertEqual(self.names_in(self.collection), [(NEW, 1), (OLD, 3)])
        self.assertEqual(trades.by_entry(GAME)[held][0]['quantity'], 3)
        # The trade is cancelled: the next try merges the entry into its twin
        trades.cancel(trade)
        self.assertEqual(card_names.adopt(RENAMED, inventories=(self.collection,))['waiting'], [])
        self.assertEqual(self.names_in(self.collection), [(NEW, 4)])

    def test_nothing_renamed_changes_nothing(self):
        self.collection.add_card(SLD, GAME, 'regular', quiet=True)
        self.assertEqual(card_names.adopt({}, inventories=(self.collection,), decks=self.decks),
                         {'entries': 0, 'deck_cards': 0, 'covers': 0, 'others': 0, 'kept': 0, 'waiting': [], 'errors': []})
        self.assertEqual(self.names_in(self.collection), [(OLD, 1)])


if __name__ == '__main__':
    unittest.main()
