"""
The automatic updates (docs/plans/UPDATE_SCHEDULE_PLAN.md): server/card_data.py - the card data's schedule and the picture index's retry.
Stand-ins for the settings, the card database, the printing check and the build; nothing is downloaded and no real data is touched.
(Phase 4 of the plan adds the rest; this holds the cases found by review first, and the Settings page's route of phase 3.)

    venv/bin/python -m unittest discover tests -p test_update_schedule.py
"""
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's modules
from card_scanner.server import lock  # noqa: E402
from card_scanner.server.state import app  # noqa: E402
from card_scanner.station.station_login import Login  # noqa: E402

card_data = sys.modules['card_scanner.server.card_data']
image_index = card_data.image_index
NOW = datetime(2026, 10, 22, 12, 0)
BASE = 'http://localhost'


class FakeSettings(dict):
    """data/settings.json in memory: get / set as app_settings has them"""

    def set(self, key, value):
        self[key] = value


class IndexRetryWhenAnotherBuildHoldsTheIndex(unittest.TestCase):
    """A build from the command line holds the index: the schedule's attempt never began, so it must not use up the interval"""

    def setUp(self):
        self.settings = FakeSettings(image_index_every_hours=168)    # a week: the interval a skip must not use up
        checker = mock.Mock()
        checker.index.return_value = object()                     # an index exists
        status = {'missing': 10, 'printings': 1000, 'indexed': 990}
        for patch in (mock.patch.object(card_data, 'app_settings', self.settings),
                      mock.patch.object(card_data, 'database', mock.Mock()),
                      mock.patch.object(card_data, 'printing_check', checker),
                      mock.patch.object(card_data, 'index_status', lambda: status),
                      mock.patch.object(card_data, 'data_updates_running', set())):
            patch.start()
            self.addCleanup(patch.stop)

    def tick(self, build, now=NOW):
        """One scheduler tick with image_index.build replaced; waits for the build's thread to report its end"""
        ended = threading.Event()
        real_start = card_data.start_index_update

        def start(on_done=None):
            def reported(ok, message):
                try:
                    if on_done:
                        on_done(ok, message)
                finally:
                    ended.set()
            return real_start(on_done=reported)

        with mock.patch.object(image_index, 'build', build), mock.patch.object(card_data, 'start_index_update', start):
            started = card_data.scheduled_index_update(now)
            if started:
                self.assertTrue(ended.wait(5), 'the build never reported its end')
        return started

    @staticmethod
    def held(*args, **kwargs):
        raise image_index.BuildRunning('a build of this index is running')

    @staticmethod
    def finished(*args, **kwargs):
        result = image_index.BuildResult((1000, 10, 0))
        result.reason = 'done'
        return result

    def test_a_skip_leaves_no_attempt_and_the_next_tick_builds(self):
        self.tick(self.held)
        self.assertIsNone(card_data.last_index_attempt())          # nothing began: nothing noted
        self.assertTrue(self.tick(self.finished, NOW + timedelta(minutes=10)))
        attempt = card_data.last_index_attempt()
        self.assertTrue(attempt['ok'])
        self.assertEqual(attempt['at'], (NOW + timedelta(minutes=10)).isoformat(timespec='seconds'))

    def test_a_skip_puts_back_the_last_real_attempt(self):
        before = {'at': (NOW - timedelta(days=8)).isoformat(timespec='seconds'), 'ok': False, 'message': 'stopped after 3 new pictures'}
        self.settings[card_data.INDEX_AUTO_LAST_KEY] = dict(before)
        self.tick(self.held)
        self.assertEqual(card_data.last_index_attempt(), before)

    def test_a_build_that_began_keeps_its_attempt_even_when_stopped(self):
        def stopped(*args, **kwargs):
            result = image_index.BuildResult((995, 5, 0))
            result.reason = 'cancelled'
            return result
        self.tick(stopped)
        attempt = card_data.last_index_attempt()
        self.assertIs(attempt['ok'], False)
        self.assertFalse(self.tick(self.finished, NOW + timedelta(minutes=10)))   # within the week: not again


class CardDataAttemptWhenTheButtonWasFirst(unittest.TestCase):
    """A download that the button started between the schedule's question and its start is not the schedule's attempt"""

    def test_the_previous_attempt_is_put_back(self):
        settings = FakeSettings(card_data_every_hours=24)
        before = {'at': (NOW - timedelta(days=2)).isoformat(timespec='seconds'), 'ok': True, 'message': '100 cards'}
        settings[card_data.AUTO_LAST_KEY] = {'mtg': dict(before)}
        game = mock.Mock(id='mtg', label='Magic')
        game.card_count.return_value = 100
        database = mock.Mock()
        database.get_data_info.return_value = {'downloaded_at': (NOW - timedelta(days=2)).isoformat()}
        with mock.patch.object(card_data, 'app_settings', settings), mock.patch.object(card_data, 'database', database), \
                mock.patch.object(card_data.games, 'all_games', lambda: [game]), \
                mock.patch.object(card_data, 'start_card_data_update', lambda game, on_done=None: False):
            self.assertEqual(card_data.scheduled_card_data_updates(NOW), [])
            self.assertEqual(card_data.last_auto_attempt('mtg'), before)


