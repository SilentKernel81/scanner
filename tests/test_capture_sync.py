"""
A phone and the server agree on each scanned card (server/stations_api.py: station_capture_sync):
the phone asks where the copy of one capture is, and can remove exactly that copy - from the
scanned cards or the review queue, never from the collection.

Two layers, both on temporary databases:

  - CopyOfACapture: the storage (inventory.py: captured_copy, review.py: by_capture) - the
    identity of a capture through duplicates, splits, merges, printing changes, backups,
    restarts and the move into the collection;
  - SyncRoute: the two routes themselves, called through Flask's test client with the server's
    shared objects replaced by temporary ones - what a phone is answered in each situation.

    venv/bin/python -m unittest discover tests -p test_capture_sync.py
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_ownership: the temporary data

import inventory as inv  # noqa: E402
import pending as pending_store  # noqa: E402
import review as review_store  # noqa: E402
from config import Config  # noqa: E402
from stations import Stations  # noqa: E402
from test_ownership import CARD, GAME, TemporaryData  # noqa: E402

OTHER_CARD = dict(CARD, name='Other Card', number='2')
PRINTING = {'card_id': None, 'card_name': 'Test Card', 'set_name': 'Another Set', 'set_code': 'ano',
            'card_number': '7', 'rarity': None, 'type_line': None, 'mana_cost': None, 'colors': None,
            'color_identity': None}


def rows(manager):
    """Everything in an inventory that makes up its cards: to show that nothing changed"""
    return [tuple(row) for table in ('inventory', 'inventory_captures', 'inventory_sources')
            for row in manager.conn.execute(f'SELECT * FROM {table} ORDER BY 1, 2')]


class CopyOfACapture(TemporaryData):
    """inventory.captured_copy: which copy a capture is, and removing that one"""

    def scan_card(self, key, station='desk-a', card=CARD, **kwargs):
        return self.scan.add_card(card, GAME, 'regular', source=station, capture=self.photo, capture_key=key,
                                  quiet=True, **kwargs)

    def state(self, key, station='desk-a'):
        return self.scan.captured_copy(key, station)[0]

    def remove(self, key, station='desk-a'):
        return self.scan.captured_copy(key, station, remove=True)[0]

    def photos(self):
        return len(list(inv.CAPTURES_DIR.glob('*.jpg')))

    def test_of_identical_cards_the_one_named_goes(self):
        for key in ('first', 'second', 'third'):
            self.scan_card(key)                                   # one entry, three copies
        self.scan_card('other-desk', station='desk-b')
        self.assertEqual(self.remove('second'), 'removed')
        self.assertEqual((self.total('desk-a'), self.total('desk-b'), self.total()), (2, 1, 3))
        self.assertEqual(self.photos(), 3)                        # its photo went with it
        self.assertEqual([self.state(key) for key in ('first', 'third')], ['scanned', 'scanned'])
        self.assertEqual(self.state('other-desk', 'desk-b'), 'scanned')

    def test_removing_again_removes_nothing_more(self):
        self.scan_card('first')
        self.scan_card('second')
        self.assertEqual(self.remove('second'), 'removed')
        self.assertIsNone(self.remove('second'))                   # gone: the route says 'resolved'
        self.assertIsNone(self.remove('second'))
        self.assertEqual(self.total(), 1)
        self.assertEqual(self.state('first'), 'scanned')

    def test_the_last_copy_takes_its_entry_along(self):
        self.scan_card('only')
        self.assertEqual(self.remove('only'), 'removed')
        self.assertEqual(rows(self.scan), [])

    def test_another_station_cannot_ask_about_or_remove_it(self):
        self.scan_card('first')
        before = rows(self.scan)
        self.assertEqual(self.state('first', 'desk-b'), 'unfollowed')      # never "gone"
        self.assertEqual(self.remove('first', 'desk-b'), 'unfollowed')
        self.assertEqual(rows(self.scan), before)

    def test_a_capture_never_added_is_not_here(self):
        self.scan_card('first')
        self.assertIsNone(self.state('never-seen'))
        self.assertIsNone(self.remove('never-seen'))
        self.assertEqual(self.total(), 1)

    def test_a_copy_without_a_photo_cannot_be_followed(self):
        """No photo row carries the capture: it must not read as gone while the card is there"""
        self.scan.add_card(CARD, GAME, 'regular', source='desk-a', capture=None, capture_key='blind', quiet=True)
        self.assertEqual(self.state('blind'), 'unfollowed')
        self.assertEqual(self.remove('blind'), 'unfollowed')
        self.assertEqual(self.total(), 1)

    def test_identity_follows_the_copy_through_a_split(self):
        entry = self.scan_card('first', location='Box 1')
        self.scan_card('second', location='Box 1')
        # The newest copy (and its photo) moves to Box 2: 'second'
        self.scan.update_card(entry, location='Box 2', split_quantity=1, quiet=True)
        self.assertEqual(self.cards(), {('Test Card', 'regular', 'Box 1'): 1, ('Test Card', 'regular', 'Box 2'): 1})
        self.assertEqual(self.remove('second'), 'removed')
        self.assertEqual(self.cards(), {('Test Card', 'regular', 'Box 1'): 1})   # the Box 2 copy went
        self.assertEqual(self.state('first'), 'scanned')

    def test_identity_follows_the_copy_through_a_merge(self):
        one = self.scan_card('first', location='Box 1')
        self.scan_card('second', location='Box 2')
        self.scan.update_card(one, location='Box 2', quiet=True)   # becomes identical: merged
        self.assertEqual(self.cards(), {('Test Card', 'regular', 'Box 2'): 2})
        self.assertEqual(self.remove('first'), 'removed')
        self.assertEqual(self.cards(), {('Test Card', 'regular', 'Box 2'): 1})
        self.assertEqual(self.state('second'), 'scanned')

    def test_identity_follows_the_copy_to_another_printing(self):
        entry = self.scan_card('first')
        self.scan_card('second')
        self.scan.update_card(entry, printing=PRINTING, split_quantity=1, quiet=True)   # one copy is another printing
        self.assertEqual(len(self.scan.get_all_cards(GAME)), 2)
        self.assertEqual(self.remove('second'), 'removed')
        left = self.scan.get_all_cards(GAME)
        self.assertEqual([(card['set_name'], card['quantity']) for card in left], [('Test Set', 1)])
        # ... and a whole entry changed to another printing keeps its copy's identity too
        self.scan.update_card(left[0]['id'], printing=PRINTING, quiet=True)
        self.assertEqual(self.state('first'), 'scanned')
        self.assertEqual(self.remove('first'), 'removed')
        self.assertEqual(self.total(), 0)

    def test_two_stations_one_entry_split(self):
        """After a split a photo can sit in an entry where its station has no row of its own
        (_move_sources decides by the newest photos): the copy is still found and removed, and
        the stations never claim more copies than are left"""
        entry = self.scan_card('a1', location='Box 1')
        self.scan_card('b1', station='desk-b', location='Box 1')
        self.scan_card('a2', location='Box 1')
        self.scan.update_card(entry, location='Box 2', split_quantity=2, quiet=True)
        for key, station in (('a1', 'desk-a'), ('b1', 'desk-b'), ('a2', 'desk-a')):
            self.assertEqual(self.state(key, station), 'scanned')
        self.assertEqual(self.remove('b1', 'desk-b'), 'removed')
        self.assertEqual(self.total(), 2)
        for row in self.scan.conn.execute('''SELECT i.quantity, COALESCE(SUM(s.quantity), 0) AS claimed FROM inventory i
                                             LEFT JOIN inventory_sources s ON s.inventory_id = i.id GROUP BY i.id'''):
            self.assertLessEqual(row['claimed'], row['quantity'])
        self.assertEqual(self.remove('a2'), 'removed')
        self.assertEqual(self.remove('a1'), 'removed')
        self.assertEqual(rows(self.scan), [])

    def test_in_the_collection_it_is_out_of_reach(self):
        self.scan_card('first', location='Shelf')
        self.scan_card('second')
        self.scan_card('other-desk', station='desk-b', card=OTHER_CARD)
        self.collection.take_from(self.scan, GAME, location='Box 1', station='desk-a')
        collection = rows(self.collection)
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 2)
        self.assertEqual({card['location'] for card in self.collection.get_all_cards(GAME)}, {'Box 1'})
        for key in ('first', 'second'):
            self.assertIsNone(self.state(key))                   # the route says 'resolved'
            self.assertIsNone(self.remove(key))
        self.assertEqual(rows(self.collection), collection)       # nothing left the collection
        self.assertEqual(self.state('other-desk', 'desk-b'), 'scanned')   # the other camera's card waits on
        # The collection's photos carry no capture: nothing there can ever be matched to a phone's row
        self.assertEqual(self.collection.conn.execute(
            'SELECT COUNT(*) FROM inventory_captures WHERE capture_key IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(self.collection.captured_copy('first', 'desk-a', remove=True)[0], None)

    def test_a_removed_capture_read_again_adds_nothing(self):
        """Uploads are 'at least once': the capture's uid stays noted after its copy went"""
        self.scan_card('first')
        self.remove('first')
        self.scan_card('first')            # the same capture, read again after a crash
        self.assertEqual(self.total(), 0)

    def test_after_a_restart(self):
        self.scan_card('first')
        self.scan_card('second')
        self.scan.conn.close()
        self.scan = inv.InventoryManager(db_file=self.tmp / 'scan.db')
        self.assertEqual(self.state('first'), 'scanned')
        self.assertEqual(self.remove('second'), 'removed')
        self.assertEqual(self.total(), 1)

    def test_undo_does_not_take_another_copy(self):
        self.scan_card('first')
        self.scan_card('second')            # the station's Undo points at this one
        self.remove('second')
        self.assertIsNone(self.scan.undo_last_add(source='desk-a'))
        self.assertEqual(self.total(), 1)

    def test_a_backup_brings_the_identity_back(self):
        self.scan_card('first')
        backup = self.backup()
        self.remove('first')
        self.restore(backup)
        self.assertEqual(self.state('first'), 'scanned')

    def test_a_file_from_before_gets_the_column_and_keeps_its_cards(self):
        """The migration: a scanned-cards file without capture_key is opened by the new code"""
        self.scan_card('first')
        self.scan.conn.close()
        old = sqlite3.connect(self.tmp / 'scan.db')
        old.execute('DROP INDEX idx_captures_key')
        old.execute('ALTER TABLE inventory_captures DROP COLUMN capture_key')
        old.execute('DROP TABLE unfollowed_captures')
        before = [old.execute(f'SELECT * FROM {table}').fetchall()
                  for table in ('inventory', 'inventory_captures', 'inventory_sources')]
        old.commit()
        old.close()
        self.scan = inv.InventoryManager(db_file=self.tmp / 'scan.db')
        columns = [row['name'] for row in self.scan.conn.execute('PRAGMA table_info(inventory_captures)')]
        self.assertIn('capture_key', columns)
        indexes = {row['name'] for row in self.scan.conn.execute('PRAGMA index_list(inventory_captures)')}
        self.assertIn('idx_captures_key', indexes)
        without_key = ', '.join(column for column in columns if column != 'capture_key')
        after = [[tuple(row) for row in self.scan.conn.execute(f'SELECT {select} FROM {table}')]
                 for table, select in (('inventory', '*'), ('inventory_captures', without_key), ('inventory_sources', '*'))]
        self.assertEqual(after, before)
        # The copy that was there has no capture any more: not found, the route says so by
        # the capture's outcome (no sync_key) - and a new one is followed
        self.scan_card('second')
        self.assertEqual(self.state('second'), 'scanned')


