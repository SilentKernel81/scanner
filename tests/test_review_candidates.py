"""
Phase 4 of docs/plans/READING_REFACTOR_PLAN.md: the review screen shows what the capture's picture looks like. PrintingCheck.candidates (the nearest printings of a picture, as JSON-ready
values) and the review_open event that sends them beside the AI's read. Synthetic index and cards; Socket.IO's test client with stand-ins for the queue and the game.

    venv/bin/python -m unittest discover tests -p test_review_candidates.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

import app as launcher  # noqa: E402,F401 - the server's routes and events
from card_scanner.core import image_index, printing_check  # noqa: E402
from card_scanner.server.state import app, socketio  # noqa: E402
from test_printing_check import degraded, synthetic_card, write_pictures  # noqa: E402

scan_events = sys.modules['card_scanner.server.scan_events']


class Cards:
    """get_card_by_id for the synthetic printings p0..p5; p4 is one the card database lacks"""

    def get_card_by_id(self, printing_id):
        if printing_id == 'p4':
            return None
        return {'id': printing_id, 'name': f'Card {printing_id}', 'set': 'Test Set', 'set_code': 'tst', 'number': printing_id[1:], 'oracle_id': f'o-{printing_id}',
                'image_uri': f'https://cards.example/normal/{printing_id}.jpg', 'finishes': ['nonfoil', 'foil']}


class Candidates(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        pictures = self.root / 'pictures'
        pictures.mkdir()
        write_pictures(pictures, range(6))
        image_index.build([(f'p{n}', None) for n in range(6)], self.root / 'index', image_dir=pictures, fetch=False, log=lambda m: None)
        self.check = printing_check.PrintingCheck(Cards(), self.root / 'index', self.root / 'log.jsonl', 'off', 'off')
        self.capture = self.root / 'capture.jpg'
        cv2.imwrite(str(self.capture), cv2.cvtColor(degraded(synthetic_card(3)), cv2.COLOR_RGB2BGR))

    def test_the_nearest_printings_come_nearest_first_as_json_ready_values(self):
        found = self.check.candidates(self.capture)
        self.assertEqual(found[0]['id'], 'p3')                                    # the picture is p3
        self.assertLessEqual(len(found), 5)
        similarities = [c['similarity'] for c in found]
        self.assertEqual(similarities, sorted(similarities, reverse=True))
        self.assertEqual(set(found[0]), {'id', 'name', 'set', 'set_code', 'number', 'image_uri', 'finishes', 'similarity'})
        json.dumps(found)                                                          # plain Python values: no numpy float reaches the page
        self.assertIs(type(found[0]['similarity']), float)

    def test_a_printing_the_card_database_lacks_is_left_out(self):
        picture = self.root / 'p4.jpg'
        cv2.imwrite(str(picture), cv2.cvtColor(degraded(synthetic_card(4)), cv2.COLOR_RGB2BGR))
        self.assertNotIn('p4', [c['id'] for c in self.check.candidates(picture)])

    def test_nothing_without_an_index_or_a_picture(self):
        self.assertEqual(self.check.candidates(self.root / 'not-there.jpg'), [])
        self.assertEqual(self.check.candidates(None), [])
        no_index = printing_check.PrintingCheck(Cards(), self.root / 'none', self.root / 'log.jsonl', 'off', 'off')
        self.assertEqual(no_index.candidates(self.capture), [])
        (self.root / 'broken.jpg').write_bytes(b'not a picture')
        self.assertEqual(self.check.candidates(self.root / 'broken.jpg'), [])


class TheEvent(unittest.TestCase):
    """review_open sends the candidates beside what the AI read, and an item still opens when they cannot be found"""

    ITEM = {'id': 7, 'file': 'x.jpg', 'ai_name': 'Card', 'ai_number': '3', 'ai_set': 'tst', 'foil': 'unknown', 'card_id': None, 'match': None,
            'created_at': '2026-10-20 12:00:00', 'station': None}

    def setUp(self):
        self.game = mock.Mock()
        self.game.id = 'mtg'
        self.game.get_card.return_value = None
        self.review = mock.Mock()
        self.review.first.return_value = (dict(self.ITEM), 1)
        self.review.list_items.return_value = []
        self.review.image_path.return_value = Path('/nonexistent/x.jpg')
        self.check = mock.Mock()
        self.check.candidates.return_value = [{'id': 'p3', 'name': 'Card p3', 'similarity': 0.9}]
        desk = mock.MagicMock()
        desk.return_value.review_filter = None
        for name, value in (('review', self.review), ('games', mock.Mock(active=mock.Mock(return_value=self.game))), ('desk', desk),
                            ('printing_check', self.check), ('set_pending_capture', mock.Mock()), ('stations', mock.Mock(get=mock.Mock(return_value=None)))):
            patcher = mock.patch.object(scan_events, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = socketio.test_client(app, flask_test_client=app.test_client())
        self.client.get_received()
        self.addCleanup(self.client.disconnect)

    def opened(self):
        self.client.emit('review_open')
        return [e['args'][0] for e in self.client.get_received() if e['name'] == 'review_item'][0]

    def test_the_candidates_travel_with_the_item(self):
        item = self.opened()
        self.assertEqual(item['candidates'], [{'id': 'p3', 'name': 'Card p3', 'similarity': 0.9}])
        self.assertEqual((item['id'], item['ai']['foil']), (7, 'unknown'))               # the rest of the item is as it was
        self.check.candidates.assert_called_once_with(Path('/nonexistent/x.jpg'))

    def test_an_item_opens_without_candidates_when_they_cannot_be_found(self):
        self.check.candidates.side_effect = RuntimeError('index unreadable')
        item = self.opened()
        self.assertEqual(item['candidates'], [])
        self.assertEqual(item['id'], 7)

    def test_an_empty_queue_is_still_just_empty(self):
        self.review.first.return_value = (None, 0)
        self.client.emit('review_open')
        item = [e['args'][0] for e in self.client.get_received() if e['name'] == 'review_item'][0]
        self.assertEqual(item, {'id': None, 'total': 0})
        self.check.candidates.assert_not_called()

    def test_stepping_through_review_items_keeps_them_in_queue(self):
        second = dict(self.ITEM, id=8, reason='finish not read (printed in foil and non-foil)')
        self.review.step.return_value = (second, 2, 3)
        self.client.emit('review_open', {'step': 1})
        item = [e['args'][0] for e in self.client.get_received() if e['name'] == 'review_item'][0]
        self.assertEqual((item['id'], item['position'], item['total']), (8, 2, 3))
        self.assertEqual(item['reason'], 'finish not read (printed in foil and non-foil)')

    def test_review_queue_persists_reason_and_steps_without_deleting(self):
        from card_scanner.core.review import ReviewQueue
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        queue = ReviewQueue(Path(folder.name) / 'review.db')
        id1 = queue.add('mtg', None, 'First', reason='not found')
        id2 = queue.add('mtg', None, 'Second', reason='printing not confirmed')
        id3 = queue.add('mtg', None, 'Third', reason='finish not read')
        item, pos, total = queue.step('mtg', current_id=id1, delta=1)
        self.assertEqual((item['id'], item['reason'], pos, total), (id2, 'printing not confirmed', 2, 3))
        item, pos, total = queue.step('mtg', current_id=id3, delta=1)
        self.assertEqual((item['id'], item['reason'], pos, total), (id1, 'not found', 1, 3))
        item, pos, total = queue.step('mtg', current_id=id1, delta=-1)
        self.assertEqual((item['id'], item['reason'], pos, total), (id3, 'finish not read', 3, 3))
        item, pos, total = queue.open_id('mtg', item_id=id2)
        self.assertEqual((item['id'], pos, total), (id2, 2, 3))
        self.assertEqual(queue.next_after('mtg', current_id=id2), id3)
        self.assertIsNone(queue.next_after('mtg', current_id=id3))
        self.assertEqual([r['id'] for r in queue.list_items('mtg')], [id1, id2, id3])
        self.assertEqual(queue.count('mtg'), 3)

    def test_jumping_to_specific_review_item_by_id(self):
        second = dict(self.ITEM, id=9, reason='printing not confirmed')
        self.review.open_id.return_value = (second, 2, 4)
        self.review.list_items.return_value = [dict(self.ITEM, id=7), second]
        self.client.emit('review_open', {'id': 9})
        item = [e['args'][0] for e in self.client.get_received() if e['name'] == 'review_item'][0]
        self.assertEqual((item['id'], item['position'], item['total']), (9, 2, 4))
        self.assertEqual([q['id'] for q in item['queue']], [7, 9])

    def test_malformed_review_open_parameters_return_error(self):
        self.client.emit('review_open', {'id': 'not-an-int'})
        errors = [e['args'][0] for e in self.client.get_received() if e['name'] == 'error']
        self.assertEqual(len(errors), 1)
        self.client.emit('review_open', {'step': 'bad'})
        errors = [e['args'][0] for e in self.client.get_received() if e['name'] == 'error']
        self.assertEqual(len(errors), 1)

    def test_undo_review_add_restores_original_item_and_capture_key_and_multi_finish(self):
        from card_scanner.core.inventory import InventoryManager
        from card_scanner.core.review import ReviewQueue
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        photo = root / 'orig.jpg'
        cv2.imwrite(str(photo), cv2.cvtColor(synthetic_card(1), cv2.COLOR_RGB2BGR))

        inv = InventoryManager(root / 'scan.db')
        queue = ReviewQueue(root / 'review.db')
        cap_dir, full_dir = root / 'caps', root / 'caps_full'
        rev_dir = root / 'rev_imgs'
        with mock.patch('card_scanner.core.inventory.CAPTURES_DIR', cap_dir), \
                mock.patch('card_scanner.core.inventory.FULL_CAPTURES_DIR', full_dir), \
                mock.patch('card_scanner.core.review.REVIEW_DIR', rev_dir):
            orig_id = queue.add('mtg', photo, 'Test Card', '12', 'tst', 'unknown',
                                station='phone-1', capture_key='cap-uid-1', reason='finish not read')
            orig = queue.get(orig_id)
            self.assertEqual(queue.by_capture('cap-uid-1')['id'], orig_id)

            # Simulate adding 1 regular + 1 foil from the review item (with capture_key on the first finish)
            fields = {'card_id': 'p1', 'name': 'Test Card', 'set_name': 'Test Set', 'set_code': 'tst', 'number': '12', 'price': 1.0}
            r1 = inv.add_card(fields, 'mtg', 'regular', 'Near Mint', 1, capture=queue.image_path(orig),
                              source='phone-1', capture_key='cap-uid-1')
            e1 = (*inv.last_added['phone-1'], 'phone-1')
            r2 = inv.add_card(fields, 'mtg', 'foil', 'Near Mint', 1, capture=None,
                              source='phone-1', capture_key=None)
            e2 = (*inv.last_added['phone-1'], 'phone-1')
            queue.remove(orig_id)

            self.assertEqual(inv.captured_copy('cap-uid-1', 'phone-1')[0], 'scanned')
            self.assertIsNone(queue.by_capture('cap-uid-1'))

            # Undo: restore original review row (same id, reason, capture_key) and revert both finishes
            img_path = inv.capture_file_for(e1[2])
            self.assertIsNotNone(img_path)
            restored_id = queue.restore(orig, img_path, transfer='undo')
            inv.revert_added_entries([e1, e2], capture_key='cap-uid-1')
            queue.confirm_transfer(restored_id)

            self.assertEqual(restored_id, orig_id)
            restored = queue.by_capture('cap-uid-1')
            self.assertIsNotNone(restored)
            self.assertEqual((restored['id'], restored['reason'], restored['ai_name'], restored['station']),
                             (orig_id, 'finish not read', 'Test Card', 'phone-1'))
            self.assertEqual(inv.get_all_cards('mtg'), [])
            self.assertEqual(inv.captured_copy('cap-uid-1', 'phone-1'), (None, None))  # NOT unfollowed!

            # Re-adding after undo still accepts the original capture_key cleanly
            r3 = inv.add_card(fields, 'mtg', 'foil', 'Near Mint', 1, capture=queue.image_path(restored),
                              source='phone-1', capture_key='cap-uid-1')
            self.assertIsNotNone(r3)
            self.assertEqual(inv.captured_copy('cap-uid-1', 'phone-1')[0], 'scanned')
            # Limit query returns only the requested number of newest rows
            inv.add_card({**fields, 'card_id': 'p2', 'name': 'Second Card', 'number': '13'}, 'mtg', 'regular', 'Near Mint', 1, source='phone-1')
            self.assertEqual(len(inv.get_all_cards('mtg', limit=1)), 1)
            self.assertEqual(len(inv.get_all_cards('mtg', station='phone-1', limit=1)), 1)


if __name__ == '__main__':
    unittest.main()
