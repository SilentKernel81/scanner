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


if __name__ == '__main__':
    unittest.main()