class SyncRoute(TemporaryData):
    """GET / DELETE /api/stations/<id>/captures/<n>/sync, with temporary data behind the server"""

    def setUp(self):
        super().setUp()
        import app as launcher  # the server's routes (nothing is started, no database opened)
        from server import desks as desks_module
        from server import state
        self.state, self.desks = state, desks_module
        self.saved_review_dir = review_store.REVIEW_DIR
        review_store.REVIEW_DIR = self.tmp / 'review'
        self.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.stations = Stations(file=self.tmp / 'stations.json')
        services = state.services
        self.saved_services = {name: getattr(services, name) for name in (
            'scan_inventory', 'inventory', 'review', 'pending', 'stations', 'camera_hub', 'default_desk')}
        services.scan_inventory, services.inventory = self.scan, self.collection
        services.review, services.pending, services.stations = self.review, self.pending, self.stations
        services.camera_hub = mock.Mock()                 # a desk asks it for the station's camera
        services.default_desk = desks_module.Desk()
        desks_module.desks.clear()
        state.capture_outcomes.clear()
        self.saved_token = Config.STATION_TOKEN
        Config.STATION_TOKEN = ''
        self.client = launcher.app.test_client()
        self.numbers = {}

    def tearDown(self):
        Config.STATION_TOKEN = self.saved_token
        for name, value in self.saved_services.items():
            setattr(self.state.services, name, value)
        self.desks.desks.clear()
        self.state.capture_outcomes.clear()
        self.review.conn.close()
        self.pending.conn.close()
        review_store.REVIEW_DIR = self.saved_review_dir
        super().tearDown()

    # What server/captures.py: submit_capture does with a capture, without the reading itself
    def upload(self, station='phone-a'):
        """A capture arrives and waits to be read; returns (its number, its record's id, its uid)"""
        number = self.stations.capture(station, station)['captures']
        pending_id, uid = self.pending.add(GAME, station, number, 'card.jpg', None, False, '2026-10-12 10:00:00',
                                           capture_id=f'{station}-{number}', sender=station)
        return number, pending_id, uid

    def settle(self, pending_id, outcome, station, number, remember=True):
        self.pending.settle(pending_id, outcome)
        if remember:
            self.state.capture_outcomes[(station, number)] = outcome

    def scanned(self, station='phone-a', card=CARD, remember=True, **kwargs):
        """A capture read and added to the scanned cards; returns (its number, its uid)"""
        number, pending_id, uid = self.upload(station)
        self.scan.add_card(card, GAME, 'regular', source=station, capture=self.photo, capture_key=uid, quiet=True,
                           **kwargs)
        self.settle(pending_id, {'status': 'added', 'sync_key': uid}, station, number, remember)
        return number, uid

    def queued(self, station='phone-a', remember=True):
        """A capture read and queued for review; returns (its number, its uid, the item's id)"""
        number, pending_id, uid = self.upload(station)
        item = self.review.add(GAME, self.photo, 'Unknown', station=station, capture_key=uid)
        self.settle(pending_id, {'status': 'review', 'reason': 'not found', 'sync_key': uid}, station, number, remember)
        return number, uid, item

    def url(self, number, station='phone-a'):
        return f'/api/stations/{station}/captures/{number}/sync'

    def ask(self, number, station='phone-a'):
        return self.client.get(self.url(number, station))

    def delete(self, number, station='phone-a'):
        return self.client.delete(self.url(number, station))

    def state_of(self, number, station='phone-a'):
        answer = self.ask(number, station)
        self.assertEqual((answer.status_code, answer.json['success']), (200, True), answer.json)
        return answer.json['state']

    def test_duplicates_one_removed_twice(self):
        first, _ = self.scanned()
        second, _ = self.scanned()
        third, _ = self.scanned()
        self.assertEqual([self.state_of(n) for n in (first, second, third)], ['scanned'] * 3)
        self.assertEqual(self.delete(second).json, {'success': True, 'state': 'removed'})
        self.assertEqual(self.total(), 2)
        self.assertEqual(self.delete(second).json, {'success': True, 'state': 'resolved'})   # harmless
        self.assertEqual(self.delete(second).json, {'success': True, 'state': 'resolved'})
        self.assertEqual(self.total(), 2)
        self.assertEqual([self.state_of(n) for n in (first, second, third)], ['scanned', 'resolved', 'scanned'])

    def test_a_station_reaches_only_its_own_captures(self):
        mine, _ = self.scanned('phone-a')
        theirs, _ = self.scanned('phone-b', card=OTHER_CARD)
        self.assertEqual(mine, theirs)          # both are their station's capture 1
        self.assertEqual(self.delete(mine, 'phone-a').json['state'], 'removed')
        self.assertEqual(self.cards(), {('Other Card', 'regular', ''): 1})    # phone-b's card is untouched
        self.assertEqual(self.state_of(theirs, 'phone-b'), 'scanned')
        # A number the other station never had: unknown, refused - not 'resolved'
        answer = self.delete(7, 'phone-b')
        self.assertEqual((answer.status_code, answer.json['success']), (404, False))
        self.assertEqual(self.total(), 1)

    def test_the_token_is_asked_for(self):
        number, _ = self.scanned()
        Config.STATION_TOKEN = 'secret'
        self.assertEqual(self.ask(number).status_code, 401)
        self.assertEqual(self.delete(number).status_code, 401)
        self.assertEqual(self.client.delete(self.url(number), headers={'X-Station-Token': 'wrong'}).status_code, 401)
        self.assertEqual(self.total(), 1)
        answer = self.client.delete(self.url(number), headers={'X-Station-Token': 'secret'})
        self.assertEqual(answer.json, {'success': True, 'state': 'removed'})

    def test_while_it_is_being_read_nothing_is_removed(self):
        number, pending_id, uid = self.upload()
        self.assertEqual(self.state_of(number), 'pending')
        self.assertEqual(self.delete(number).json, {'success': True, 'state': 'pending'})
        # Read and added meanwhile: the phone's next try removes it
        self.scan.add_card(CARD, GAME, 'regular', source='phone-a', capture=self.photo, capture_key=uid, quiet=True)
        self.settle(pending_id, {'status': 'added', 'sync_key': uid}, 'phone-a', number)
        self.assertEqual(self.state_of(number), 'scanned')
        self.assertEqual(self.delete(number).json['state'], 'removed')
        self.assertEqual(self.total(), 0)

    def test_a_failed_capture_kept_for_another_try_is_pending(self):
        """Its outcome in this run says 'error', but it is read again at the next start: the
        phone must not be told it is gone, and then find the card added after all"""
        number, pending_id, _ = self.upload()
        error = {'status': 'error', 'message': 'database is locked'}
        self.assertTrue(self.pending.fail(pending_id, 'database is locked', error))   # kept
        self.state.capture_outcomes[('phone-a', number)] = error
        self.assertEqual(self.state_of(number), 'pending')
        self.assertEqual(self.delete(number).json['state'], 'pending')

    def test_a_capture_that_failed_for_good_is_refused(self):
        number, pending_id, _ = self.upload()
        error = {'status': 'error', 'message': 'no'}
        for _ in range(pending_store.MAX_ATTEMPTS):
            self.pending.fail(pending_id, 'no', error)
        for answer in (self.ask(number), self.delete(number)):
            self.assertEqual((answer.status_code, answer.json['success']), (409, False))
            self.assertIn('failed', answer.json['message'])

    def test_unknown_and_legacy_captures_are_refused_not_resolved(self):
        self.scanned()
        unknown = self.ask(99)
        self.assertEqual((unknown.status_code, unknown.json['success']), (404, False))
        # Settled by a server from before: an outcome without the capture's identity
        number, pending_id, uid = self.upload()
        self.scan.add_card(CARD, GAME, 'regular', source='phone-a', capture=self.photo, quiet=True)
        self.settle(pending_id, {'status': 'added'}, 'phone-a', number)
        for answer in (self.ask(number), self.delete(number)):
            self.assertEqual((answer.status_code, answer.json['success']), (409, False))
            self.assertTrue(answer.json['message'])
        self.assertEqual(self.total(), 2)

    def test_a_copy_without_a_photo_is_refused(self):
        number, pending_id, uid = self.upload()
        self.scan.add_card(CARD, GAME, 'regular', source='phone-a', capture=None, capture_key=uid, quiet=True)
        self.settle(pending_id, {'status': 'added', 'sync_key': uid}, 'phone-a', number)
        for answer in (self.ask(number), self.delete(number)):
            self.assertEqual((answer.status_code, answer.json['success']), (409, False))
        self.assertEqual(self.total(), 1)

    def reopen(self):
        """The server restarted: what it remembered in memory is gone, the files are opened again"""
        self.state.capture_outcomes.clear()
        self.desks.desks.clear()
        for manager in (self.scan, self.review, self.pending):
            manager.conn.close()
        self.scan = inv.InventoryManager(db_file=self.tmp / 'scan.db')
        self.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        services = self.state.services
        services.scan_inventory, services.review, services.pending = self.scan, self.review, self.pending

    def test_after_a_restart(self):
        kept, _ = self.scanned()
        gone, _ = self.scanned()
        waiting, _, _ = self.queued()
        self.reopen()
        self.assertEqual([self.state_of(n) for n in (kept, gone, waiting)], ['scanned', 'scanned', 'review'])
        self.assertEqual(self.delete(gone).json['state'], 'removed')
        self.assertEqual(self.delete(waiting).json['state'], 'removed')
        self.reopen()
        self.assertEqual([self.state_of(n) for n in (kept, gone, waiting)], ['scanned', 'resolved', 'resolved'])
        self.assertEqual((self.total(), self.review.count(GAME)), (1, 0))

    def test_an_outcome_forgotten_is_unknown_not_resolved(self):
        number, _ = self.scanned(remember=False)
        self.pending.conn.execute('UPDATE settled_captures SET settled_at = ?',
                                  (pending_store.timestamp(pending_store.KEEP_DAYS + 1),))
        self.pending.conn.commit()
        self.reopen()                                   # old outcomes are cleared away at a start
        self.assertEqual(self.ask(number).status_code, 404)
        self.assertEqual(self.delete(number).status_code, 404)
        self.assertEqual(self.total(), 1)

    def test_review_removed_at_the_station(self):
        number, _, item = self.queued()
        other, _, other_item = self.queued('phone-b')
        self.assertEqual(self.state_of(number), 'review')
        # Its page has the item open: it must not stay there to be added after all
        desk = self.desks.desk_for('phone-a')
        desk.current_review_id, desk.current_card_info = item, {'name': 'Unknown'}
        self.assertEqual(self.delete(number).json, {'success': True, 'state': 'removed'})
        self.assertIsNone(self.review.get(item))
        self.assertEqual((desk.current_review_id, desk.current_card_info, desk.pending_capture), (None, None, None))
        self.assertEqual(self.delete(number).json, {'success': True, 'state': 'resolved'})
        self.assertIsNotNone(self.review.get(other_item))        # the other phone's item waits on
        self.assertEqual(len(list(review_store.REVIEW_DIR.glob('*.jpg'))), 1)

    def add_from_review(self, item, station='phone-a', **kwargs):
        """What server/scan_events.py: handle_add_inventory does for the open review item"""
        reviewed = self.review.get(item)
        self.scan.add_card(CARD, GAME, 'regular', source=station, capture=self.review.image_path(reviewed),
                           capture_key=reviewed['capture_key'], quiet=True, **kwargs)
        self.review.remove(item)

    def test_added_from_review_then_into_the_collection(self):
        number, _, item = self.queued()
        self.add_from_review(item, location='Shelf')
        self.assertEqual(self.state_of(number), 'scanned')       # not what it first became
        self.collection.take_from(self.scan, GAME, location='Box 2', station='phone-a')
        collection = rows(self.collection)
        self.assertEqual(self.state_of(number), 'resolved')
        self.assertEqual(self.delete(number).json, {'success': True, 'state': 'resolved'})
        self.assertEqual(rows(self.collection), collection)
        self.assertEqual([(card['quantity'], card['location']) for card in self.collection.get_all_cards(GAME)],
                         [(1, 'Box 2')])

    def test_added_from_review_then_removed_at_the_station(self):
        number, _, item = self.queued()
        self.add_from_review(item)
        self.scanned()                                           # an identical card, scanned directly
        self.assertEqual(self.delete(number).json['state'], 'removed')
        self.assertEqual(self.total(), 1)

    def test_a_review_item_older_than_the_applied_notes(self):
        """applied_captures forgets after KEEP_DAYS; an item may wait longer and is still found"""
        number, _, item = self.queued(remember=False)
        self.review.conn.execute('UPDATE applied_captures SET applied_at = ?',
                                 (pending_store.timestamp(pending_store.KEEP_DAYS + 5),))
        self.review.conn.commit()
        self.state.capture_outcomes[('phone-a', number)] = self.pending.outcome('phone-a', number)
        for manager in (self.review,):
            manager.conn.close()
        self.review = self.state.services.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')
        self.assertEqual(self.review.conn.execute('SELECT COUNT(*) FROM applied_captures').fetchone()[0], 0)
        self.assertEqual(self.state_of(number), 'review')
        self.assertEqual(self.delete(number).json['state'], 'removed')
        self.assertIsNone(self.review.get(item))

    def test_skipped_or_cleared_on_the_server_is_resolved(self):
        skipped, _, item = self.queued()
        self.review.remove(item)                                  # Skip on the page
        cleared, _ = self.scanned()
        self.scan.clear_inventory(GAME, station='phone-a')        # the camera's scanned cards cleared
        self.assertEqual([self.state_of(n) for n in (skipped, cleared)], ['resolved', 'resolved'])

    def test_split_merge_and_collection_with_locations(self):
        first, _ = self.scanned(location='Box 1')
        second, _ = self.scanned(location='Box 1')
        third, _ = self.scanned(location='Box 1')
        entry = self.scan.get_all_cards(GAME)[0]['id']
        self.scan.update_card(entry, location='Box 2', split_quantity=1, quiet=True)   # the newest: third
        self.assertEqual(self.delete(third).json['state'], 'removed')
        self.assertEqual(self.cards(), {('Test Card', 'regular', 'Box 1'): 2})
        self.collection.take_from(self.scan, GAME, location='Binder', station='phone-a')
        collection = rows(self.collection)
        for number in (first, second):
            self.assertEqual(self.state_of(number), 'resolved')
            self.assertEqual(self.delete(number).json['state'], 'resolved')
        self.assertEqual(rows(self.collection), collection)
        self.assertEqual([(card['quantity'], card['location']) for card in self.collection.get_all_cards(GAME)],
                         [(2, 'Binder')])

    # ---- the page's own Add (server/scan_events.py: handle_add_inventory), through Socket.IO

    PAGE_CARD = {'id': None, 'name': 'Test Card', 'set': 'Test Set', 'set_code': 'tst', 'number': '1', 'price': 1.0}

    def page(self, station=None):
        """A scanner page connected to the server: a station's, or the one that shows every station's review"""
        import app as launcher
        import games
        patcher = mock.patch('server.scan_events.start_price_update')   # no price look-up in the background
        patcher.start()
        self.addCleanup(patcher.stop)
        # The game being scanned, as at start-up - with no card database behind it (the card
        # on the page is put there by the test) - and put back as it was afterwards
        registered = dict(games._games)
        games.init(mock.Mock(), None)
        self.addCleanup(lambda: (games._games.clear(), games._games.update(registered)))
        page = launcher.socketio.test_client(launcher.app, query_string=f'station={station}' if station else '')
        self.addCleanup(page.disconnect)
        return page

    def open_review(self, page, desk):
        """The page opens the oldest review item and a card is picked for it (as a manual search does)"""
        page.emit('review_open')
        desk.current_card_info = dict(self.PAGE_CARD)
        return desk.current_review_id

    def errors(self, page):
        return [event['args'][0]['message'] for event in page.get_received() if event['name'] == 'error']

    def test_the_pages_add_hands_the_capture_on(self):
        number, uid, item = self.queued()
        page, desk = self.page('phone-a'), self.desks.desk_for('phone-a')
        self.assertEqual(self.open_review(page, desk), item)
        page.emit('add_to_inventory', {'items': [{'finish': None, 'quantity': 1}]})
        self.assertEqual((self.total('phone-a'), self.review.count(GAME)), (1, 0))
        self.assertEqual(self.state_of(number), 'scanned')
        self.assertEqual(self.delete(number).json['state'], 'removed')
        self.assertEqual(self.total(), 0)

    def test_reviewed_on_the_page_of_every_station_it_stays_the_stations_card(self):
        """The page without a station lists every station's review items: a card added there is
        still the capture's station's - counted as its, and within its phone's reach"""
        number, uid, item = self.queued()
        page, desk = self.page(), self.state.services.default_desk
        self.assertEqual(self.open_review(page, desk), item)
        page.emit('add_to_inventory', {'items': [{'finish': None, 'quantity': 1}]})
        self.assertEqual((self.total('phone-a'), self.total()), (1, 1))
        self.assertEqual(self.state_of(number), 'scanned')
        self.assertEqual(self.delete(number).json, {'success': True, 'state': 'removed'})
        self.assertEqual(self.total(), 0)

    def test_removed_at_the_station_first_the_pages_add_adds_nothing(self):
        number, uid, item = self.queued()
        page, desk = self.page('phone-a'), self.desks.desk_for('phone-a')
        self.open_review(page, desk)
        self.assertEqual(self.delete(number).json['state'], 'removed')
        page.get_received()
        page.emit('add_to_inventory', {'items': [{'finish': None, 'quantity': 1}]})   # the page had not caught up
        self.assertEqual(self.errors(page), ['No card selected'])
        self.assertEqual((self.total(), self.review.count(GAME)), (0, 0))

    def test_an_item_resolved_elsewhere_is_not_added_from_a_page_that_still_shows_it(self):
        number, uid, item = self.queued()
        page, desk = self.page('phone-a'), self.desks.desk_for('phone-a')
        self.open_review(page, desk)
        self.review.remove(item)                        # gone, and this page's desk was not told
        page.get_received()
        page.emit('add_to_inventory', {'items': [{'finish': None, 'quantity': 1}]})
        self.assertEqual(self.errors(page), ['This card is no longer in the review queue'])
        self.assertEqual(self.total(), 0)
        self.assertIsNone(desk.current_review_id)

    def test_a_removal_waits_for_an_add_that_is_under_way(self):
        """The Add has read which item is open and is writing the card: a DELETE arriving now
        must not find the queue item, remove it and say 'removed' while the card goes in. It
        waits for the add, then finds the card in the scanned cards and removes that."""
        import threading
        number, uid, item = self.queued()
        page, desk = self.page('phone-a'), self.desks.desk_for('phone-a')
        self.open_review(page, desk)
        answer, waited = {}, {}
        add_card = self.scan.add_card

        def add_while_a_delete_arrives(*args, **kwargs):
            # In the middle of the page's Add: the station's DELETE comes in on another thread
            removal = threading.Thread(target=lambda: answer.update(self.delete(number).json))
            removal.start()
            removal.join(0.4)
            waited['blocked'] = removal.is_alive()       # still waiting for the add to finish
            waited['thread'] = removal
            return add_card(*args, **kwargs)

        with mock.patch.object(self.scan, 'add_card', add_while_a_delete_arrives):
            page.emit('add_to_inventory', {'items': [{'finish': None, 'quantity': 1}]})
        waited['thread'].join(5)
        self.assertTrue(waited['blocked'])
        self.assertEqual(answer, {'success': True, 'state': 'removed'})
        self.assertEqual((self.total(), self.review.count(GAME)), (0, 0))   # in neither place: really removed

    def test_a_number_used_again_is_not_mistaken_for_the_old_capture(self):
        """A station forgotten on the server counts from 1 again: with the key it was given, a
        phone's old row cannot reach the new capture of the same number"""
        old_number, old_key = self.scanned()
        self.scan.clear_inventory(GAME, station='phone-a')        # forgetting clears its cards ...
        self.stations.remove('phone-a')                           # ... and its count
        self.state.capture_outcomes.clear()
        number, key = self.scanned()
        self.assertEqual(number, old_number)
        for answer in (self.client.get(self.url(number) + f'?sync_key={old_key}'),
                       self.client.delete(self.url(number) + f'?sync_key={old_key}')):
            self.assertEqual((answer.status_code, answer.json['success']), (409, False))
        self.assertEqual(self.total(), 1)
        self.assertEqual(self.client.delete(self.url(number) + f'?sync_key={key}').json['state'], 'removed')

    def test_a_settled_answer_carries_the_identity(self):
        """The upload's answers (first, repeated, asked later) come from the outcome on record"""
        from server.stations_api import capture_outcome_response
        number, uid = self.scanned()
        import app as launcher
        with launcher.app.app_context():
            answer = capture_outcome_response('phone-a', number).json
        self.assertEqual((answer['capture'], answer['sync_key'], answer['status']), (number, uid, 'added'))
        self.reopen()
        with launcher.app.app_context():
            answer = capture_outcome_response('phone-a', number).json
        self.assertEqual((answer['capture'], answer['sync_key'], answer['status']), (number, uid, 'added'))
        sender, settled_number, outcome = self.pending.settled(f'phone-a-{number}')   # a repeated upload
        self.assertEqual((settled_number, outcome['sync_key']), (number, uid))


if __name__ == '__main__':
    unittest.main()
