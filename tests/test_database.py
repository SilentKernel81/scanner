"""
The cases of docs/plans/DATABASE_REVIEW.md (2026-10-09): transactions that are whole or not at all, reads
that do not write, station ownership that follows the quantity, captures that count once, and
the web cache's own bounded file. Everything runs on temporary databases and folders; no
application data is touched.

    venv/bin/python -m unittest discover tests
"""
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core import backups  # noqa: E402
from card_scanner.core import inventory as inv  # noqa: E402
from card_scanner.core import pending as pending_store  # noqa: E402
from card_scanner.core import recommendations  # noqa: E402
from card_scanner.core import review as review_store  # noqa: E402
from card_scanner.core.database import CONFIRMED_MATCHES, CardDatabase  # noqa: E402
from test_ownership import CARD, GAME, OTHER, TemporaryData  # noqa: E402


class ReadsDoNotWrite(TemporaryData):
    def test_a_filtered_list_leaves_no_transaction_open(self):
        self.add('desk-a')
        self.scan.get_all_cards(GAME, 'desk-a')
        self.scan.get_stats(GAME, 'desk-a')
        self.assertFalse(self.scan.conn.in_transaction)
        other = sqlite3.connect(str(self.tmp / 'scan.db'), timeout=0.2)  # used to fail: database is locked
        other.execute('UPDATE inventory SET tags = ?', ('seen',))
        other.commit()
        other.close()

    def test_deleting_an_entry_clears_what_stood_beside_it(self):
        row_id = self.add('desk-a')
        self.scan.delete_card(row_id, quiet=True)
        count = lambda table: self.scan.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        self.assertEqual((count('inventory_sources'), count('inventory_captures')), (0, 0))
        self.assertEqual(list(inv.CAPTURES_DIR.glob('*.jpg')), [])


class StationsNeverClaimMoreThanThereIs(TemporaryData):
    def claims(self):
        """[(entry quantity, copies the stations claim)] - the second never above the first"""
        return self.scan.conn.execute('''
            SELECT i.quantity, COALESCE((SELECT SUM(s.quantity) FROM inventory_sources s WHERE s.inventory_id = i.id), 0)
            FROM inventory i''').fetchall()

    def assert_consistent(self):
        for quantity, claimed in self.claims():
            self.assertLessEqual(claimed, quantity)

    def test_lowering_the_quantity_takes_from_the_stations(self):
        self.add('desk-a', copies=2)
        row_id = self.add('desk-b', copies=2)
        self.scan.update_card(row_id, quantity=2, quiet=True)
        self.assert_consistent()
        # The newest copies go, like their captures: desk-b scanned last
        self.assertEqual((self.total('desk-a'), self.total('desk-b')), (2, 0))
        self.assertEqual(self.total(), 2)

    def test_copies_added_by_hand_go_before_a_stations(self):
        row_id = self.add('desk-a', copies=2)
        self.scan.add_card(CARD, GAME, 'regular', quantity=3, quiet=True)   # nobody's
        self.scan.update_card(row_id, quantity=3, quiet=True)
        self.assertEqual(self.total('desk-a'), 2)
        self.scan.update_card(row_id, quantity=1, quiet=True)
        self.assertEqual(self.total('desk-a'), 1)
        self.assert_consistent()

    def test_consistent_through_splits_merges_undo_and_moves(self):
        self.add('desk-a', copies=3)
        row_id = self.add('desk-b', copies=2)
        self.scan.update_card(row_id, location='Box', split_quantity=2, quiet=True)   # split
        self.assert_consistent()
        self.scan.update_card(row_id, quantity=2, quiet=True)                         # lowered
        self.assert_consistent()
        moved = self.scan.conn.execute("SELECT id FROM inventory WHERE location = 'Box'").fetchone()[0]
        self.scan.update_card(moved, location='', quantity=1, quiet=True)             # merged back, one fewer
        self.assert_consistent()
        self.add('desk-a', OTHER)
        self.scan.undo_last_add(source='desk-a')                                      # undo
        self.assert_consistent()
        self.collection.take_from(self.scan, GAME, station='desk-a')                  # one camera's cards move
        self.assert_consistent()
        self.assertEqual(self.total('desk-a'), 0)


class FailedWritesLeaveNothing(TemporaryData):
    def test_a_failed_change_is_not_committed_with_the_next_one(self):
        row_id = self.add('desk-a')

        def broken(*args, **kwargs):
            raise RuntimeError('disk trouble')
        trim, self.scan._trim_captures = self.scan._trim_captures, broken
        try:
            with self.assertRaises(RuntimeError):
                self.scan.update_card(row_id, tags='half done', quiet=True)
        finally:
            self.scan._trim_captures = trim
        self.assertFalse(self.scan.conn.in_transaction)
        self.scan.set_price(row_id, 2.0)                       # commits - and used to take the tags along
        self.assertEqual(self.scan.get_entry(row_id)['tags'], '')

    def test_thumbnails_go_only_when_the_delete_is_committed(self):
        row_id = self.add('desk-a')
        files = list(inv.CAPTURES_DIR.glob('*.jpg'))
        with self.scan._lock:
            self.scan._delete_entries('id = ?', (row_id,))
            self.assertTrue(files[0].exists())                 # not yet: it could still be rolled back
            self.scan._rollback()
        self.assertTrue(files[0].exists())
        self.assertEqual(self.total('desk-a'), 1)
        self.scan.delete_card(row_id, quiet=True)
        self.assertFalse(files[0].exists())

    def test_replacing_with_a_file_without_usable_rows_keeps_the_inventory(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        bad = self.tmp / 'bad.csv'
        bad.write_text('Card Name,Set,Finish\n,,\nSome Card,Some Set,hologram\n')
        result = self.collection.import_csv(bad, GAME, ['regular', 'foil'], replace_existing=True)
        self.assertFalse(result['success'])
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 1)
        self.assertFalse(self.collection.import_entries([], GAME, replace_existing=True)['success'])
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 1)
        good = self.tmp / 'good.csv'
        good.write_text('Card Name,Set,Finish,Quantity\nSome Card,Some Set,foil,3\n')
        self.assertTrue(self.collection.import_csv(good, GAME, ['regular', 'foil'], replace_existing=True)['success'])
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 3)


class CsvImport(TemporaryData):
    FINISHES = ['regular', 'foil']

    def file(self, text):
        path = self.tmp / 'import.csv'
        path.write_text(text)
        return path

    def test_headers_in_any_case(self):
        result = self.collection.import_csv(self.file(' card name ,SET,quantity,FINISH\nSome Card,Some Set,2,Foil\n'),
                                            GAME, self.FINISHES)
        self.assertEqual((result['added'], result['skipped']), (1, 0))
        card, = self.collection.get_all_cards(GAME)
        self.assertEqual((card['name'], card['quantity'], card['finish']), ('Some Card', 2, 'foil'))

    def test_a_refused_replacement_keeps_cards_captures_and_cameras(self):
        self.add('desk-a', copies=2)
        one_bad_row = self.file('Card Name,Set,Finish\nGood Card,Some Set,foil\nBad Card,Some Set,hologram\n')
        result = self.scan.import_csv(one_bad_row, GAME, self.FINISHES, replace_existing=True)
        self.assertFalse(result['success'])
        self.assertIn('1 row', result['error'])
        self.assertEqual((self.total(), self.total('desk-a')), (2, 2))
        self.assertEqual(len(list(inv.CAPTURES_DIR.glob('*.jpg'))), 2)
        # Added instead of replacing, the row that cannot be used is only left out
        result = self.scan.import_csv(one_bad_row, GAME, self.FINISHES)
        self.assertEqual((result['success'], result['added'], result['errors']), (True, 1, 1))
        self.assertEqual(self.total(), 3)


class OldOverclaims(TemporaryData):
    def test_repaired_at_the_next_start(self):
        self.add('desk-a', copies=2)
        row_id = self.add('desk-b', copies=2)
        self.scan.conn.execute('UPDATE inventory SET quantity = 2 WHERE id = ?', (row_id,))   # as an older version left it
        self.scan.conn.commit()
        self.scan.conn.close()
        self.scan = inv.InventoryManager(db_file=self.tmp / 'scan.db')
        claimed = self.scan.conn.execute('SELECT SUM(quantity) FROM inventory_sources').fetchone()[0]
        self.assertEqual((claimed, self.total()), (2, 2))


