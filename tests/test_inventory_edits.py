#!/usr/bin/env python3
"""
An edit of an inventory is one transaction: a method that calls others commits once, at the end,
and a failure anywhere leaves nothing of it (core/inventory.py: @_writes, storage.Transactional).

Run:  venv/bin/python tests/test_inventory_edits.py        (or with the others: -m unittest discover tests)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ownership import CARD, GAME, OTHER, TemporaryData  # noqa: E402


class EditsAreWhole(TemporaryData):
    def test_a_bulk_edit_that_fails_half_way_changes_nothing(self):
        first = self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        second = self.collection.add_card(OTHER, GAME, 'regular', quiet=True)
        real = self.collection.update_card
        calls = []

        def failing(row_id, **kwargs):
            calls.append(row_id)
            if len(calls) == 2:
                raise RuntimeError('boom')
            return real(row_id, **kwargs)

        with mock.patch.object(self.collection, 'update_card', failing):
            with self.assertRaises(RuntimeError):
                self.collection.bulk_update([first, second], 'condition', 'Played')
        self.assertEqual({c['condition'] for c in self.collection.get_all_cards(GAME)}, {'Near Mint'})
        self.assertFalse(self.collection.conn.in_transaction)

    def test_a_delete_that_fails_after_its_rows_went_keeps_row_and_photo(self):
        row = self.add('s1')
        photos = set(self.tmp.joinpath('captures').glob('*'))
        real = self.scan._delete_entries

        def deleted_then_fails(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('boom')

        with mock.patch.object(self.scan, '_delete_entries', deleted_then_fails):
            with self.assertRaises(RuntimeError):
                self.scan.delete_card(row)
        self.assertEqual(set(self.tmp.joinpath('captures').glob('*')), photos)  # files outlive a rollback
        self.assertEqual(self.total(), 1)

    def test_the_files_of_a_deleted_entry_go_after_the_commit(self):
        row = self.add('s1')
        self.assertTrue(self.scan.delete_card(row))
        self.assertEqual(list(self.tmp.joinpath('captures').glob('*')), [])


if __name__ == '__main__':
    unittest.main()
