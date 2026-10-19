"""
"Send to review" (inventory.py: review_candidate, release_for_review; server/inventory_api.py:
send_inventory_card_to_review): one copy of an entry goes back to the review queue with its
capture. Storage level, on temporary databases: which copy, what is left, the phone's answer
about its capture afterwards, and a trade that holds the copies.

    venv/bin/python -m unittest discover tests -p test_send_to_review.py

The route itself is tried in test_capture_sync.py (SyncRoute: send_to_review_*).
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_ownership: the temporary data

from card_scanner.core import inventory as inv  # noqa: E402
from test_ownership import CARD, GAME, TemporaryData  # noqa: E402


class SendCopyBack(TemporaryData):
    def scan_card(self, key, station='desk-a'):
        return self.scan.add_card(CARD, GAME, 'regular', source=station, capture=self.photo, capture_key=key, quiet=True)

    def test_one_copy_goes_with_its_photo_and_its_station_count(self):
        row_id = self.scan_card('first')
        self.scan_card('second')
        candidate = self.scan.review_candidate(row_id)
        self.assertEqual(candidate['capture_key'], 'second')       # the newest capture, as a lowered quantity drops
        self.assertTrue(candidate['file'].exists())                # a picture the review can copy
        self.scan.release_for_review(candidate)
        self.assertEqual((self.total('desk-a'), self.total()), (1, 1))
        self.assertEqual(self.scan.captured_copy('first', 'desk-a')[0], 'scanned')

    def test_a_copy_that_moved_to_another_entry_meanwhile_is_not_released(self):
        row_id = self.scan_card('first')
        self.scan_card('second')
        candidate = self.scan.review_candidate(row_id)         # the entry's newest copy: 'second'
        self.scan.update_card(row_id, location='Box 1', split_quantity=1, quiet=True)  # it splits off
        before = self.cards()
        with self.assertRaises(ValueError):
            self.scan.release_for_review(candidate)
        self.assertEqual(self.cards(), before)                 # nothing was taken from either entry
        self.assertEqual(self.total(), 2)

    def test_the_last_copy_takes_the_entry_with_it(self):
        row_id = self.scan_card('only')
        self.scan.release_for_review(self.scan.review_candidate(row_id))
        self.assertEqual(self.total(), 0)
        self.assertIsNone(self.scan.get_entry(row_id))

    def test_the_phone_is_told_it_cannot_follow_the_capture_not_that_it_is_gone(self):
        row_id = self.scan_card('only')
        self.scan.release_for_review(self.scan.review_candidate(row_id))
        self.assertEqual(self.scan.captured_copy('only', 'desk-a')[0], 'unfollowed')

    def test_a_card_without_a_capture_cannot_be_sent(self):
        row_id = self.collection.add_card(CARD, GAME, 'regular', quiet=True)  # typed in / imported: no photo
        self.assertIsNone(self.collection.review_candidate(row_id))
        self.assertIsNone(self.collection.review_candidate(9999))

    def test_copies_a_trade_holds_are_not_released_and_nothing_changes(self):
        row_id = self.scan_card('only')
        candidate = self.scan.review_candidate(row_id)
        photos = lambda: len(list(inv.CAPTURES_DIR.glob('*.jpg')))
        # The same trigger a held entry meets (inventory.TRADE_HELD): forced here by making the
        # delete fail, since the trade tables belong to the collection's file
        self.scan.conn.execute(f"CREATE TRIGGER refuse BEFORE DELETE ON inventory BEGIN SELECT RAISE(ABORT, '{inv.TRADE_HELD}'); END")
        with self.assertRaises(sqlite3.Error):
            self.scan.release_for_review(candidate)
        self.assertEqual(self.total(), 1)
        self.assertEqual(self.scan.captured_copy('only', 'desk-a')[0], 'scanned')  # photo row and key as before
        # A later write that commits must not delete the file of the row that was rolled back
        self.scan.conn.execute('DROP TRIGGER refuse')
        self.scan_card('another')
        self.assertEqual(photos(), 2)

if __name__ == '__main__':
    unittest.main()