class DeckChangesAreWhole(TemporaryData):
    def setUp(self):
        super().setUp()
        self.deck = self.decks.create(GAME, 'Test', 'commander')
        self.decks.set_card(self.deck, 'Original', 'main', 1)

    def names(self, board=None):
        return sorted(card['name'] for card in self.decks.get(self.deck)['cards'] if board in (None, card['board']))

    def test_a_failed_import_replaces_nothing(self):
        with self.assertRaises(ValueError):
            self.decks.import_cards(self.deck, [{'name': 'New', 'quantity': 1}, {'name': 'Bad', 'quantity': 'many'}],
                                    replace=True)
        self.decks.update(self.deck, notes='later')            # used to commit the half-done replacement
        self.assertEqual(self.names(), ['Original'])

    def test_a_failure_inside_an_import_is_rolled_back(self):
        touch, self.decks._touch = self.decks._touch, lambda deck_id: 1 / 0
        try:
            with self.assertRaises(ZeroDivisionError):
                self.decks.import_cards(self.deck, [{'name': 'New', 'quantity': 1}], replace=True)
        finally:
            self.decks._touch = touch
        self.assertFalse(self.decks.conn.in_transaction)
        self.decks.update(self.deck, notes='later')
        self.assertEqual(self.names(), ['Original'])

    def test_a_move_is_there_and_gone_or_neither(self):
        calls = []
        set_card = self.decks.set_card

        def fails_on_removal(deck_id, name, board, quantity, card_id=None):
            calls.append(board)
            if len(calls) == 2:
                raise RuntimeError('before the source is removed')
            return set_card(deck_id, name, board, quantity, card_id)
        self.decks.set_card = fails_on_removal
        try:
            with self.assertRaises(RuntimeError):
                self.decks.move_card(self.deck, 'Original', 'main', 'side')
        finally:
            del self.decks.set_card
        self.assertEqual((self.names('main'), self.names('side')), (['Original'], []))
        self.decks.move_card(self.deck, 'Original', 'main', 'side')
        self.assertEqual((self.names('main'), self.names('side')), ([], ['Original']))

    def test_replacing_with_nothing_is_refused(self):
        for entries in ([], [{'name': '  ', 'quantity': 1}]):
            with self.assertRaises(ValueError):
                self.decks.import_cards(self.deck, entries, replace=True)
        self.assertEqual(self.names(), ['Original'])

    def test_a_move_takes_the_printing_along(self):
        self.decks.set_printing(self.deck, 'Original', 'main', 'printing-a')
        self.decks.move_card(self.deck, 'Original', 'main', 'side')
        self.assertEqual([card['card_id'] for card in self.decks.get(self.deck)['cards']], ['printing-a'])
        self.decks.set_card(self.deck, 'Original', 'main', 1, 'printing-b')
        self.decks.move_card(self.deck, 'Original', 'main', 'side')           # the side board's printing stays
        card, = self.decks.get(self.deck)['cards']
        self.assertEqual((card['quantity'], card['card_id']), (2, 'printing-a'))

    def test_no_cards_for_a_deck_that_is_not_there(self):
        with self.assertRaises(ValueError):
            self.decks.set_card(999, 'Ghost', 'main', 1)
        with self.assertRaises(ValueError):
            self.decks.import_cards(999, [{'name': 'Ghost'}])
        self.assertEqual(self.decks.conn.execute('SELECT COUNT(*) FROM deck_cards WHERE deck_id = 999').fetchone()[0], 0)


class CatalogSwap(TemporaryData):
    def setUp(self):
        super().setUp()
        self.cards = CardDatabase(db_file=self.tmp / 'cards.db')
        self.cards.conn.execute("INSERT INTO cards (id, name) VALUES ('1', 'Kept Card')")
        self.cards.conn.commit()

    def tearDown(self):
        self.cards.conn.close()
        super().tearDown()

    def kept(self):
        return [row[0] for row in self.cards.conn.execute('SELECT name FROM cards')]

    def stage(self, rows):
        cursor = self.cards.conn.cursor()
        cursor.execute('DROP TABLE IF EXISTS cards_import')
        self.cards._create_cards_table(cursor, table='cards_import')
        cursor.executemany('INSERT INTO cards_import (id, name) VALUES (?, ?)', rows)
        self.cards.conn.commit()

    def test_a_missing_or_empty_import_leaves_the_cards(self):
        with self.assertRaises(RuntimeError):                  # used to drop `cards` and then fail
            self.cards.replace_table('cards_import', 'cards')
        self.assertEqual(self.kept(), ['Kept Card'])
        self.stage([])
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards')
        self.assertEqual(self.kept(), ['Kept Card'])

    def test_a_failure_while_swapping_is_rolled_back(self):
        self.stage([('2', 'New Card')])

        def broken(cursor):
            raise RuntimeError('index trouble')
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards', broken)
        self.assertEqual(self.kept(), ['Kept Card'])
        self.cards.replace_table('cards_import', 'cards', self.cards._create_card_indexes)
        self.assertEqual(self.kept(), ['New Card'])


    def test_a_much_smaller_import_is_refused(self):
        self.cards.conn.executemany('INSERT INTO cards (id, name) VALUES (?, ?)', [(f'k{n}', f'Card {n}') for n in range(9)])
        self.cards.conn.commit()
        self.stage([('2', 'New Card')])                        # 1 card where 10 are in use: a download cut short
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards')
        self.assertEqual(len(self.kept()), 10)

    def test_the_cards_and_what_is_known_about_them_change_together(self):
        self.cards.set_data_info('mtg', 'old', 1)
        self.stage([('2', 'New Card')])

        def broken(cursor):
            raise RuntimeError('index trouble')
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards', broken, info=('mtg', 'new', 1))
        self.assertEqual(self.cards.get_data_info('mtg')['source_updated'], 'old')
        self.cards.replace_table('cards_import', 'cards', info=('mtg', 'new', 1))
        self.assertEqual((self.kept(), self.cards.get_data_info('mtg')['source_updated']), (['New Card'], 'new'))

    def test_only_card_tables(self):
        with self.assertRaises(ValueError):
            self.cards.replace_table('cards_import', 'inventory')


