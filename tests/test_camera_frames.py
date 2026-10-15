"""
The camera frame behind a capture, kept as training data for a better detector later
(core/frames.py, CardScanner._frame_snapshot, station_client.queue_upload, stations_api.keep_camera_frame).

All on temporary folders - never the real data/:

  - FrameStore: a pair per capture, a repeat adds nothing, what is not a frame is refused, the
    station's id cannot name a path, the oldest go over the cap;
  - Snapshot: the outline is written in the pixels of the frame that is stored - with a
    camera whose detection runs at half size the corners are scaled, and a rotated camera's frame is
    stored rotated;
  - Upload: the route keeps the frame once, and never fails the capture over it.

    venv/bin/python -m unittest discover tests -p test_camera_frames.py
"""
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_ownership: the temporary data

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.core import frames  # noqa: E402


def jpeg(width=64, height=48, value=120):
    ok, buffer = cv2.imencode('.jpg', np.full((height, width, 3), value, np.uint8))
    assert ok
    return buffer.tobytes()


class FrameStore(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(frames, 'FRAMES_DIR', Path(self.folder.name) / 'frames')
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.folder.cleanup()

    def test_a_pair_per_capture_and_a_repeat_adds_nothing(self):
        data = jpeg()
        self.assertTrue(frames.save(data, {'corners': [[1, 2]]}, 'pi4', 7, 'abc123', 'pi4_7_1.jpg'))
        self.assertFalse(frames.save(jpeg(value=10), {'corners': None}, 'pi4', 7, 'abc123', 'pi4_7_1.jpg'))
        self.assertEqual((frames.FRAMES_DIR / 'abc123.jpg').read_bytes(), data)  # the first one stays
        note = json.loads((frames.FRAMES_DIR / 'abc123.json').read_text())
        self.assertEqual((note['station'], note['number'], note['image'], note['corners']),
                         ('pi4', 7, 'pi4_7_1.jpg', [[1, 2]]))
        self.assertEqual(sorted(p.name for p in frames.FRAMES_DIR.iterdir()), ['abc123.jpg', 'abc123.json'])

    def test_what_is_not_a_frame_is_refused(self):
        self.assertFalse(frames.save(b'', {}, 's', 1, 'a'))
        self.assertFalse(frames.save(b'GIF89a not a jpeg', {}, 's', 1, 'a'))
        with mock.patch.object(frames, 'MAX_FRAME_BYTES', 10):
            self.assertFalse(frames.save(jpeg(), {}, 's', 1, 'a'))
        self.assertFalse(frames.FRAMES_DIR.exists())

    def test_the_stations_id_cannot_name_a_path(self):
        self.assertTrue(frames.save(jpeg(), {}, 'pi4', 3, '../../etc/passwd', 'x.jpg'))
        names = [p.name for p in frames.FRAMES_DIR.iterdir()]
        self.assertEqual(len(names), 2)
        self.assertTrue(all(name.startswith('pi4_3_') for name in names), names)
        self.assertEqual(sorted(p for p in Path(self.folder.name).rglob('*') if p.is_file()),
                         sorted(frames.FRAMES_DIR.iterdir()))  # nothing written outside

    def test_the_oldest_go_when_the_folder_is_over_its_cap(self):
        size = len(jpeg())
        for n in range(6):
            frames.save(jpeg(), {}, 's', n, f'id{n}')
        with mock.patch.object(frames, 'MAX_BYTES', size * 3 + 6 * 60):
            with frames._lock:
                frames._prune()
        left = sorted(p.stem for p in frames.FRAMES_DIR.glob('*.jpg'))
        self.assertLess(len(left), 6)
        self.assertIn('id5', left)  # the newest stays


class Snapshot(unittest.TestCase):
    """CardScanner._frame_snapshot / get_detected_card, without a camera"""

    def scanner(self, half_size, rotation=0):
        from card_scanner.station.scanner import CardScanner
        scanner = object.__new__(CardScanner)
        scanner.frame_lock = threading.Lock()
        scanner.half_size_decode = half_size
        scanner.rotation, scanner.camera_rotation = rotation, 0
        scanner.fixed_area_enabled = False
        scanner._fixed_area_active = lambda: False
        return scanner

    def test_corners_are_in_the_pixels_of_the_stored_frame(self):
        scanner = self.scanner(half_size=True)
        full = np.full((360, 640, 3), 90, np.uint8)
        ok, buffer = cv2.imencode('.jpg', full)
        raw = np.frombuffer(buffer.tobytes(), np.uint8)
        half = np.full((180, 320, 3), 90, np.uint8)  # detection ran on this
        corners = np.array([[50, 20], [250, 20], [250, 160], [50, 160]], np.float32)  # in half-size pixels
        scanner.detected_card = (half, (50, 20, 250, 160), corners, raw)
        scanner.detected_card_name = ''
        snapshot = {}
        card, _, _ = scanner.get_detected_card(snapshot)
        self.assertIsNotNone(card)
        self.assertEqual((snapshot['width'], snapshot['height']), (640, 360))
        self.assertEqual(snapshot['corners'][0], [100.0, 40.0])      # x2, y2
        self.assertEqual(snapshot['corners'][2], [500.0, 320.0])
        self.assertEqual(snapshot['jpeg'], raw.tobytes())            # the camera's own bytes, not encoded again

    def test_a_rotated_cameras_frame_is_stored_rotated(self):
        scanner = self.scanner(half_size=True, rotation=90)
        full = np.zeros((360, 640, 3), np.uint8)
        ok, buffer = cv2.imencode('.jpg', full)
        raw = np.frombuffer(buffer.tobytes(), np.uint8)
        shot = scanner._frame_snapshot(np.zeros((640, 360, 3), np.uint8), raw, None)  # a rotated frame is 640 tall
        self.assertNotEqual(shot['jpeg'], raw.tobytes())  # encoded from the rotated frame
        stored = cv2.imdecode(np.frombuffer(shot['jpeg'], np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(stored.shape[:2], (640, 360))
        self.assertEqual((shot['width'], shot['height']), (360, 640))
        self.assertIsNone(shot['corners'])


class Upload(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(frames, 'FRAMES_DIR', Path(self.folder.name) / 'frames')
        self.patch.start()
        from card_scanner.server import state
        self.app = state.app

    def tearDown(self):
        self.patch.stop()
        self.folder.cleanup()

    def send(self, **form):
        from card_scanner.server import stations_api
        files = {'frame': (io.BytesIO(jpeg()), 'f.jpg')} if form.pop('with_frame', True) else {}
        with self.app.test_request_context('/', method='POST', data={**form, **files},
                                           content_type='multipart/form-data'):
            stations_api.keep_camera_frame('pi4', 12, form.get('capture_id'), 'pi4_12_1.jpg')

    def test_kept_once_with_the_stations_note_and_the_servers(self):
        self.send(capture_id='c0ffee', frame_meta=json.dumps({'new_card': {'kind': 'card_gone'}}))
        self.send(capture_id='c0ffee', frame_meta='{}')
        note = json.loads((frames.FRAMES_DIR / 'c0ffee.json').read_text())
        self.assertEqual(note['new_card'], {'kind': 'card_gone'})
        self.assertEqual((note['station'], note['number'], note['image']), ('pi4', 12, 'pi4_12_1.jpg'))

    def test_no_frame_and_a_broken_note_change_nothing(self):
        self.send(capture_id='aa', with_frame=False)
        self.assertFalse(frames.FRAMES_DIR.exists())
        self.send(capture_id='bb', frame_meta='{not json')  # kept without a note from the station
        self.assertTrue((frames.FRAMES_DIR / 'bb.jpg').exists())
        with mock.patch.object(frames, 'save', side_effect=OSError('disk full')):
            self.send(capture_id='cc', frame_meta='{}')  # must not raise


class LargeCopies(unittest.TestCase):
    """core/inventory.py: the large copy of a capture beside its thumbnail"""

    def setUp(self):
        from test_ownership import TemporaryData
        self.data = TemporaryData('setUp')
        self.data.setUp()
        # a card picture as tall as a computer station's (the thumbnail is 400 px, the copy at most 1200)
        cv2.imwrite(str(self.data.photo), np.full((1500, 1000, 3), 128, np.uint8))
        self.inv = sys.modules['card_scanner.core.inventory']

    def tearDown(self):
        self.data.tearDown()

    def height(self, path):
        return cv2.imread(str(path)).shape[0]

    def test_a_thumbnail_and_a_large_copy_that_go_together(self):
        from test_ownership import CARD, GAME
        row_id = self.data.scan.add_card(CARD, GAME, 'regular', quiet=True, capture=self.data.photo)
        name = self.data.scan.conn.execute('SELECT file FROM inventory_captures').fetchone()[0]
        self.assertEqual(self.height(self.inv.CAPTURES_DIR / name), 400)
        self.assertEqual(self.height(self.inv.FULL_CAPTURES_DIR / name), 1200)
        self.data.scan.delete_card(row_id, quiet=True)
        self.assertFalse((self.inv.CAPTURES_DIR / name).exists())
        self.assertFalse((self.inv.FULL_CAPTURES_DIR / name).exists())  # not left behind

    def test_a_small_picture_is_not_enlarged(self):
        from test_ownership import CARD, GAME
        cv2.imwrite(str(self.data.photo), np.full((300, 200, 3), 128, np.uint8))
        self.data.scan.add_card(CARD, GAME, 'regular', quiet=True, capture=self.data.photo)
        name = self.data.scan.conn.execute('SELECT file FROM inventory_captures').fetchone()[0]
        self.assertEqual(self.height(self.inv.FULL_CAPTURES_DIR / name), 300)

    def test_a_large_copy_that_cannot_be_written_does_not_cost_the_capture(self):
        from test_ownership import CARD, GAME
        with mock.patch.object(self.inv, 'FULL_CAPTURES_DIR', Path(self.data.photo)):  # a file: no folder
            self.data.scan.add_card(CARD, GAME, 'regular', quiet=True, capture=self.data.photo)
        self.assertEqual(self.data.scan.conn.execute('SELECT COUNT(*) FROM inventory_captures').fetchone()[0], 1)


class LabelTool(unittest.TestCase):
    """scripts/label_frames.py: what it lists and writes (the page itself is not tested)"""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'label_frames', Path(__file__).resolve().parent.parent / 'scripts' / 'label_frames.py')
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        (self.path / 'aa11.jpg').write_bytes(jpeg())
        (self.path / 'aa11.json').write_text(json.dumps({
            'station': 's', 'number': 3, 'frame_width': 640, 'frame_height': 360,
            'corners': [[10, 10], [100, 10], [100, 150], [10, 150]]}))
        self.frames = self.tool.Frames(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_corners_are_put_in_the_order_top_left_clockwise(self):
        shuffled = [[100, 150], [10, 10], [10, 150], [100, 10]]
        self.assertEqual(self.tool.clockwise(shuffled), [[10, 10], [100, 10], [100, 150], [10, 150]])
        tilted = [[20, 5], [110, 20], [95, 160], [5, 140]]  # a card turned a little: TL, TR, BR, BL
        self.assertEqual(self.tool.clockwise(tilted[::-1]), tilted)

    def test_a_label_is_written_beside_the_pair_and_listed(self):
        self.assertEqual(self.frames.listing()[0]['status'], None)
        label = self.frames.save('aa11', {'status': 'labelled',
                                         'corners': [[12, 12.04], [98, 11], [97, 149], [11, 151]]})
        self.assertEqual(label['corners'][0], [12.0, 12.0])
        self.assertEqual(label['detector_corners'], [[10, 10], [100, 10], [100, 150], [10, 150]])
        self.assertEqual(self.frames.listing()[0]['status'], 'labelled')
        self.assertEqual(sorted(p.name for p in self.path.iterdir()), ['aa11.jpg', 'aa11.json', 'aa11.label.json'])
        self.assertEqual(self.frames.stems(), ['aa11'])  # a label is not a frame of its own

    def test_what_is_not_a_label_is_refused(self):
        for body in ({'status': 'bogus', 'corners': []}, {'status': 'labelled', 'corners': [[1, 2]]},
                     {'status': 'labelled', 'corners': [[0, 0], [1, 0], [1, 1], [9999, 9999]]},
                     {'status': 'labelled', 'corners': 'x'}):
            with self.assertRaises(ValueError):
                self.frames.save('aa11', body)
        with self.assertRaises(ValueError):
            self.frames.save('nope', {'status': 'no_card'})
        self.assertEqual(self.frames.save('aa11', {'status': 'no_card'})['corners'], None)


class CaptureWash(unittest.TestCase):
    """The green wash over the whole card at a capture (CardScanner._wash_card)"""

    def setUp(self):
        from card_scanner.station.scanner import CardScanner
        self.wash = CardScanner._wash_card
        self.frame = np.full((200, 300, 3), 100, np.uint8)
        self.corners = np.array([[50, 40], [150, 40], [150, 180], [50, 180]], np.float32)

    def test_the_card_goes_green_see_through_and_the_rest_stays(self):
        frame = self.frame.copy()
        self.wash(frame, self.corners, (50, 40, 150, 180), 0.5)
        inside = frame[110, 100].astype(int)          # the middle of the card: half way to green
        for got, want in zip(inside, (50, 177.5, 50)):                    # half way from the card's grey to green
            self.assertAlmostEqual(got, want, delta=1)
        self.assertTrue(np.array_equal(frame[10, 10], self.frame[10, 10]))      # outside: untouched
        self.assertTrue(np.array_equal(frame[110, 200], self.frame[110, 200]))  # beside the card
        self.assertGreater(inside[1], 100)            # it is a wash, not a fill: the card shows through

    def test_a_tilted_card_only_inside_its_outline(self):
        frame = self.frame.copy()
        tilted = np.array([[100, 20], [180, 60], [150, 180], [70, 140]], np.float32)
        x1, y1, x2, y2 = 70, 20, 180, 180
        self.wash(frame, tilted, (x1, y1, x2, y2), 1.0)
        self.assertEqual(tuple(frame[100, 125]), (0, 255, 0))            # inside the outline
        self.assertTrue(np.array_equal(frame[30, 75], self.frame[30, 75]))  # in the box, outside the card

    def test_a_box_without_corners_and_nothing_to_do(self):
        frame = self.frame.copy()
        self.wash(frame, None, (20, 20, 80, 80), 1.0)                     # the fixed area is a box
        self.assertEqual(tuple(frame[50, 50]), (0, 255, 0))
        before = frame.copy()
        self.wash(frame, None, (20, 20, 80, 80), 0.0)                     # faded out: nothing changes
        self.wash(frame, self.corners, (400, 400, 500, 500), 0.5)         # a box outside the picture
        self.assertTrue(np.array_equal(frame, before))


class CaptureCopySize(unittest.TestCase):
    """The size of the large copy is the person's choice (inventory.set_full_capture_size, /api/capture_copy)"""

    def setUp(self):
        from test_ownership import TemporaryData
        self.data = TemporaryData('setUp')
        self.data.setUp()
        cv2.imwrite(str(self.data.photo), np.full((1500, 1000, 3), 128, np.uint8))
        self.inv = sys.modules['card_scanner.core.inventory']
        self.saved = self.inv.FULL_CAPTURE_HEIGHT

    def tearDown(self):
        self.inv.FULL_CAPTURE_HEIGHT = self.saved
        self.data.tearDown()

    def kept(self):
        from test_ownership import CARD, GAME
        self.data.scan.add_card(CARD, GAME, 'regular', quiet=True, capture=self.data.photo)
        return self.data.scan.conn.execute('SELECT file FROM inventory_captures ORDER BY id DESC').fetchone()[0]

    def test_each_size_gives_its_height(self):
        for name, height in (('small', 600), ('medium', 900), ('large', 1200)):
            self.assertEqual(self.inv.set_full_capture_size(name), name)
            file = self.kept()
            self.assertEqual(cv2.imread(str(self.inv.FULL_CAPTURES_DIR / file)).shape[0], height)
            self.assertEqual(cv2.imread(str(self.inv.CAPTURES_DIR / file)).shape[0], 400)  # the thumbnail is the same

    def test_off_keeps_the_thumbnail_only(self):
        self.inv.set_full_capture_size('off')
        file = self.kept()
        self.assertTrue((self.inv.CAPTURES_DIR / file).is_file())
        self.assertFalse((self.inv.FULL_CAPTURES_DIR / file).exists())
        self.assertEqual(self.inv.full_capture_usage(), (0, 0))

    def test_an_unknown_size_is_the_default_and_usage_counts_what_is_kept(self):
        self.assertEqual(self.inv.set_full_capture_size('enormous'), 'large')
        self.assertEqual(self.inv.FULL_CAPTURE_HEIGHT, 1200)
        self.kept()
        self.kept()
        count, size = self.inv.full_capture_usage()
        self.assertEqual(count, 2)
        self.assertGreater(size, 0)

    def test_the_route_chooses_and_refuses(self):
        from card_scanner.server import settings_events
        from card_scanner.server.state import app

        class Store(dict):
            def set(self, key, value):
                self[key] = value
        store = Store()
        with mock.patch.object(settings_events, 'app_settings', store):
            with app.test_request_context('/api/capture_copy', method='POST', json={'size': 'small'}):
                answer = settings_events.set_capture_copy()
            self.assertEqual(answer.get_json()['size'], 'small')
            self.assertEqual(store['capture_copy_size'], 'small')
            self.assertEqual(self.inv.FULL_CAPTURE_HEIGHT, 600)
            with app.test_request_context('/api/capture_copy', method='POST', json={'size': 'huge'}):
                refused, status = settings_events.set_capture_copy()
            self.assertEqual(status, 400)
            self.assertIn('error', refused.get_json())          # the key the pages' api() shows
            self.assertEqual(self.inv.FULL_CAPTURE_HEIGHT, 600)  # unchanged
            with app.test_request_context('/api/capture_copy', method='GET'):
                info = settings_events.set_capture_copy().get_json()
            self.assertEqual((info['success'], info['size']), (True, 'small'))
            self.assertEqual(info['sizes']['large'], 1200)


class ScoreTool(unittest.TestCase):
    """scripts/score_detector.py: the overlap measure and the replay over labelled frames"""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'score_detector', Path(__file__).resolve().parent.parent / 'scripts' / 'score_detector.py')
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.label = [[20, 20], [60, 20], [60, 80], [20, 80]]
        (self.path / 'f1.jpg').write_bytes(jpeg(100, 100))
        (self.path / 'f1.json').write_text(json.dumps({
            'station': 's', 'number': 1, 'corners': [[10, 10], [60, 10], [60, 80], [10, 80]]}))
        (self.path / 'f1.label.json').write_text(json.dumps({'status': 'labelled', 'corners': self.label}))
        # a frame that was labelled "no card" is not scored
        (self.path / 'f2.jpg').write_bytes(jpeg(100, 100))
        (self.path / 'f2.json').write_text(json.dumps({'station': 's', 'number': 2, 'corners': None}))
        (self.path / 'f2.label.json').write_text(json.dumps({'status': 'no_card', 'corners': None}))

    def tearDown(self):
        self.folder.cleanup()

    def test_iou(self):
        box = [[0, 0], [10, 0], [10, 10], [0, 10]]
        shape = (20, 20)
        self.assertEqual(self.tool.iou(box, box, shape), 1.0)
        half = [[5, 0], [15, 0], [15, 10], [5, 10]]
        self.assertAlmostEqual(self.tool.iou(box, half, shape), 50 / 150, places=1)  # 50 shared of 150 covered
        self.assertEqual(self.tool.iou(None, box, shape), 0.0)

    def test_a_replay_is_scored_against_the_label_and_a_miss_is_zero(self):
        class Perfect:
            def detect(self_, frame, previous=None):
                return (0, 0, 1, 1), 'Card', 1.0, np.array(self.label, np.float32)

        class Nothing:
            def detect(self_, frame, previous=None):
                return None, '', 0, None

        rows = self.tool.score(self.path, detector=Perfect())
        self.assertEqual([r['stem'] for r in rows], ['f1'])           # the "no card" frame is left out
        self.assertAlmostEqual(rows[0]['replay'], 1.0)
        self.assertLess(rows[0]['stored'], 0.95)                      # the note's outline was too wide
        miss = self.tool.score(self.path, detector=Nothing())[0]
        self.assertEqual((miss['replay'], miss['found']), (0.0, False))


if __name__ == '__main__':
    unittest.main()
