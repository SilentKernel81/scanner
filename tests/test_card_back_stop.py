"""
A card back stops the camera (server/captures.py: stop_at_card_back, from stations_api.station_capture; docs/plans/CARD_BACK_STOP_PLAN.md), through Flask's test client
with the server's shared objects replaced by temporary ones and the reading a stand-in that FAILS the test if it is asked: a back uploaded in mode 'auto' and in mode
'review' stops the camera, is answered 'card_back' and is never read; a repeated upload of it gets the same answer and stops nothing twice; a 'review' back releases the
station's hold; the setting off, a front, or a sender that does not understand the answer (an older phone app) go on exactly as before; the sync route refuses (409) a back;
and a failing check is "not a back". The "back" is a synthetic picture whose fingerprint replaces the real one (core/card_back.py's tests say why).

    venv/bin/python -m unittest discover tests -p test_card_back_stop.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_ownership: the temporary data; test_printing_check: the synthetic pictures

import cv2  # noqa: E402

from card_scanner.config import Config  # noqa: E402
from card_scanner.core import card_back, image_index  # noqa: E402
from card_scanner.core import pending as pending_store  # noqa: E402
from card_scanner.core import review as review_store  # noqa: E402
from card_scanner.core.stations import Stations  # noqa: E402
from test_ownership import TemporaryData  # noqa: E402
from test_printing_check import degraded, synthetic_card  # noqa: E402

BACK = synthetic_card(41)


class Settings(dict):
    """A station's settings as StationSettings offers them: get / set"""

    def set(self, key, value):
        self[key] = value


class Camera:
    """What the server's RemoteScanner offers here: the state the server sets, and the station's settings"""

    def __init__(self):
        self.auto_capture_enabled = True
        self.card_under_review = True        # mode 'review': the station holds its auto-capture until Add / Skip
        self.settings = Settings()

    def __bool__(self):
        return True


class Reading:
    """identification, which must not be asked about a back"""

    def __init__(self, test):
        self.test = test

    def _new_record(self):
        return None             # no reading log here

    def identify(self, *args, **kwargs):
        self.test.fail('a card back was read')

    def submit(self, *args, **kwargs):
        self.test.fail('a card back was queued to be read')


