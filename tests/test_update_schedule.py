"""
The automatic updates (docs/plans/UPDATE_SCHEDULE_PLAN.md): server/card_data.py - the card data's schedule and the picture index's retry.
Stand-ins for the settings, the card database, the printing check and the build; nothing is downloaded and no real data is touched.
Phases 2-4 of the plan: the cases found by review first, the Settings page's route, then the two due rules, the loop's tick and the config fallback.

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



def a_game(game_id, cards=100):
    """A game as the schedule sees it: an id, a label and a number of cards (0: no card data yet)"""
    game = mock.Mock(id=game_id, label=game_id.upper())
    game.card_count.return_value = cards
    return game


class CardDataSchedule(unittest.TestCase):
    """scheduled_card_data_updates / card_data_due: when the card data is downloaded on its own, and what is remembered of it"""

    def setUp(self):
        self.settings = FakeSettings(card_data_every_hours=24)
        self.downloaded = {}                      # game id -> downloaded_at (None: card data from before card_data_info)
        self.games = [a_game('mtg')]
        self.started = []
        self.outcome = (True, '100 cards')
        database = mock.Mock()
        database.get_data_info.side_effect = lambda game_id: {'downloaded_at': self.downloaded.get(game_id)}

        def start(game, on_done=None):
            # The download itself is not this test's: it "ends" at once, as self.outcome says
            self.started.append(game.id)
            on_done(*self.outcome)
            return True
        for patch in (mock.patch.object(card_data, 'app_settings', self.settings),
                      mock.patch.object(card_data, 'database', database),
                      mock.patch.object(card_data.games, 'all_games', lambda: self.games),
                      mock.patch.object(card_data, 'start_card_data_update', start),
                      mock.patch.object(card_data, 'data_updates_running', set())):
            patch.start()
            self.addCleanup(patch.stop)

    def downloaded_ago(self, game_id, **ago):
        self.downloaded[game_id] = (NOW - timedelta(**ago)).isoformat(timespec='seconds')

    def test_off_downloads_nothing(self):
        self.settings['card_data_every_hours'] = 0
        self.downloaded_ago('mtg', days=30)
        self.assertEqual(card_data.scheduled_card_data_updates(NOW), [])

    def test_due_once_the_data_is_as_old_as_the_interval(self):
        self.downloaded_ago('mtg', hours=23, minutes=59)
        self.assertEqual(card_data.scheduled_card_data_updates(NOW), [])
        self.downloaded_ago('mtg', hours=24)
        self.assertEqual(card_data.scheduled_card_data_updates(NOW), ['mtg'])

    def test_data_without_a_date_is_old_enough(self):
        self.downloaded['mtg'] = None
        self.assertEqual(card_data.scheduled_card_data_updates(NOW), ['mtg'])

    def test_how_it_ended_is_noted_beside_when_it_began(self):
        self.downloaded_ago('mtg', days=2)
        card_data.scheduled_card_data_updates(NOW)
        self.assertEqual(card_data.last_auto_attempt('mtg'), {'at': NOW.isoformat(timespec='seconds'), 'ok': True, 'message': '100 cards'})

    def test_a_failed_download_waits_an_hour_also_across_a_restart(self):
        self.downloaded_ago('mtg', days=2)
        self.outcome = (False, 'Scryfall is down')
        card_data.scheduled_card_data_updates(NOW)
        self.assertIs(card_data.last_auto_attempt('mtg')['ok'], False)
        # A restart: a new settings object holding what the file held - the back-off is in it, not in memory
        restarted = FakeSettings(self.settings)
        with mock.patch.object(card_data, 'app_settings', restarted):
            self.assertEqual(card_data.scheduled_card_data_updates(NOW + timedelta(minutes=50)), [])
            self.assertEqual(card_data.scheduled_card_data_updates(NOW + timedelta(minutes=60)), ['mtg'])

    def test_every_game_with_data_and_none_without(self):
        self.games = [a_game('mtg'), a_game('other'), a_game('empty', cards=0)]
        self.downloaded_ago('mtg', days=2)
        self.downloaded_ago('other', hours=1)                       # fresh: not due
        self.downloaded['empty'] = None                              # the first download is startup's, never the schedule's
        self.assertEqual(card_data.scheduled_card_data_updates(NOW), ['mtg'])

    def test_a_running_download_is_left_alone(self):
        self.downloaded_ago('mtg', days=2)
        with mock.patch.object(card_data, 'data_updates_running', {'mtg'}):
            self.assertEqual(card_data.scheduled_card_data_updates(NOW), [])
        self.assertIsNone(card_data.last_auto_attempt('mtg'))        # nothing noted for it

    def test_one_game_failing_does_not_keep_the_next_from_its_update(self):
        broken = a_game('broken')
        broken.card_count.side_effect = OSError('database is locked')
        self.games = [broken, a_game('mtg')]
        self.downloaded_ago('mtg', days=2)
        with mock.patch.object(card_data.logger, 'exception') as logged:   # logged, not raised - and kept out of the test output
            self.assertEqual(card_data.scheduled_card_data_updates(NOW), ['mtg'])
        logged.assert_called_once()

    def test_config_yaml_decides_until_the_person_chooses(self):
        self.downloaded_ago('mtg', days=2)
        del self.settings['card_data_every_hours']
        with mock.patch.object(card_data.Config, 'DATABASE_AUTO_UPDATE_HOURS', 24):
            self.assertEqual(card_data.card_data_schedule(), 24)
            self.settings['card_data_every_hours'] = 0               # the person's "off" wins over config.yaml
            self.assertEqual(card_data.card_data_schedule(), 0)
        del self.settings['card_data_every_hours']
        for wrong in (12, True, '24', None):                         # a value that is not a choice is off, never a guess
            with mock.patch.object(card_data.Config, 'DATABASE_AUTO_UPDATE_HOURS', wrong):
                self.assertEqual(card_data.card_data_schedule(), 0, wrong)


class CardDataDownloadEnd(unittest.TestCase):
    """start_card_data_update(on_done=): told once how the download ended, and a callback that fails does not lose the download"""

    def run_download(self, download, on_done):
        ended = threading.Event()
        game = a_game('test-game')
        game.download.side_effect = download

        def told(ok, message):
            try:
                on_done(ok, message)
            finally:
                ended.set()
        with mock.patch.object(card_data, 'extend_index_after_update', lambda: False), \
                mock.patch.object(card_data, 'socketio', mock.Mock()):
            self.assertTrue(card_data.start_card_data_update(game, on_done=told))
            self.assertTrue(ended.wait(5))
        for _ in range(100):                       # the thread's finally (data_updates_running) comes right after
            if 'test-game' not in card_data.data_updates_running:
                break
            threading.Event().wait(0.01)
        self.assertNotIn('test-game', card_data.data_updates_running)

    def test_a_download_that_worked(self):
        told = []
        self.run_download(lambda progress: None, lambda ok, message: told.append((ok, message)))
        self.assertEqual(told, [(True, '100 cards')])

    def test_a_download_that_failed(self):
        told = []

        def fails(progress):
            raise OSError('connection reset')
        self.run_download(fails, lambda ok, message: told.append((ok, message)))
        self.assertEqual(told, [(False, 'connection reset')])

    def test_a_callback_that_raises_is_only_logged(self):
        def raises(ok, message):
            raise RuntimeError('settings file is read-only')
        with mock.patch.object(card_data.logger, 'exception'):
            self.run_download(lambda progress: None, raises)           # ends, and lets go of the game (checked in run_download)


class IndexSchedule(unittest.TestCase):
    """index_due / scheduled_index_update: when the picture index goes back to what is missing"""

    def setUp(self):
        self.settings = FakeSettings(image_index_every_hours=24)
        self.status = {'missing': 10, 'printings': 1000, 'indexed': 990}
        self.checker = mock.Mock()
        self.checker.index.return_value = object()
        self.started = []

        def start(on_done=None):
            self.started.append(1)
            on_done(True, '10 new pictures')
            return True
        for patch in (mock.patch.object(card_data, 'app_settings', self.settings),
                      mock.patch.object(card_data, 'database', mock.Mock()),
                      mock.patch.object(card_data, 'printing_check', self.checker),
                      mock.patch.object(card_data, 'index_status', lambda: self.status),
                      mock.patch.object(card_data, 'start_index_update', start),
                      mock.patch.object(card_data, 'data_updates_running', set())):
            patch.start()
            self.addCleanup(patch.stop)

    def test_due_when_something_is_missing(self):
        self.assertTrue(card_data.scheduled_index_update(NOW))
        self.assertEqual(card_data.last_index_attempt(), {'at': NOW.isoformat(timespec='seconds'), 'ok': True, 'message': '10 new pictures'})

    def test_off_builds_nothing(self):
        self.settings['image_index_every_hours'] = 0
        self.assertFalse(card_data.scheduled_index_update(NOW))

    def test_never_a_first_build(self):
        self.checker.index.return_value = None
        self.assertFalse(card_data.scheduled_index_update(NOW))
        self.assertEqual(self.started, [])

    def test_nothing_missing_nothing_built(self):
        self.status = {'missing': 0, 'printings': 1000, 'indexed': 1000}
        self.assertFalse(card_data.scheduled_index_update(NOW))

    def test_a_first_build_stopped_early_stays_the_buttons(self):
        self.status = {'missing': 101, 'printings': 1000, 'indexed': 899}     # just under 90%
        self.assertFalse(card_data.scheduled_index_update(NOW))
        self.status = {'missing': 100, 'printings': 1000, 'indexed': 900}     # 90%: the schedule's
        self.assertTrue(card_data.scheduled_index_update(NOW))

    def test_not_while_a_build_or_a_card_data_download_runs(self):
        with mock.patch.dict(card_data.index_state, running=True):
            self.assertFalse(card_data.scheduled_index_update(NOW))
        with mock.patch.object(card_data, 'data_updates_running', {'mtg'}):  # its success extends the index itself
            self.assertFalse(card_data.scheduled_index_update(NOW))
        self.assertEqual(self.started, [])

    def test_once_per_interval(self):
        self.assertTrue(card_data.scheduled_index_update(NOW))
        self.assertFalse(card_data.scheduled_index_update(NOW + timedelta(hours=23, minutes=59)))
        self.assertTrue(card_data.scheduled_index_update(NOW + timedelta(hours=24)))

    def test_config_yaml_decides_until_the_person_chooses(self):
        del self.settings['image_index_every_hours']
        with mock.patch.object(card_data.Config, 'READING_INDEX_RETRY_HOURS', 6):
            self.assertEqual(card_data.index_schedule(), 6)
        for wrong in (72, True, '6'):
            with mock.patch.object(card_data.Config, 'READING_INDEX_RETRY_HOURS', wrong):
                self.assertEqual(card_data.index_schedule(), 0, wrong)


class ScheduleTick(unittest.TestCase):
    """schedule_tick: the update check, the card data and the index each get their turn, whatever the others do"""

    def tick(self, check_due, downloads_due, **failing):
        called = []

        def step(name):
            def run(*args):
                called.append(name)
                if name in failing:
                    raise failing[name]
            return run
        with mock.patch.object(card_data, 'check_card_data_updates', step('check')), \
                mock.patch.object(card_data, 'scheduled_card_data_updates', step('card_data')), \
                mock.patch.object(card_data, 'scheduled_index_update', step('index')), \
                mock.patch.object(card_data.logger, 'error'):
            card_data.schedule_tick(check_due, downloads_due)
        return called

    def test_in_order_card_data_before_the_index(self):
        self.assertEqual(self.tick(True, True), ['check', 'card_data', 'index'])

    def test_one_failing_does_not_keep_the_others_from_their_turn(self):
        self.assertEqual(self.tick(True, True, check=OSError('no network')), ['check', 'card_data', 'index'])
        self.assertEqual(self.tick(True, True, card_data=RuntimeError('bad settings')), ['check', 'card_data', 'index'])

    def test_no_download_in_the_first_minutes(self):
        self.assertEqual(self.tick(True, False), ['check'])
        self.assertEqual(self.tick(False, True), ['card_data', 'index'])


if __name__ == '__main__':
    unittest.main()
