"""
The automatic updates (docs/plans/UPDATE_SCHEDULE_PLAN.md): server/card_data.py - the card data's schedule and the picture index's retry.
Stand-ins for the settings, the card database, the printing check and the build; nothing is downloaded and no real data is touched.
(Phase 4 of the plan adds the rest; this holds the cases found by review first.)

    venv/bin/python -m unittest discover tests -p test_update_schedule.py
"""
import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's modules

card_data = sys.modules['card_scanner.server.card_data']
image_index = card_data.image_index
NOW = datetime(2026, 10, 22, 12, 0)


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


if __name__ == '__main__':
    unittest.main()