class CardBackStop(TemporaryData):
    def setUp(self):
        super().setUp()
        import app as launcher  # the server's routes (nothing is started, no database opened)
        from card_scanner.server import captures, desks as desks_module, state, stations_api
        self.state, self.desks, self.captures, self.api = state, desks_module, captures, stations_api
        self.saved_review_dir = review_store.REVIEW_DIR
        review_store.REVIEW_DIR = self.tmp / 'review'
        self.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.stations = Stations(file=self.tmp / 'stations.json')
        self.camera = Camera()
        services = state.services
        self.saved_services = {name: getattr(services, name) for name in (
            'scan_inventory', 'inventory', 'review', 'pending', 'stations', 'camera_hub', 'default_desk', 'identification')}
        services.scan_inventory, services.inventory = self.scan, self.collection
        services.review, services.pending, services.stations = self.review, self.pending, self.stations
        services.camera_hub = mock.Mock(scanner=lambda station_id: self.camera)
        services.default_desk = desks_module.Desk()
        services.identification = Reading(self)
        desks_module.desks.clear()
        state.capture_outcomes.clear()
        state.seen_captures.clear()
        self.saved_token, self.saved_images = Config.STATION_TOKEN, Config.IMAGES_DIR
        Config.STATION_TOKEN = ''
        Config.IMAGES_DIR = self.tmp / 'scanned_cards'
        Config.IMAGES_DIR.mkdir()
        # what the pages were told, in order: (event, data)
        self.events = []
        record = lambda event, data: self.events.append((event, data))
        for patcher in (mock.patch.object(card_back, 'CARD_BACK_FINGERPRINT', image_index.hash_card(BACK)),
                        mock.patch.object(captures, 'emit_desk', record), mock.patch.object(stations_api, 'emit_desk', record),
                        mock.patch.object(stations_api, 'submit_capture')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = launcher.app.test_client()

    def tearDown(self):
        Config.STATION_TOKEN, Config.IMAGES_DIR = self.saved_token, self.saved_images
        for name, value in self.saved_services.items():
            setattr(self.state.services, name, value)
        self.desks.desks.clear()
        self.state.capture_outcomes.clear()
        self.state.seen_captures.clear()
        self.review.conn.close()
        self.pending.conn.close()
        review_store.REVIEW_DIR = self.saved_review_dir
        super().tearDown()

    def upload(self, picture, station='pi', capture_id='c1', **form):
        """A capture uploaded as the computer station sends it (camera=1, mode auto, foil_is_image: the flat card) unless form says otherwise"""
        jpeg = cv2.imencode('.jpg', cv2.cvtColor(picture, cv2.COLOR_RGB2BGR))[1].tobytes()
        data = {'name': station, 'camera': '1', 'mode': 'auto', 'wait': '0', 'capture_id': capture_id, 'foil_is_image': '1', **form}
        data = {k: v for k, v in data.items() if v is not None}
        from io import BytesIO
        data['image'] = (BytesIO(jpeg), 'card.jpg')
        return self.client.post(f'/api/stations/{station}/captures', data=data, content_type='multipart/form-data')

    def stopped(self):
        return [data for event, data in self.events if event == 'card_back_seen']

    # ----- a back stops the camera, whichever way it came

    def test_an_auto_back_stops_the_camera_and_is_not_read(self):
        answer = self.upload(degraded(BACK))
        self.assertEqual((answer.status_code, answer.json['status'], answer.json['capture']), (200, 'card_back', 1))
        self.assertFalse(self.camera.auto_capture_enabled)
        self.assertIn(('auto_capture_toggled', {'enabled': False}), self.events)
        self.assertEqual(len(self.stopped()), 1)
        self.api.submit_capture.assert_not_called()
        self.assertEqual(self.pending.all(), [])                     # nothing on record to be read

    def test_a_review_back_stops_the_camera_and_releases_the_stations_hold(self):
        answer = self.upload(cv2.rotate(BACK, cv2.ROTATE_180), mode='review')      # face down, either way up
        self.assertEqual(answer.json['status'], 'card_back')
        self.assertFalse(self.camera.auto_capture_enabled)
        self.assertFalse(self.camera.card_under_review)              # or the station waits for an Add / Skip that never comes
        self.assertEqual(self.review.count('mtg', review_store.ANY), 0)

    def test_the_same_back_sent_again_gets_the_same_answer_and_stops_nothing_twice(self):
        first = self.upload(BACK, capture_id='again')
        self.camera.auto_capture_enabled = True                      # the person started it again meanwhile
        second = self.upload(BACK, capture_id='again')
        self.assertEqual((first.json['status'], second.json['status'], second.json['capture']), ('card_back', 'card_back', first.json['capture']))
        self.assertTrue(self.camera.auto_capture_enabled)            # the repeat stopped nothing
        self.assertEqual(len(self.stopped()), 1)

    def test_the_phone_app_that_knows_the_answer_gets_it(self):
        answer = self.upload(BACK, station='phone-a', camera=None, mode=None, accepts='card_back')
        self.assertEqual(answer.json['status'], 'card_back')

    # ----- everything else as before

    def test_a_front_goes_on_as_before(self):
        self.upload(degraded(synthetic_card(3)))
        self.api.submit_capture.assert_called_once()
        self.assertTrue(self.camera.auto_capture_enabled)
        self.assertEqual(self.stopped(), [])

    def test_with_the_setting_off_a_back_is_read_as_a_card(self):
        self.camera.settings.set('stop_at_card_back', False)
        self.upload(BACK)
        self.api.submit_capture.assert_called_once()
        self.assertTrue(self.camera.auto_capture_enabled)

    def test_an_older_phone_app_is_never_answered_card_back(self):
        # it maps an unknown status to an error: its captures are read as before
        self.upload(BACK, station='phone-a', camera=None, mode=None)
        self.api.submit_capture.assert_called_once()
        self.assertEqual(self.stopped(), [])

    def test_a_check_that_fails_is_not_a_back(self):
        with mock.patch.object(card_back, 'card_back_similarity', side_effect=RuntimeError('broken')):
            self.upload(BACK)
        self.api.submit_capture.assert_called_once()
        self.assertTrue(self.camera.auto_capture_enabled)

    # ----- the phone's sync route

    def test_the_sync_route_refuses_a_back(self):
        number = self.upload(BACK, station='phone-a', camera=None, mode=None, accepts='card_back').json['capture']
        answer = self.client.get(f'/api/stations/phone-a/captures/{number}/sync')
        self.assertEqual(answer.status_code, 409)                    # never 'resolved': nothing was added for it
        self.assertIn('back of a card', answer.json['message'])


if __name__ == '__main__':
    unittest.main()
