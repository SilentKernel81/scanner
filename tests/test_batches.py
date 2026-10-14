#!/usr/bin/env python3
"""
Which copies of an entry came together (inventory_batches): kept through splits, merges,
quantity changes, undo, moves, imports, backups - and the migration of a file from before.

Run:  venv/bin/python tests/test_batches.py        (or with the others: -m unittest discover tests)
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ownership import CARD, GAME, OTHER, TemporaryData  # noqa: E402

from card_scanner.core import backups  # noqa: E402
from card_scanner.core import inventory as inv  # noqa: E402


class Batches(TemporaryData):
    def parts(self, manager=None, name=None):
        """{batch: copies} of the entries (of one card)"""
        manager = manager or self.collection
        found = {}
        for card in manager.get_all_cards(GAME):
            if name and card['name'] != name:
                continue
            for part in card['batches']:
                found[part['batch']] = found.get(part['batch'], 0) + part['quantity']
        return found

    def consistent(self, manager=None):
        """No entry's batches add up to more than it has"""
        manager = manager or self.collection
        over = manager.conn.execute('''SELECT i.id FROM inventory i JOIN inventory_batches b ON b.inventory_id = i.id
                                       GROUP BY i.id HAVING SUM(b.quantity) > i.quantity''').fetchall()
        self.assertEqual(over, [])
        self.assertEqual(manager.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_each_add_is_a_batch_and_merges_keep_them_apart(self):
        self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True)
        self.collection.add_card(CARD, GAME, 'regular', quantity=3, quiet=True)
        card = self.collection.get_all_cards(GAME)[0]
        self.assertEqual((card['quantity'], sorted(p['quantity'] for p in card['batches'])), (5, [2, 3]))
        self.assertEqual(card['added_quantity'], card['batches'][0]['quantity'])
        self.consistent()

    def test_one_batch_for_cards_that_come_in_together(self):
        batch = self.collection.new_batch()
        self.collection.add_card(CARD, GAME, 'regular', quiet=True, batch=batch)
        self.collection.add_card(OTHER, GAME, 'regular', quantity=2, quiet=True, batch=batch)
        self.assertEqual(self.parts(), {batch[0]: 3})
        removed = self.collection.remove_batch(GAME, batch[0])
        self.assertEqual((removed['entries'], removed['cards']), (2, 3))
        self.assertEqual(self.collection.get_all_cards(GAME), [])

    def test_removing_a_batch_leaves_older_copies(self):
        self.collection.add_card(CARD, GAME, 'regular', quantity=3, quiet=True)
        newer = self.collection.new_batch()
        self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True, batch=newer)
        self.assertEqual(self.collection.remove_batch(GAME, newer[0])['cards'], 2)
        card = self.collection.get_all_cards(GAME)[0]
        self.assertEqual((card['quantity'], sum(p['quantity'] for p in card['batches'])), (3, 3))
        self.assertEqual(self.collection.remove_batch(GAME, newer[0]), {'entries': 0, 'cards': 0})  # not twice
        self.consistent()

    def test_a_split_and_a_merge_carry_the_batch_with_the_copies(self):
        old = self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True)
        newer = self.collection.new_batch()
        self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True, batch=newer)
        self.collection.update_card(old, location='Box', split_quantity=3, quiet=True)  # the newest 3 move
        by_location = {c['location']: {p['batch']: p['quantity'] for p in c['batches']}
                       for c in self.collection.get_all_cards(GAME)}
        self.assertEqual((by_location['Box'][newer[0]], sum(by_location['Box'].values())), (2, 3))
        self.assertEqual(sum(by_location[''].values()), 1)
        # moved back: merged into the entry that is there
        box = next(c for c in self.collection.get_all_cards(GAME) if c['location'] == 'Box')
        self.collection.update_card(box['id'], location='', split_quantity=3, quiet=True)
        self.assertEqual(self.collection.get_all_cards(GAME)[0]['quantity'], 4)
        self.assertEqual(sum(self.parts().values()), 4)
        self.assertEqual(self.parts()[newer[0]], 2)
        self.consistent()

    def test_a_lowered_quantity_takes_the_newest_batch_first(self):
        old = self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True)
        newer = self.collection.new_batch()
        self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True, batch=newer)
        self.collection.update_card(old, quantity=3, quiet=True)
        parts = self.parts()
        self.assertEqual((parts.get(newer[0]), sum(parts.values())), (1, 3))
        self.consistent()

    def test_undo_takes_back_its_own_batch(self):
        self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True)
        added = self.collection.add_card(CARD, GAME, 'regular', quantity=1, quiet=True)
        self.assertEqual(self.collection.undo_last_add(), 'Test Card')
        card = self.collection.get_all_cards(GAME)[0]
        self.assertEqual((card['id'], card['quantity'], sum(p['quantity'] for p in card['batches'])), (added, 2, 2))
        self.consistent()

    def test_a_deleted_entry_takes_its_batches(self):
        row = self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        self.collection.delete_card(row, quiet=True)
        self.assertEqual(self.collection.conn.execute('SELECT COUNT(*) FROM inventory_batches').fetchone()[0], 0)

    def test_the_move_to_the_collection_is_one_batch_even_with_cameras(self):
        self.add('desk-a', copies=2)
        self.add('desk-b', card=OTHER)
        self.collection.take_from(self.scan, GAME)
        self.assertEqual(list(self.parts().values()), [3])
        self.assertEqual(self.scan.conn.execute('SELECT COUNT(*) FROM inventory_batches').fetchone()[0], 0)
        # one camera's cards only: its copies are the batch
        self.add('desk-a', copies=1)
        self.add('desk-b', copies=1)
        self.collection.take_from(self.scan, GAME, station='desk-a')
        self.assertEqual(sorted(self.parts().values()), [1, 3])
        self.consistent()
        self.consistent(self.scan)

    def test_an_import_is_one_batch(self):
        stats = self.collection.import_entries(
            [{'fields': CARD, 'quantity': 2, 'finish': 'regular'}, {'fields': OTHER, 'quantity': 1, 'finish': 'regular'}], GAME)
        self.assertEqual(stats['added'], 2)
        self.assertEqual(list(self.parts().values()), [3])

    def test_a_restore_of_a_backup_from_before_batches_gives_each_entry_its_own(self):
        self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True)
        backup_id = backups.create(self.collection, self.scan, self.decks, note='x')['id']
        old = sqlite3.connect(str(backups.BACKUPS_DIR / backup_id / 'backup.db'))
        old.execute('DROP TABLE collection_inventory_batches')
        old.execute('DROP TABLE scanned_inventory_batches')
        old.commit()
        old.close()
        self.collection.add_card(OTHER, GAME, 'regular', quiet=True)
        backups.restore(backup_id, self.collection, self.scan, self.decks)
        card = self.collection.get_all_cards(GAME)[0]
        self.assertEqual((card['quantity'], [p['quantity'] for p in card['batches']]), (2, [2]))
        self.consistent()

    def test_a_restore_brings_back_the_batches_of_that_moment(self):
        self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True)
        newer = self.collection.new_batch()
        self.collection.add_card(CARD, GAME, 'regular', quantity=1, quiet=True, batch=newer)
        backup_id = backups.create(self.collection, self.scan, self.decks, note='x')['id']
        self.collection.remove_batch(GAME, newer[0])
        backups.restore(backup_id, self.collection, self.scan, self.decks)
        self.assertEqual(self.parts()[newer[0]], 1)
        self.consistent()