class CapturesCountOnce(TemporaryData):
    def setUp(self):
        super().setUp()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.saved_review = review_store.REVIEW_DIR
        review_store.REVIEW_DIR = self.tmp / 'review'
        self.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')

    def tearDown(self):
        self.pending.conn.close()
        self.review.conn.close()
        review_store.REVIEW_DIR = self.saved_review
        super().tearDown()

    def queue(self, capture_id='cap-1'):
        return self.pending.add(GAME, 'desk-a', 7, 'a.jpg', None, True, '2026-10-09 12:00:00',
                                capture_id=capture_id, sender='desk-a')

    def test_a_capture_read_again_is_not_added_twice(self):
        _, uid = self.queue()
        first = self.scan.add_card(CARD, GAME, 'regular', capture=self.photo, source='desk-a', quiet=True, capture_key=uid)
        # ... a crash before the capture left the queue: at the next start it is read again
        again = self.scan.add_card(CARD, GAME, 'regular', capture=self.photo, source='desk-a', quiet=True, capture_key=uid)
        self.assertEqual(first, again)
        self.assertEqual((self.total(), self.total('desk-a')), (1, 1))
        self.assertEqual(len(list(inv.CAPTURES_DIR.glob('*.jpg'))), 1)
        _, other = self.queue('cap-2')
        self.scan.add_card(CARD, GAME, 'regular', source='desk-a', quiet=True, capture_key=other)
        self.assertEqual(self.total(), 2)                       # another capture of the same card counts

    def test_a_failed_add_notes_nothing(self):
        _, uid = self.queue()
        with self.assertRaises(KeyError):
            self.scan.add_card({'set_name': 'no name'}, GAME, 'regular', quiet=True, capture_key=uid)
        self.scan.add_card(CARD, GAME, 'regular', quiet=True, capture_key=uid)
        self.assertEqual(self.total(), 1)

    def test_a_capture_is_queued_for_review_once(self):
        _, uid = self.queue()
        self.assertIsNotNone(self.review.add(GAME, self.photo, 'Unknown', station='desk-a', capture_key=uid))
        self.assertIsNone(self.review.add(GAME, self.photo, 'Unknown', station='desk-a', capture_key=uid))
        self.assertEqual(self.review.count(GAME), 1)
        self.assertEqual(len(list(review_store.REVIEW_DIR.glob('*.jpg'))), 1)

    def test_items_without_a_station_show_on_every_cameras_page(self):
        """
        What the server's own camera queued before 2026-10-12 (station NULL) has no page of
        its own any more: each camera's page lists it beside its own items, so it can still
        be resolved, and resolving it once takes it off all of them.
        """
        nobodys = self.review.add(GAME, self.photo, 'From the old camera')            # station NULL
        own = self.review.add(GAME, self.photo, 'Unknown', station='desk-a')
        self.review.add(GAME, self.photo, 'Unknown', station='desk-b')
        self.assertEqual(self.review.count(GAME, 'desk-a'), 2)      # its own and nobody's, not desk-b's
        self.assertEqual(self.review.count(GAME, 'desk-b'), 2)
        self.assertEqual(self.review.count(GAME, None), 1)
        self.assertEqual(self.review.count(GAME), 3)                # each item once
        item, total = self.review.first(GAME, 'desk-a')
        self.assertEqual((item['id'], total), (nobodys, 2))         # oldest first
        # Clearing one camera drops only what that camera queued
        self.assertEqual(self.review.clear(GAME, 'desk-a'), 1)
        self.assertIsNone(self.review.get(own))
        self.assertEqual(self.review.count(GAME, 'desk-a'), 1)
        # Resolved on one page: gone from the other too
        self.review.remove(nobodys)
        self.assertEqual(self.review.count(GAME, 'desk-a'), 0)
        self.assertEqual(self.review.count(GAME, 'desk-b'), 1)

    def test_the_outcome_survives_a_restart(self):
        pending_id, _ = self.queue()
        self.assertTrue(self.pending.has('desk-a', 7))
        self.pending.settle(pending_id, {'status': 'added', 'card': {'name': 'Test Card'}})
        self.pending.conn.close()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')   # the next start
        self.assertFalse(self.pending.has('desk-a', 7))
        self.assertEqual(self.pending.settled('cap-1'), ('desk-a', 7, {'status': 'added', 'card': {'name': 'Test Card'}}))
        self.assertEqual(self.pending.outcome('desk-a', 7)['status'], 'added')
        self.assertIsNone(self.pending.settled('never-sent'))
        self.assertIsNone(self.pending.outcome('desk-a', 8))

    def test_a_failed_capture_is_kept_a_few_times_then_settled(self):
        pending_id, uid = self.queue()
        error = {'status': 'error', 'message': 'database is locked'}
        for _ in range(pending_store.MAX_ATTEMPTS - 1):
            self.assertTrue(self.pending.fail(pending_id, 'database is locked', error))
            self.assertEqual([row['uid'] for row in self.pending.all()], [uid])      # still to be read again
        self.assertFalse(self.pending.fail(pending_id, 'database is locked', error))
        self.assertEqual(self.pending.all(), [])
        self.assertEqual(self.pending.settled('cap-1')[2], error)

    def test_captures_queued_before_uids_get_one(self):
        self.pending.conn.execute("INSERT INTO pending_captures (game, number, image, captured_at) VALUES ('mtg', 1, 'old.jpg', 'then')")
        self.pending.conn.commit()
        self.pending.conn.close()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.assertTrue(all(row['uid'] for row in self.pending.all()))


class WebCache(TemporaryData):
    def cache(self, **kwargs):
        return recommendations.Recommendations(db_file=self.tmp / 'web_cache.db', old_file=self.tmp / 'collection.db', **kwargs)

    def test_answers_come_back_and_expire(self):
        cache = self.cache()
        cache._store('https://example.org/a', {'cards': ['Sol Ring'] * 500})
        self.assertEqual(len(cache.cached('https://example.org/a', 60)['cards']), 500)
        self.assertIsNone(cache.cached('https://example.org/a', 0))
        self.assertIsNone(cache.cached('https://example.org/none', 60))
        with cache._connect() as conn:
            size, = conn.execute('SELECT size FROM web_cache').fetchone()
            conn.execute('UPDATE web_cache SET fetched_at = ?', (time.time() - recommendations.MAX_AGE - 60,))
        self.assertLess(size, 500)                              # compressed
        cache.prune()
        with cache._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM web_cache').fetchone()[0], 0)

    def test_the_oldest_make_room(self):
        cache = self.cache()
        for number in range(5):
            cache._store(f'https://example.org/{number}', {'text': str(number) * 50})
            with cache._connect() as conn:
                conn.execute('UPDATE web_cache SET fetched_at = ? WHERE url = ?', (time.time() - 100 + number, f'https://example.org/{number}'))
        with cache._connect() as conn:
            sizes = [row[0] for row in conn.execute('SELECT size FROM web_cache ORDER BY fetched_at')]
        limit, recommendations.MAX_BYTES = recommendations.MAX_BYTES, sum(sizes[-2:])
        try:
            cache.prune()
        finally:
            recommendations.MAX_BYTES = limit
        with cache._connect() as conn:
            self.assertEqual([row[0][-1] for row in conn.execute('SELECT url FROM web_cache ORDER BY fetched_at')], ['3', '4'])

    def test_the_cache_in_the_card_database_moves_over(self):
        old = sqlite3.connect(str(self.tmp / 'collection.db'))
        old.execute('CREATE TABLE web_cache (url TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)')
        old.execute('INSERT INTO web_cache VALUES (?, ?, ?)', ('https://example.org/old', time.time(), '{"kept": true}'))
        old.commit()
        old.close()
        cache = self.cache()
        self.assertEqual(cache.cached('https://example.org/old', 60), {'kept': True})
        old = sqlite3.connect(str(self.tmp / 'collection.db'))
        self.assertIsNone(old.execute("SELECT 1 FROM sqlite_master WHERE name = 'web_cache'").fetchone())
        self.assertEqual(old.execute('SELECT COUNT(*) FROM inventory').fetchone()[0], 0)   # the rest of the file is as it was
        old.close()
        self.cache()                                            # a second start finds nothing to move



OLD_INVENTORY = """
    CREATE TABLE inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL DEFAULT 'mtg', card_id TEXT, card_name TEXT NOT NULL,
        set_name TEXT NOT NULL, set_code TEXT, card_number TEXT NOT NULL DEFAULT '', rarity TEXT, type_line TEXT,
        mana_cost TEXT, colors TEXT, color_identity TEXT, price_usd REAL, quantity INTEGER NOT NULL DEFAULT 1,
        condition TEXT NOT NULL DEFAULT 'Near Mint', finish TEXT NOT NULL DEFAULT 'regular', timestamp TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '', added_at TEXT, added_quantity INTEGER,
        UNIQUE(game, card_name, set_name, card_number, condition, finish, location))"""


