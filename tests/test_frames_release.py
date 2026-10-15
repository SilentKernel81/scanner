"""
The camera frames kept for training (core/frames.py) go with the scanned cards they were taken
for: when those are cleared (a camera's or all) or added to the collection - unless "Keep camera
frames" is on. A frame without a uid (taken earlier) stays. Temporary data only.

    venv/bin/python -m unittest discover tests -p test_frames_release.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_ownership: the temporary data

from card_scanner.core import frames  # noqa: E402
from test_ownership import CARD, GAME, TemporaryData  # noqa: E402

JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64


class FrameFiles(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patch = mock.patch.object(frames, 'FRAMES_DIR', Path(self.folder.name) / 'frames')
        patch.start()
        self.addCleanup(patch.stop)

    def names(self):
        return sorted(p.name for p in frames.FRAMES_DIR.iterdir())

    def test_a_frame_is_noted_with_its_uid_and_let_go_with_it(self):
        frames.save(JPEG, {}, 'pi', 1, 'cap1', 'a.jpg')
        frames.save(JPEG, {}, 'pi', 2, 'cap2', 'b.jpg')
        self.assertTrue(frames.attach('cap1', 'uid-1'))
        self.assertTrue(frames.attach('cap2', 'uid-2'))
        self.assertEqual(frames.forget({'uid-1'}), 1)
        self.assertEqual(self.names(), ['cap2.jpg', 'cap2.json'])

    def test_a_frame_without_a_uid_stays_and_nothing_fails_without_a_frame(self):
        frames.save(JPEG, {}, 'pi', 1, 'old', 'a.jpg')          # never settled: no uid
        self.assertEqual(frames.forget({'uid-x', None}), 0)
        self.assertFalse(frames.attach('no-such', 'uid'))       # a capture that had no frame
        self.assertFalse(frames.attach('../escape', 'uid'))     # the id names a file: checked
        self.assertEqual(self.names(), ['old.jpg', 'old.json'])

    def test_usage_counts_frames_and_bytes(self):
        frames.save(JPEG, {}, 'pi', 1, 'cap1', 'a.jpg')
        count, size = frames.usage()
        self.assertEqual(count, 1)
        self.assertGreater(size, len(JPEG))


class ReleaseWithCards(TemporaryData):
    """The scanned cards' clear and move routes release the frames"""

    def setUp(self):
        super().setUp()
        import app as launcher  # the server's routes (nothing is started)
        from card_scanner.server import desks as desks_module
        from card_scanner.server import state
        self.state, self.desks = state, desks_module
        self.folder = tempfile.TemporaryDirectory()
        patch = mock.patch.object(frames, 'FRAMES_DIR', Path(self.folder.name) / 'frames')
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.folder.cleanup)

        class Settings:
            values = {}
            def get(self, key, default=None): return self.values.get(key, default)
            def set(self, key, value): self.values[key] = value
        self.settings = Settings()
        services = state.services
        self.saved_services = {name: getattr(services, name) for name in (
            'scan_inventory', 'inventory', 'app_settings', 'camera_hub', 'default_desk', 'stations', 'review')}
        from card_scanner.core.review import ReviewQueue
        from card_scanner.core import review as review_store
        self.saved_review_dir = review_store.REVIEW_DIR
        review_store.REVIEW_DIR = self.tmp / 'review'
        self.review_store = review_store
        self.review = ReviewQueue(db_file=self.tmp / 'collection.db')
        services.scan_inventory, services.inventory, services.app_settings = self.scan, self.collection, self.settings
        services.review = self.review
        from card_scanner.core.stations import Stations
        self.stations = Stations(file=self.tmp / 'stations.json')
        for station in ('pi', 'other'):
            self.stations.capture(station, station)   # a camera filter names a station that is known
        services.stations = self.stations
        services.camera_hub = mock.Mock()
        services.default_desk = desks_module.Desk()
        desks_module.desks.clear()
        from types import SimpleNamespace
        from card_scanner.server import inventory_api
        patch_game = mock.patch.object(inventory_api.games, 'active',
                                       return_value=SimpleNamespace(id=GAME, deck_formats=[], label='Test'))
        patch_game.start()
        self.addCleanup(patch_game.stop)
        self.client = launcher.app.test_client()
        for number, station, key in ((1, 'pi', 'uid-1'), (2, 'pi', 'uid-2'), (3, 'other', 'uid-3')):
            frames.save(JPEG, {}, station, number, f'cap{number}', f'{number}.jpg')
            self.scan.add_card(dict(CARD, number=str(number), name=f'Card {number}'), GAME, 'regular', source=station,
                               capture=self.photo, capture_key=key, quiet=True)
            frames.attach(f'cap{number}', key)

    def tearDown(self):
        for name, value in self.saved_services.items():
            setattr(self.state.services, name, value)
        self.desks.desks.clear()
        self.review.conn.close()
        self.review_store.REVIEW_DIR = self.saved_review_dir
        super().tearDown()

    def left(self):
        return sorted(p.stem for p in frames.FRAMES_DIR.glob('*.jpg'))

    def test_the_capture_keys_of_a_camera(self):
        self.assertEqual(self.scan.capture_keys(GAME, 'pi'), {'uid-1', 'uid-2'})
        self.assertEqual(self.scan.capture_keys(GAME), {'uid-1', 'uid-2', 'uid-3'})

    def test_clearing_a_camera_releases_its_frames_only(self):
        answer = self.client.post('/api/clear_inventory?area=scan&camera=pi')
        self.assertEqual(answer.status_code, 200, answer.get_json())
        self.assertEqual(self.left(), ['cap3'])

    def test_adding_to_the_collection_releases_the_frames(self):
        answer = self.client.post('/api/scan_inventory/to_collection', json={})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.left(), [])
        self.assertEqual(self.total(), 0)

    def test_clearing_everything_releases_all(self):
        answer = self.client.post('/api/clear_inventory?area=scan')
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.left(), [])

    def test_the_switch_keeps_the_frames(self):
        self.settings.set('keep_frames', True)
        self.client.post('/api/scan_inventory/to_collection', json={})
        self.assertEqual(self.left(), ['cap1', 'cap2', 'cap3'])

    def test_the_setting_is_asked_and_changed_through_the_route(self):
        self.assertFalse(self.client.get('/api/capture_copy').get_json()['keep_frames'])
        answer = self.client.post('/api/capture_copy', json={'keep_frames': True}).get_json()
        self.assertTrue(answer['success'] and answer['keep_frames'])
        self.assertEqual(answer['frames'], 3)


if __name__ == '__main__':
    unittest.main()