class PartlyTracked(TemporaryData):
    """Entries migrated from before batches: only some of their copies are in a batch (the newest;
    the others were there before). Copies that leave go newest first, so they leave the batch"""

    def entry(self):
        row = self.collection.add_card(CARD, GAME, 'regular', quantity=3, quiet=True)
        batch = self.collection.new_batch()
        # One of the three copies is the batch's; the other two are older and in none
        self.collection.conn.execute('UPDATE inventory_batches SET batch = ?, quantity = 1 WHERE inventory_id = ?',
                                     (batch[0], row))
        self.collection.conn.commit()
        return row, batch[0]

    def batches(self, row):
        return [tuple(r) for r in self.collection.conn.execute(
            'SELECT batch, quantity FROM inventory_batches WHERE inventory_id = ?', (row,))]

    def test_a_lowered_quantity_takes_the_tracked_copy_before_the_older_ones(self):
        row, batch = self.entry()
        self.collection.update_card(row, quantity=2, quiet=True)
        self.assertEqual(self.batches(row), [])
        self.assertEqual(self.collection.remove_batch(GAME, batch), {'entries': 0, 'cards': 0})
        self.assertEqual(self.collection.get_all_cards(GAME)[0]['quantity'], 2)  # the older copies stay

    def test_a_merge_that_gives_copies_up_does_the_same(self):
        row, batch = self.entry()
        twin = self.collection.add_card(CARD, GAME, 'regular', condition='Played', quantity=1, quiet=True)
        self.collection.update_card(row, quantity=2, condition='Played', quiet=True)  # lowered, and merged into the twin
        self.assertEqual(self.collection.get_all_cards(GAME)[0]['quantity'], 3)
        self.assertEqual(self.collection.get_all_cards(GAME)[0]['id'], twin)
        self.assertNotIn(batch, {b for b, _ in self.batches(twin)})
        self.assertEqual(self.collection.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_copies_taken_out_by_undo_or_a_trade_do_not_take_a_second_batch(self):
        row, batch = self.entry()
        again = self.collection.new_batch()
        self.collection.add_card(CARD, GAME, 'regular', quantity=1, quiet=True, batch=again)
        self.collection.undo_last_add()                       # its own batch goes, not the older one too
        self.assertEqual(self.batches(row), [(batch, 1)])
        self.collection.captured_copy('missing-key', 'desk-a')  # nothing to take: no change either
        self.assertEqual(self.batches(row), [(batch, 1)])


class Migration(TemporaryData):
    def test_a_file_from_before_gets_the_batch_each_entry_knew_of(self):
        path = self.tmp / 'old.db'
        first = inv.InventoryManager(db_file=path)
        first.add_card(CARD, GAME, 'regular', quantity=3, quiet=True, when='2026-01-01 10:00:00')
        first.add_card(OTHER, GAME, 'regular', quantity=2, quiet=True)
        # As a file made before the table: no table, the entry's own added_at / added_quantity
        first.conn.execute('DROP TABLE inventory_batches')
        first.conn.execute("DELETE FROM schema_migrations WHERE name = ?", (inv.BATCHES_MIGRATION,))
        first.conn.execute("UPDATE inventory SET added_quantity = 1 WHERE card_name = 'Test Card'")
        first.conn.commit()
        first.conn.close()
        second = inv.InventoryManager(db_file=path)
        try:
            batches = {c['name']: [(p['quantity'], p['added_at']) for p in c['batches']] for c in second.get_all_cards(GAME)}
            self.assertEqual([quantity for quantity, _ in batches['Test Card']], [1])    # of 3: the rest was there before
            self.assertEqual([quantity for quantity, _ in batches['Other Card']], [2])
            self.assertTrue(second.conn.execute("SELECT batch FROM inventory_batches WHERE batch LIKE 'old:%'").fetchone())
            second.conn.close()
            third = inv.InventoryManager(db_file=path)  # once: a second start adds nothing
            self.assertEqual(third.conn.execute('SELECT COUNT(*) FROM inventory_batches').fetchone()[0], 2)
            third.conn.close()
        finally:
            pass


if __name__ == '__main__':
    unittest.main()