class Constraints(TemporaryData):
    def old_file(self, with_stations=True):
        """A file as the versions before the constraints made it - also before stations"""
        path = self.tmp / 'old.db'
        conn = sqlite3.connect(str(path))
        conn.execute(OLD_INVENTORY)
        conn.execute('CREATE TABLE inventory_captures (id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_id INTEGER NOT NULL, '
                     'file TEXT NOT NULL, captured_at TEXT NOT NULL' + (', station TEXT)' if with_stations else ')'))
        if with_stations:
            conn.execute('CREATE TABLE inventory_sources (inventory_id INTEGER NOT NULL, station TEXT NOT NULL, '
                         'quantity INTEGER NOT NULL, PRIMARY KEY (inventory_id, station))')
        entry = "INSERT INTO inventory (id, card_name, set_name, card_number, quantity, timestamp) VALUES (?, ?, 'Set', ?, ?, 'then')"
        conn.executemany(entry, [(3, 'Kept', '1', 2), (7, 'Also kept', '2', 1), (9, 'No copies', '3', 0)])
        conn.execute("DELETE FROM inventory WHERE id = 7")
        conn.execute(entry, (12, 'Newest', '4', 5))            # ids 7 and 10-11 were given out and are gone
        conn.executemany("INSERT INTO inventory_captures (id, inventory_id, file, captured_at) VALUES (?, ?, ?, 'then')",
                         [(1, 3, 'a.jpg'), (2, 12, 'b.jpg'), (3, 7, 'orphan.jpg'), (4, 9, 'of-nothing.jpg')])
        if with_stations:
            conn.executemany('INSERT INTO inventory_sources VALUES (?, ?, ?)',
                             [(3, 'desk-a', 2), (12, 'desk-a', 1), (7, 'desk-a', 1), (12, 'desk-b', 0)])
        conn.commit()
        conn.close()
        return path

    def test_an_older_file_is_rebuilt_with_them_once(self):
        from card_scanner.core import storage
        try:
            path = self.old_file()
            old = inv.InventoryManager(db_file=path)
            backups_made = list((self.tmp / 'backups' / 'migrations').glob('*.db'))
            self.assertEqual(len(backups_made), 1)
            kept = sqlite3.connect(str(backups_made[0]))       # as it was, readable
            self.assertEqual(kept.execute('SELECT COUNT(*) FROM inventory').fetchone()[0], 3)
            kept.close()
            rows = lambda sql: [tuple(row) for row in old.conn.execute(sql)]
            self.assertEqual(rows('SELECT id, card_name, quantity FROM inventory ORDER BY id'), [(3, 'Kept', 2), (12, 'Newest', 5)])
            self.assertEqual(rows('SELECT id, inventory_id FROM inventory_captures ORDER BY id'), [(1, 3), (2, 12)])
            self.assertEqual(rows('SELECT inventory_id, station, quantity FROM inventory_sources ORDER BY 1'),
                             [(3, 'desk-a', 2), (12, 'desk-a', 1)])
            self.assertEqual(rows('PRAGMA foreign_key_check'), [])
            # Ids are not given out again: the next entry is 13, the next capture 5
            new_id = old.add_card(CARD, GAME, 'regular', capture=self.photo, source='desk-a', quiet=True)
            self.assertEqual((new_id, rows('SELECT MAX(id) FROM inventory_captures')), (13, [(5,)]))
            old.conn.close()
            again = inv.InventoryManager(db_file=path)          # a second start: nothing to do
            self.assertEqual(len(list((self.tmp / 'backups' / 'migrations').glob('*.db'))), 1)
            self.assertEqual(again.get_stats(GAME)['total_cards'], 8)
            again.conn.close()
        finally:
            storage._backed_up.clear()

    def test_a_file_from_before_stations(self):
        from card_scanner.core import storage
        try:
            old = inv.InventoryManager(db_file=self.old_file(with_stations=False))
            self.assertEqual(old.get_stats(GAME)['total_cards'], 7)
            self.assertEqual(old.get_stats(GAME, 'desk-a')['total_cards'], 0)      # nobody is given old cards
            self.assertEqual(old.conn.execute('SELECT COUNT(*) FROM inventory_captures').fetchone()[0], 2)
            old.conn.close()
        finally:
            storage._backed_up.clear()

    def test_what_the_database_refuses_now(self):
        row_id = self.add('desk-a')
        for sql in ("INSERT INTO inventory_captures (inventory_id, file, captured_at) VALUES (999, 'x.jpg', 'now')",
                    "INSERT INTO inventory_sources VALUES (999, 'desk-a', 1)",
                    f"INSERT INTO inventory_sources VALUES ({row_id}, 'desk-b', 0)",
                    f'UPDATE inventory SET quantity = 0 WHERE id = {row_id}',
                    f"UPDATE inventory SET quantity = 'two' WHERE id = {row_id}",
                    f'UPDATE inventory SET added_quantity = -1 WHERE id = {row_id}'):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                self.scan.conn.execute(sql)
            self.scan.conn.rollback()
        deck = self.decks.create(GAME, 'Test', 'commander')
        self.decks.set_card(deck, 'Card', 'main', 1)
        for sql in ("INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (999, 'Ghost', 1, 'main')",
                    f"INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES ({deck}, 'Zero', 0, 'main')",
                    f"INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES ({deck}, 'Odd', 1, 'attic')"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                self.decks.conn.execute(sql)
            self.decks.conn.rollback()
        # The children go with their parent, in the database itself
        self.decks.conn.execute('DELETE FROM decks WHERE id = ?', (deck,))
        self.decks.conn.commit()
        self.assertEqual(self.decks.conn.execute('SELECT COUNT(*) FROM deck_cards').fetchone()[0], 0)
        self.scan.conn.execute('DELETE FROM inventory WHERE id = ?', (row_id,))
        self.scan.conn.commit()
        count = lambda table: self.scan.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        self.assertEqual((count('inventory_captures'), count('inventory_sources')), (0, 0))

    def test_old_decks_are_rebuilt_too(self):
        from card_scanner.core import storage
        try:
            path = self.tmp / 'olddecks.db'
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE decks (id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL, name TEXT NOT NULL, "
                         "format TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE deck_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, deck_id INTEGER NOT NULL, card_name TEXT NOT NULL, "
                         "card_id TEXT, quantity INTEGER NOT NULL DEFAULT 1, board TEXT NOT NULL DEFAULT 'main', UNIQUE(deck_id, card_name, board))")
            conn.execute('CREATE INDEX idx_deck_cards_deck ON deck_cards(deck_id)')
            conn.execute("INSERT INTO decks VALUES (4, 'mtg', 'Kept', 'commander', '', 'then', 'then')")
            conn.executemany('INSERT INTO deck_cards (id, deck_id, card_name, quantity, board) VALUES (?, ?, ?, ?, ?)',
                             [(10, 4, 'A', 1, 'main'), (11, 4, 'B', 3, 'side'), (12, 99, 'Of a deck that is gone', 1, 'main'),
                              (13, 4, 'None', 0, 'main')])
            conn.commit()
            conn.close()
            from card_scanner.core.decks import DeckManager
            decks = DeckManager(db_file=path)
            self.assertEqual([(card['name'], card['quantity'], card['board']) for card in decks.get(4)['cards']],
                             [('A', 1, 'main'), ('B', 3, 'side')])
            decks.set_card(4, 'C', 'main', 1)
            self.assertEqual(decks.conn.execute("SELECT id FROM deck_cards WHERE card_name = 'C'").fetchone()[0], 14)
            decks.conn.close()
        finally:
            storage._backed_up.clear()

    def test_a_backup_from_before_restores(self):
        self.add('desk-a')
        backup_id = self.backup()
        # As a backup made before the constraints could be: rows beside entries that were gone
        old = sqlite3.connect(str(backups.BACKUPS_DIR / backup_id / 'backup.db'))
        old.execute("INSERT INTO scanned_inventory_sources VALUES (999, 'desk-b', 1)")
        old.execute("INSERT INTO scanned_inventory_captures (inventory_id, file, captured_at) VALUES (999, 'gone.jpg', 'then')")
        old.execute("INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (999, 'Ghost', 1, 'main')")
        old.commit()
        old.close()
        self.scan.clear_inventory(GAME)
        self.restore(backup_id)
        self.assertEqual((self.total(), self.total('desk-a'), self.total('desk-b')), (1, 1, 0))
        self.assertEqual(self.scan.conn.execute('PRAGMA foreign_key_check').fetchall(), [])


class Batches(TemporaryData):
    def batch(self, batch):
        """Copies of the collection that came with one batch"""
        return sum(row['quantity'] for row in self.collection.conn.execute(
            'SELECT quantity FROM inventory_batches WHERE batch = ?', (batch,)))

    def test_a_split_takes_only_its_share_of_the_batch(self):
        self.collection.add_card(CARD, GAME, 'regular', quantity=3, quiet=True, when='2026-01-01 10:00:00')   # there before
        self.add('desk-a', copies=2)
        self.collection.take_from(self.scan, GAME)                                    # the batch: 2 of the 5
        row = self.collection.conn.execute('SELECT * FROM inventory').fetchone()
        batch = self.collection.get_all_cards(GAME)[0]['batches'][0]['batch']
        self.assertEqual(self.batch(batch), 2)
        self.collection.update_card(row['id'], location='Box', split_quantity=3, quiet=True)   # 3 move, 2 stay
        self.assertEqual(self.batch(batch), 2)                                        # used to be 5: 3 + 2
        removed = self.collection.remove_batch(GAME, batch)
        self.assertEqual(removed['cards'], 2)
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 3)