class RefusedBuildWording(unittest.TestCase):
    """A build Scryfall refused tells the schedule what happened, without the button's 'press the button again later'"""

    def test_the_schedule_is_told_without_the_button(self):
        told = []
        ended = threading.Event()

        def refused(*args, **kwargs):
            result = image_index.BuildResult((995, 5, 0))
            result.reason = 'refused'
            return result
        with mock.patch.object(card_data, 'database', mock.Mock()), mock.patch.object(card_data, 'printing_check', mock.Mock()), \
                mock.patch.object(image_index, 'build', refused):
            card_data.start_index_update(on_done=lambda ok, message: (told.append((ok, message)), ended.set()))
            self.assertTrue(ended.wait(5))
        ok, message = told[0]
        self.assertIs(ok, False)
        self.assertIn('rate limit', message)
        self.assertNotIn('press the button', message)
        self.assertIn('press the button again later', card_data.index_state['error'])     # the page's own text stays the button's


class ScheduleRoute(unittest.TestCase):
    """/api/updates/schedule: open to read, guarded to change, every value checked before any is saved"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.login = Login(file=Path(folder.name) / 'settings_login.json', variable='TEST_SETTINGS_PASSWORD', cookie=lock.COOKIE,
                           session_seconds=lock.SESSION_SECONDS)
        self.settings = FakeSettings()
        game = mock.Mock(id='mtg', label='Magic')
        database = mock.Mock()
        self.downloaded = datetime.now() - timedelta(days=2)
        database.get_data_info.return_value = {'downloaded_at': self.downloaded.isoformat(timespec='seconds')}
        for patch in (mock.patch.object(lock, 'login', self.login),
                      mock.patch.object(card_data, 'app_settings', self.settings),
                      mock.patch.object(card_data, 'database', database),
                      mock.patch.object(card_data.games, 'active', lambda: game)):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = app.test_client()

    def post(self, body):
        return self.client.post('/api/updates/schedule', json=body, base_url=BASE)

    def test_reading_is_open_and_off_by_default(self):
        self.login.set_password('correct horse')
        answer = self.client.get('/api/updates/schedule', base_url=BASE)
        self.assertEqual(answer.status_code, 200)
        state = answer.get_json()
        self.assertEqual(state['card_data']['every_hours'], 0)
        self.assertEqual(state['image_index']['every_hours'], 0)
        self.assertIsNone(state['card_data']['next'])
        self.assertIsNone(state['card_data']['last'])

    def test_a_change_needs_the_settings_password_when_there_is_one(self):
        self.login.set_password('correct horse')
        answer = self.post({'card_data': 24})
        self.assertEqual(answer.status_code, 401)
        self.assertTrue(answer.get_json()['locked'])
        self.assertNotIn('card_data_every_hours', self.settings)

    def test_a_change_is_saved_and_answered_with_the_new_state(self):
        state = self.post({'card_data': 168, 'image_index': 6}).get_json()
        self.assertEqual(self.settings['card_data_every_hours'], 168)
        self.assertEqual(self.settings['image_index_every_hours'], 6)
        self.assertEqual(state['card_data']['every_hours'], 168)
        # Due a week after the last download, shown with the server's offset
        self.assertEqual(datetime.fromisoformat(state['card_data']['next']).replace(tzinfo=None),
                         self.downloaded.replace(microsecond=0) + timedelta(hours=168))
        self.assertIsNotNone(datetime.fromisoformat(state['card_data']['next']).tzinfo)

    def test_data_older_than_the_interval_is_due_now(self):
        state = self.post({'card_data': 24}).get_json()
        self.assertLessEqual(datetime.fromisoformat(state['card_data']['next']).replace(tzinfo=None), datetime.now())

    def test_wrong_values_are_refused_and_nothing_is_saved(self):
        for body in ({'card_data': True}, {'card_data': 12}, {'card_data': '24'}, {'image_index': 72}, {'backup': 24}, {},
                     {'card_data': 24, 'image_index': 5}, [24], None):
            answer = self.post(body)
            self.assertEqual(answer.status_code, 400, body)
            self.assertEqual(self.settings, {}, body)


if __name__ == '__main__':
    unittest.main()