class CardDataImport(TemporaryData):
    def card(self, number):
        # Legal somewhere, as every real card is (the deck search leaves out a card legal nowhere: a token, an Un-card)
        return {'id': f'id-{number}', 'name': f'Card {number}', 'set': 'tst', 'set_name': 'Test', 'collector_number': str(number),
                'layout': 'normal', 'prices': {}, 'image_uris': {'normal': ''}, 'legalities': {'vintage': 'legal'}}

    def test_tokens_and_emblems_are_kept_and_art_cards_are_not(self):
        # Scryfall's own records: the Cadet of Fallout (set 'tfra', number 1; its strip prints "T 0001" over "FRA")
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            token = {**self.card(1), 'id': 'tok', 'name': 'Cadet', 'set': 'tfra', 'set_name': 'Fallout Tokens', 'collector_number': '1',
                     'layout': 'token', 'type_line': 'Token Creature \u2014 Wizard Soldier', 'rarity': 'common'}
            emblem = {**token, 'id': 'emb', 'name': 'Ajani Resolute Emblem', 'collector_number': '16', 'layout': 'emblem', 'type_line': 'Emblem'}
            art = {**token, 'id': 'art', 'name': 'Some Art', 'collector_number': '9', 'layout': 'art_series', 'type_line': 'Card // Card'}
            kept = cards.populate_database([self.card(n) for n in range(40)] + [token, emblem, art])
            self.assertEqual(kept, 42)
            ids = {row[0] for row in cards.conn.execute('SELECT id FROM cards')}
            self.assertTrue({'tok', 'emb'} <= ids)
            self.assertNotIn('art', ids)
            # Found by what the parser writes for it (card_ocr.parse_magic: 'TFRA', '0001'), and as a confirmed match
            found = cards.search_card_exact('Cadet', '0001', 'TFRA')
            self.assertEqual((found['name'], found['set_code'], found['number']), ('Cadet', 'tfra', '1'))
            self.assertIn(found['match'], CONFIRMED_MATCHES)
        finally:
            cards.conn.close()

    def test_the_facts_a_phone_gets_of_each_printing(self):
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            token = {**self.card(1), 'id': 'tok', 'name': 'Cadet', 'set': 'tfra', 'collector_number': '1', 'layout': 'token', 'finishes': ['nonfoil', 'foil'],
                     'oracle_id': 'oracle-cadet', 'type_line': 'Token Creature'}
            cards.populate_database([self.card(n) for n in range(40)] + [token])
            facts = cards.printing_facts()
            self.assertEqual(len(facts), 41)
            self.assertEqual(facts['tok'][:5], ('Cadet', 'tfra', '1', 'oracle-cadet', 'nonfoil,foil'))
            self.assertEqual(len(facts['tok']), 7)                        # and released_at, frame (the phone's fixed-place foil reading)
            self.assertEqual(facts['id-3'][:3], ('Card 3', 'tst', '3'))
        finally:
            cards.conn.close()

    def test_a_card_has_the_lowest_rarity_of_its_printings(self):
        # A common with a newer "rare" promo printing (a Forest of g17 / a Secret Lair; Llanowar Elves' "mythic" one) is a
        # common - for the deck builder's rarity filter, the label it shows, and the wildcard that crafts it in Arena.
        # The filter used to keep any card with one printing of that rarity, and showed that printing.
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            common = {**self.card(1), 'id': 'elf-m19', 'name': 'Elf', 'rarity': 'common', 'released_at': '2018-07-13',
                      'legalities': {'standard': 'legal'}}
            promo = {**common, 'id': 'elf-sld', 'set': 'sld', 'collector_number': '900', 'rarity': 'mythic', 'released_at': '2026-09-28'}
            rare = {**self.card(2), 'id': 'warg', 'name': 'Warg', 'rarity': 'rare', 'legalities': {'standard': 'legal'}}
            special = {**self.card(3), 'id': 'odd', 'name': 'Odd', 'rarity': 'special'}   # nothing else: special it stays
            # Bloom Tender: rare in its paper sets, mythic in its only Arena printing - Arena's wildcard is a mythic one
            tender_paper = {**self.card(4), 'id': 'bt-eve', 'name': 'Tender', 'rarity': 'rare', 'games': ['paper']}
            tender_arena = {**tender_paper, 'id': 'bt-ecl', 'set': 'ecl', 'collector_number': '5', 'rarity': 'mythic',
                            'games': ['paper', 'arena']}
            cards.populate_database([self.card(n) for n in range(100, 140)]
                                    + [common, promo, rare, special, tender_paper, tender_arena])
            self.assertEqual(cards.conn.execute("SELECT games FROM cards WHERE id = 'bt-ecl'").fetchone()[0], '["paper", "arena"]')
            self.assertEqual(cards.cards_by_names(['Tender'])['tender']['rarity'], 'mythic')
            self.assertEqual([card['name'] for card in cards.search_cards(text='tender', rarity='mythic')[0]], ['Tender'])
            self.assertEqual(cards.search_cards(text='tender', rarity='rare')[0], [])
            names = lambda **filters: [card['name'] for card in cards.search_cards(**filters)[0]]
            self.assertEqual(names(text='elf', rarity='common'), ['Elf'])
            self.assertEqual(names(text='elf', rarity='mythic'), [])
            self.assertEqual(names(rarity='rare'), ['Warg'])
            self.assertEqual(names(rarity='rare', legal_in='standard'), ['Warg'])      # with another filter's parameters
            self.assertEqual(cards.search_cards(text='elf')[0][0]['rarity'], 'common')  # the label too, without a filter
            by_name = cards.cards_by_names(['Elf', 'Warg', 'Odd'])                       # what arena_check counts wildcards by
            self.assertEqual({key: card['rarity'] for key, card in by_name.items()},
                             {'elf': 'common', 'warg': 'rare', 'odd': 'special'})
        finally:
            cards.conn.close()

    def test_cards_with_faces_keep_their_text_cost_colours_and_stats(self):
        # As Scryfall writes them: an adventure has its cost and colours at the top but its text only on the faces; a
        # transform or modal double-faced card has none of them at the top. Stored as they came, the 2,500 such printings
        # had no text, and the two-sided ones no cost and no colour. Power and toughness are text ("*", "1+*")
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            gandalf = {**self.card(1), 'id': 'gandalf', 'name': "Gandalf, Goblins' Bane // Flameshape", 'layout': 'adventure',
                       'mana_cost': '{2}{R} // {1}{R}', 'colors': ['R'],
                       'type_line': 'Legendary Creature \u2014 Avatar Wizard // Sorcery \u2014 Adventure',
                       'card_faces': [{'name': "Gandalf, Goblins' Bane", 'mana_cost': '{2}{R}', 'power': '2', 'toughness': '3',
                                       'oracle_text': 'Whenever you cast a noncreature spell, Gandalf gets +1/+1 until end of turn.'},
                                      {'name': 'Flameshape', 'mana_cost': '{1}{R}',
                                       'oracle_text': 'Look at the top two cards of your library.'}]}
            delver = {**self.card(2), 'id': 'delver', 'name': 'Delver of Secrets // Insectile Aberration', 'layout': 'transform',
                      'type_line': 'Creature \u2014 Human Wizard // Creature \u2014 Human Insect',
                      'card_faces': [{'name': 'Delver of Secrets', 'mana_cost': '{U}', 'colors': ['U'], 'power': '1', 'toughness': '1',
                                      'oracle_text': 'At the beginning of your upkeep, look at the top card of your library.'},
                                     {'name': 'Insectile Aberration', 'mana_cost': '', 'colors': ['U'], 'power': '3',
                                      'toughness': '2', 'oracle_text': 'Flying'}]}
            mdfc = {**self.card(3), 'id': 'mdfc', 'name': 'Bala Ged Recovery // Bala Ged Sanctuary', 'layout': 'modal_dfc',
                    'type_line': 'Sorcery // Land',
                    'card_faces': [{'name': 'Bala Ged Recovery', 'mana_cost': '{2}{G}', 'colors': ['G'],
                                    'oracle_text': 'Return target card from your graveyard to your hand.'},
                                   {'name': 'Bala Ged Sanctuary', 'mana_cost': '', 'colors': [], 'oracle_text': '{T}: Add {G}.'}]}
            ajani = {**self.card(6), 'id': 'ajani', 'name': 'Ajani, Nacatl Pariah // Ajani, Nacatl Avenger', 'layout': 'transform',
                     'card_faces': [{'name': 'Ajani, Nacatl Pariah', 'mana_cost': '{1}{W}', 'colors': ['W'], 'power': '1',
                                     'toughness': '2', 'oracle_text': 'When Ajani enters, create a 2/1 white Cat Warrior.'},
                                    {'name': 'Ajani, Nacatl Avenger', 'mana_cost': '', 'colors': ['R', 'W'], 'loyalty': '3',
                                     'oracle_text': '+2: Put a +1/+1 counter on each Cat you control.'}]}
            goyf = {**self.card(4), 'id': 'goyf', 'name': 'Tarmogoyf', 'mana_cost': '{1}{G}', 'colors': ['G'], 'power': '*',
                    'toughness': '1+*', 'oracle_text': "Tarmogoyf's power is equal to the number of card types in all graveyards."}
            plain = {**self.card(5), 'id': 'land', 'name': 'Plain Land', 'colors': [], 'oracle_text': ''}
            cards.populate_database([self.card(n) for n in range(100, 140)] + [gandalf, delver, mdfc, goyf, plain, ajani])
            row = lambda card_id: tuple(cards.conn.execute(
                'SELECT oracle_text, mana_cost, colors, power, toughness FROM cards WHERE id = ?', (card_id,)).fetchone())
            self.assertEqual(row('gandalf'), (
                'Whenever you cast a noncreature spell, Gandalf gets +1/+1 until end of turn. // '
                'Look at the top two cards of your library.', '{2}{R} // {1}{R}', '["R"]', '2', '3'))   # the cost at the top kept
            self.assertEqual(row('delver'), (
                'At the beginning of your upkeep, look at the top card of your library. // Flying', '{U}', '["U"]', '1', '1'))
            self.assertEqual(row('mdfc'), (
                'Return target card from your graveyard to your hand. // {T}: Add {G}.', '{2}{G}', '["G"]', None, None))
            self.assertEqual(row('goyf')[3:], ('*', '1+*'))
            self.assertEqual(row('ajani')[1:], ('{1}{W}', '["W"]', '1', '2'))   # the front's colour: white in the hand and deck
            self.assertEqual(row('land'), ('', None, '[]', None, None))       # an empty text at the top is not replaced
            # What the deck builder (and the MCP's search_cards) reads: found by its rules text now, with its colours and stats
            # (the search's colour filters read color_identity, which Scryfall always writes at the top)
            found = cards.search_cards(oracle_text='noncreature spell')[0]
            self.assertEqual([(card['name'], card['colors'], card['power'], card['toughness']) for card in found],
                             [("Gandalf, Goblins' Bane // Flameshape", ['R'], '2', '3')])
            self.assertEqual([card['name'] for card in cards.search_cards(oracle_text='Flying')[0]],
                             ['Delver of Secrets // Insectile Aberration'])        # a back face's text is found too
        finally:
            cards.conn.close()

    def test_a_reversible_card_is_the_card_it_is(self):
        # Scryfall's records of two Secret Lair reversible printings: named twice over, with type, mana value, cost and
        # text only on the faces. They were cards of their own ("Ajani Goldmane // Ajani Goldmane", no type, mana value 0)
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            face = {'oracle_id': 'oracle-ajani', 'layout': 'normal', 'name': 'Ajani Goldmane', 'mana_cost': '{2}{W}{W}', 'cmc': 4.0,
                    'type_line': 'Legendary Planeswalker \u2014 Ajani', 'oracle_text': '+1: You gain 2 life.', 'colors': ['W'],
                    'loyalty': '4', 'image_uris': {'normal': 'https://img/ajani-front.jpg'}}
            ajani_sld = {**self.card(1), 'id': 'ajani-sld', 'name': 'Ajani Goldmane // Ajani Goldmane', 'layout': 'reversible_card',
                         'set': 'sld', 'color_identity': ['W'], 'image_uris': None, 'card_faces': [face, {**face}]}
            del ajani_sld['image_uris']
            ajani = {**self.card(2), 'id': 'ajani-lrw', 'name': 'Ajani Goldmane', 'set': 'lrw', 'oracle_id': 'oracle-ajani',
                     'mana_cost': '{2}{W}{W}', 'cmc': 4.0, 'type_line': 'Legendary Planeswalker \u2014 Ajani', 'colors': ['W'],
                     'color_identity': ['W'], 'oracle_text': '+1: You gain 2 life.'}
            regent = {'oracle_id': 'oracle-regent', 'layout': 'adventure', 'name': 'Bloomvine Regent', 'mana_cost': '{3}{G}{G}',
                      'cmc': 5.0, 'type_line': 'Creature \u2014 Dragon', 'oracle_text': 'Flying', 'colors': ['G'],
                      'power': '4', 'toughness': '5'}
            bloomvine = {**self.card(3), 'id': 'bloomvine-sld', 'name': 'Bloomvine Regent // Claim Territory // Bloomvine Regent',
                         'layout': 'reversible_card', 'set': 'sld', 'color_identity': ['G'],
                         'card_faces': [regent, {**regent, 'name': 'Claim Territory', 'type_line': 'Sorcery \u2014 Omen'}]}
            cards.populate_database([self.card(n) for n in range(100, 140)] + [ajani_sld, ajani, bloomvine])
            row = lambda card_id: tuple(cards.conn.execute(
                'SELECT name, name_search, type_line, cmc, mana_cost, colors, oracle_text, oracle_id, image_uri, power, toughness '
                'FROM cards WHERE id = ?', (card_id,)).fetchone())
            self.assertEqual(row('ajani-sld'), ('Ajani Goldmane', 'ajani goldmane', 'Legendary Planeswalker \u2014 Ajani', 4.0,
                                                '{2}{W}{W}', '["W"]', '+1: You gain 2 life.', 'oracle-ajani',
                                                'https://img/ajani-front.jpg', None, None))
            self.assertEqual(row('bloomvine-sld')[:4], ('Bloomvine Regent // Claim Territory', 'bloomvine regent // claim territory',
                                                        'Creature \u2014 Dragon', 5.0))
            self.assertEqual(row('bloomvine-sld')[9:], ('4', '5'))
            # One Ajani in the deck search, not two - the Secret Lair printing is one of its printings
            self.assertEqual([card['name'] for card in cards.search_cards(text='ajani')[0]], ['Ajani Goldmane'])
            self.assertEqual({card['id'] for card in cards.find_printings('Ajani Goldmane')[1]}, {'ajani-sld', 'ajani-lrw'})
        finally:
            cards.conn.close()

    def test_the_deck_search_offers_only_cards_that_can_be_played_where_the_deck_is(self):
        # What the live search offered: a Dungeon, an Attraction and a Plane for "Treasure", an Alchemy-only card to a
        # paper deck, Black Lotus to an Arena deck. Scryfall's legalities and games, as the import stores them
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            both = {**self.card(1), 'id': 'shock', 'name': 'Shock', 'games': ['paper', 'arena', 'mtgo'],
                    'legalities': {'standard': 'legal', 'timeless': 'legal'}}
            lotus = {**self.card(2), 'id': 'lotus', 'name': 'Black Lotus', 'games': ['paper'], 'legalities': {'vintage': 'restricted'}}
            # Scryfall's Alchemy: Dominaria printing of it: on Arena by its games, legal in no Arena format (an event's)
            lotus_ydmu = {**lotus, 'id': 'lotus-ydmu', 'set': 'ydmu', 'collector_number': '35', 'games': ['arena']}
            alchemy = {**self.card(3), 'id': 'cygnet', 'name': 'Awestruck Cygnet', 'games': ['arena'], 'legalities': {'alchemy': 'legal'}}
            # (the self.card ones are legal in vintage only: no Arena format)
            token = {**self.card(4), 'id': 'tok', 'name': 'Treasure', 'layout': 'token', 'games': ['paper', 'arena'], 'legalities': {}}
            plane = {**self.card(5), 'id': 'plane', 'name': 'City Hall', 'type_line': 'Plane \u2014 Chicago', 'games': ['paper'],
                     'legalities': {}}
            unknown = {**self.card(6), 'id': 'old', 'name': 'Old Record'}             # no games: a record from before it existed
            older = {**self.card(7), 'id': 'older', 'name': 'Older File'}
            cards.populate_database([self.card(n) for n in range(100, 140)] + [both, lotus, lotus_ydmu, alchemy, token, plane, unknown, older])
            # A card data from before legalities were stored: the column added by ALTER TABLE, NULL
            cards.conn.execute("UPDATE cards SET legalities = NULL, games = NULL WHERE id = 'older'")
            cards.conn.commit()
            names = lambda **filters: sorted(card['name'] for card in cards.search_cards(limit=200, **filters)[0]
                                             if not card['name'].startswith('Card '))
            self.assertEqual(names(), ['Awestruck Cygnet', 'Black Lotus', 'Old Record', 'Older File', 'Shock'])   # no token, no Plane
            self.assertEqual(names(playable_in='paper'), ['Black Lotus', 'Old Record', 'Older File', 'Shock'])
            # A card without games on Arena only by its legalities; Black Lotus not even by its Alchemy printing
            self.assertEqual(names(playable_in='arena'), ['Awestruck Cygnet', 'Older File', 'Shock'])
            cards.conn.execute("UPDATE cards SET legalities = '{\"timeless\": \"legal\"}' WHERE id = 'old'")
            cards.conn.commit()
            self.assertEqual(names(playable_in='arena'), ['Awestruck Cygnet', 'Old Record', 'Older File', 'Shock'])
            self.assertEqual(names(text='treasure'), [])
            # The scanner still finds the token by its printing - it stays in the card data
            self.assertEqual(cards.search_card_exact('Treasure', '4', 'TST')['name'], 'Treasure')
            with self.assertRaises(ValueError):
                cards.search_cards(playable_in='mtgo')
        finally:
            cards.conn.close()

    def test_more_of_scryfalls_card_is_kept_and_searched(self):
        # Loyalty (on the faces of a reversible card), the mana a card makes, EDHREC's rank, the layout, Game Changer, Arena id
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            elves = {**self.card(1), 'id': 'elves', 'name': 'Llanowar Elves', 'produced_mana': ['G'], 'edhrec_rank': 120,
                     'arena_id': 68526, 'power': '1', 'toughness': '1'}
            tender = {**self.card(2), 'id': 'tender', 'name': 'Bloom Tender', 'produced_mana': ['B', 'G', 'R', 'U', 'W'],
                      'edhrec_rank': 900}
            rhystic = {**self.card(3), 'id': 'rhystic', 'name': 'Rhystic Study', 'game_changer': True, 'edhrec_rank': 7}
            face = {'name': 'Ajani Goldmane', 'mana_cost': '{2}{W}{W}', 'cmc': 4.0, 'type_line': 'Legendary Planeswalker',
                    'oracle_text': '+1: You gain 2 life.', 'colors': ['W'], 'loyalty': '4'}
            ajani = {**self.card(4), 'id': 'ajani', 'name': 'Ajani Goldmane // Ajani Goldmane', 'layout': 'reversible_card',
                     'card_faces': [face, {**face}]}
            cards.populate_database([self.card(n) for n in range(100, 140)] + [elves, tender, rhystic, ajani])
            row = lambda card_id: tuple(cards.conn.execute(
                'SELECT loyalty, produced_mana, edhrec_rank, layout, game_changer, arena_id, face_name_search FROM cards '
                'WHERE id = ?', (card_id,)).fetchone())
            self.assertEqual(row('elves'), (None, '["G"]', 120, 'normal', 0, 68526, 'llanowar elves'))
            self.assertEqual(row('rhystic')[2:5], (7, 'normal', 1))
            self.assertEqual(row('ajani')[0], '4')                               # from the face
            self.assertEqual(row('id-100')[1], None)                              # a card that makes no mana
            card = cards.cards_by_names(['Rhystic Study'])['rhystic study']
            self.assertEqual((card['game_changer'], card['edhrec_rank']), (True, 7))
            names = lambda **filters: [c['name'] for c in cards.search_cards(**filters)[0] if not c['name'].startswith('Card ')]
            self.assertEqual(names(produces='G'), ['Bloom Tender', 'Llanowar Elves'])
            self.assertEqual(names(produces='U'), ['Bloom Tender'])
            self.assertEqual(names(produces='GU'), ['Bloom Tender'])
            # By EDHREC's rank (cards no Commander deck plays last); after the exact name when there is a text
            self.assertEqual(names(sort='popularity')[:4], ['Rhystic Study', 'Llanowar Elves', 'Bloom Tender', 'Ajani Goldmane'])
            self.assertEqual(names(text='llanowar elves', sort='popularity'), ['Llanowar Elves'])
            with self.assertRaises(ValueError):
                cards.search_cards(sort='price')
        finally:
            cards.conn.close()

    def test_a_double_faced_card_is_found_by_its_front_by_an_index(self):
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            delver = {**self.card(1), 'id': 'delver', 'name': 'Delver of Secrets // Insectile Aberration', 'layout': 'transform'}
            # One front, two cards: a token (legal nowhere) and a legal card - the legal one, whatever the table's order
            token = {**self.card(2), 'id': 'tok', 'name': 'Angel // Demon', 'layout': 'token', 'legalities': {},
                     'released_at': '2030-01-01'}
            legal = {**self.card(3), 'id': 'legal', 'name': 'Angel // Angel Reborn', 'released_at': '2020-01-01'}
            older = {**self.card(4), 'id': 'old', 'name': 'Old Front // Back', 'released_at': '2001-01-01'}
            newer = {**self.card(5), 'id': 'new', 'name': 'Old Front // Other Back', 'released_at': '2024-01-01'}
            cards.populate_database([self.card(n) for n in range(100, 140)] + [delver, token, legal, older, newer])
            found = cards.cards_by_names(['Delver of Secrets', 'Angel', 'Old Front', 'Nothing Like It'])
            self.assertEqual({key: card['name'] for key, card in found.items()},
                             {'delver of secrets': 'Delver of Secrets // Insectile Aberration', 'angel': 'Angel // Angel Reborn',
                              'old front': 'Old Front // Other Back'})
            # By the index: a name not found no longer reads every row
            query = cards._ONE_PER_NAME.format(where="face_name_search IN (?) AND face_name_search != name_search")
            plan = ' '.join(row[3] for row in cards.conn.execute('EXPLAIN QUERY PLAN ' + query, ('x',)))
            self.assertIn('idx_card_face_name_search', plan)
        finally:
            cards.conn.close()

    def test_an_older_file_gets_the_front_names_and_loses_the_unused_indexes(self):
        # A file from before face_name_search: filled from the names at the start, no download needed
        path = self.tmp / 'old_cards.db'
        old = sqlite3.connect(path)
        old.execute('CREATE TABLE cards (id TEXT PRIMARY KEY, name TEXT NOT NULL, rarity TEXT, type_line TEXT)')
        old.execute("INSERT INTO cards (id, name) VALUES ('d', 'Delver of Secrets // Insectile Aberration'), ('s', 'Shock')")
        old.execute('CREATE INDEX idx_card_rarity ON cards(rarity)')
        old.execute('CREATE INDEX idx_card_type ON cards(type_line)')
        old.commit()
        old.close()
        cards = CardDatabase(db_file=path)
        try:
            self.assertEqual(dict(cards.conn.execute('SELECT id, face_name_search FROM cards').fetchall()),
                             {'d': 'delver of secrets', 's': 'shock'})
            indexes = {row[0] for row in cards.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            self.assertIn('idx_card_face_name_search', indexes)
            self.assertFalse({'idx_card_rarity', 'idx_card_type'} & indexes)
        finally:
            cards.conn.close()

    def test_an_import_on_its_own_connection(self):
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            cards.conn.execute("INSERT INTO cards (id, name) VALUES ('old', 'Old Card')")   # not committed: a search's connection
            cards.conn.rollback()
            self.assertEqual(cards.populate_database([self.card(n) for n in range(40)]), 40)
            self.assertEqual(cards.conn.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 40)
            self.assertEqual(cards.get_data_info('mtg')['card_count'], 40)
            self.assertFalse(cards.conn.in_transaction)
            with self.assertRaises(RuntimeError):                                      # a download cut short
                cards.populate_database([self.card(n) for n in range(5)])
            self.assertEqual(cards.conn.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 40)
            cards._import_lock.acquire()
            try:
                with self.assertRaises(RuntimeError):                                  # one refresh at a time
                    cards.populate_database([self.card(n) for n in range(40)])
            finally:
                cards._import_lock.release()
        finally:
            cards.conn.close()


class StationUploads(unittest.TestCase):
    def test_a_picture_is_deleted_only_when_the_server_has_it(self):
        import importlib.util
        if importlib.util.find_spec('socketio') is None:
            self.skipTest('the station client needs python-socketio')
        from card_scanner.station.station_client import upload_verdict

        class Answer:
            def __init__(self, status, body):
                self.status_code, self.body = status, body

            def json(self):
                if not isinstance(self.body, (dict, list)):
                    raise ValueError('not JSON')
                return self.body
        for status, body, verdict in (
                (200, {'success': True, 'capture': 7, 'status': 'added'}, 'accepted'),
                (202, {'success': True, 'capture': 7, 'status': 'pending'}, 'accepted'),
                (200, {'success': False, 'capture': 7, 'status': 'error'}, 'accepted'),   # on record there, failed there
                (400, {'success': False, 'message': 'No readable picture'}, 'refused'),
                (413, {'success': False, 'message': 'Capture too large'}, 'refused'),
                (401, {'success': False, 'message': 'Wrong token'}, 'retry'),
                (404, '<html>Not Found</html>', 'retry'),                               # a proxy, another program
                (200, '<html>login</html>', 'retry'),
                (200, {'success': True}, 'retry'),
                (502, 'Bad Gateway', 'retry')):
            self.assertEqual(upload_verdict(Answer(status, body)), verdict, (status, body))


class FullBackup(TemporaryData):
    def setUp(self):
        super().setUp()
        import threading
        self.barrier = threading.RLock()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.saved_review, self.saved_full = review_store.REVIEW_DIR, backups.FULL_DIR
        review_store.REVIEW_DIR = self.tmp / 'review'
        backups.FULL_DIR = self.tmp / 'full'
        self.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')
        (self.tmp / 'settings.json').write_text('{}')

    def tearDown(self):
        self.pending.conn.close()
        self.review.conn.close()
        review_store.REVIEW_DIR, backups.FULL_DIR = self.saved_review, self.saved_full
        super().tearDown()

    def full(self, large=False, **kwargs):
        pictures = 'SELECT file FROM inventory_captures'
        # The large copies are optional pictures: a capture made before they were kept has none
        optional = [('captures_full', inv.FULL_CAPTURES_DIR, self.tmp / 'collection.db', pictures, True),
                    ('captures_full', inv.FULL_CAPTURES_DIR, self.tmp / 'scan.db', pictures, True)] if large else []
        return backups.create_full(
            managers=[self.collection, self.scan, self.decks, self.review, self.pending], barrier=self.barrier,
            databases=[self.tmp / 'collection.db', self.tmp / 'scan.db', self.tmp / 'pending.db'],
            linked=[('captures', inv.CAPTURES_DIR, self.tmp / 'collection.db', pictures),
                    ('captures', inv.CAPTURES_DIR, self.tmp / 'scan.db', pictures),
                    ('review', review_store.REVIEW_DIR, self.tmp / 'collection.db', 'SELECT file FROM review_queue'),
                    ('scanned_cards', self.tmp, self.tmp / 'pending.db', 'SELECT image FROM pending_captures')] + optional,
            copied=[self.tmp / 'settings.json', self.tmp / 'not-there.json'], **kwargs)

    def test_large_copies_that_some_captures_lack_are_not_missing(self):
        self.add('desk-a', copies=2)
        self.collection.add_card(CARD, GAME, 'regular', capture=self.photo, quiet=True)
        kept = sorted(inv.FULL_CAPTURES_DIR.glob('*.jpg'))
        self.assertEqual(len(kept), 3)
        kept[0].unlink()                                   # a capture from before the large copies were kept
        made = self.full(large=True)
        self.assertTrue(made['verified'], made['missing'])
        self.assertEqual(made['missing'], [])
        self.assertEqual(made['files']['captures_full'], 2)  # only what exists is counted
        self.assertEqual(made['files']['captures'], 3)        # the thumbnails are not optional
        folder = Path(made['folder'])
        self.assertEqual(len(list((folder / 'captures_full').glob('*.jpg'))), 2)

    def test_everything_at_one_moment_and_usable_elsewhere(self):
        self.add('desk-a', copies=2)
        self.collection.add_card(CARD, GAME, 'regular', capture=self.photo, quiet=True)
        self.review.add(GAME, self.photo, 'Unread', station='desk-a')
        self.pending.add(GAME, 'desk-a', 3, 'capture.jpg', None, True, 'now', capture_id='c3', sender='desk-a')
        # A move to the collection cut short after its first commit: noted, nothing arrived
        self.scan.conn.execute("INSERT INTO pending_moves (game, move_id, added_at) VALUES ('mtg', 'm1', 'now')")
        self.scan.conn.commit()
        made = self.full()
        self.assertTrue(made['verified'], made['missing'])
        self.assertEqual(made['files'], {'captures': 3, 'review': 1, 'scanned_cards': 1})
        self.assertEqual(made['databases']['scan.db']['inventory'], 1)
        self.assertEqual(made['copied'], ['settings.json'])
        self.assertEqual([item['id'] for item in backups.list_full()], [made['id']])

        # What is there now goes away; the backup, opened in a folder of its own, is a working state
        self.scan.clear_inventory(GAME)
        self.collection.clear_inventory(GAME)
        folder = Path(made['folder'])
        captures, inv.CAPTURES_DIR = inv.CAPTURES_DIR, folder / 'captures'
        try:
            collection = inv.InventoryManager(db_file=folder / 'collection.db')
            scan = inv.InventoryManager(db_file=folder / 'scan.db')
            self.assertEqual(collection.finish_interrupted_moves(scan), [])          # the cut-short move: cleared, cards stay
            self.assertEqual(scan.conn.execute('SELECT COUNT(*) FROM pending_moves').fetchone()[0], 0)
            self.assertEqual((scan.get_stats(GAME)['total_cards'], collection.get_stats(GAME)['total_cards']), (2, 1))
            for manager in (collection, scan):
                for entry in manager.get_all_cards(GAME):
                    for capture in entry['captures']:
                        self.assertTrue((folder / 'captures' / Path(capture['url']).name).is_file())
            waiting = pending_store.PendingCaptures(db_file=folder / 'pending.db')
            self.assertEqual([row['image'] for row in waiting.all()], ['capture.jpg'])
            self.assertTrue((folder / 'scanned_cards' / 'capture.jpg').is_file())
            for conn in (collection.conn, scan.conn, waiting.conn):
                conn.close()
        finally:
            inv.CAPTURES_DIR = captures

    def test_it_waits_for_what_is_under_way_and_keeps_the_newest(self):
        import threading
        self.add('desk-a')
        done = []
        with self.barrier:                                       # a capture being settled
            worker = threading.Thread(target=lambda: done.append(self.full(keep=1)))
            worker.start()
            worker.join(0.3)
            self.assertEqual(done, [])                           # not while that is half done
        worker.join(10)
        self.assertEqual(len(done), 1)
        import time as clock
        clock.sleep(1.1)                                         # names are to the second
        second = self.full(keep=1)
        self.assertEqual([item['id'] for item in backups.list_full()], [second['id']])

    def test_a_picture_that_is_gone_is_reported(self):
        self.add('desk-a')
        for file in inv.CAPTURES_DIR.glob('*.jpg'):
            file.unlink()
        made = self.full()
        self.assertFalse(made['verified'])
        self.assertEqual(len(made['missing']), 1)


if __name__ == '__main__':
    unittest.main()
