#!/usr/bin/env python3
"""
The first download of the card data is not given up: a failure is tried again after a pause
(server/card_data.py: start_card_data_update(retry=True)), and the pages are told why.

Run:  venv/bin/python tests/test_first_download.py        (or with the others: -m unittest discover tests)
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

import card_scanner.server.startup  # noqa: E402,F401 - the routes exist once their modules are imported
from card_scanner.server import card_data  # noqa: E402


class Game:
    id, label, source = 'mtg', 'Magic', 'Scryfall'

    def __init__(self, failures, cards=0):
        self.failures, self.cards, self.attempts = failures, cards, 0
        self.done = threading.Event()

    def download(self, progress):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OSError('Network is unreachable')
        self.cards = 1000
        self.done.set()

    def card_count(self):
        return self.cards


class FirstDownload(unittest.TestCase):
    def setUp(self):
        card_data.data_updates_running.discard('mtg')
        card_data.data_update_errors.clear()
        self.patches = [mock.patch.object(card_data, 'FIRST_DOWNLOAD_RETRIES', (0, 0)),
                        mock.patch.object(card_data, 'socketio')]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])

    def run_update(self, game, **kwargs):
        self.assertTrue(card_data.start_card_data_update(game, **kwargs))
        for _ in range(200):
            if 'mtg' not in card_data.data_updates_running:
                return
            threading.Event().wait(0.02)
        self.fail('the update did not end')

    def test_a_failed_first_download_is_tried_again_until_it_works(self):
        game = Game(failures=3)
        self.run_update(game, retry=True)
        self.assertEqual((game.attempts, game.cards), (4, 1000))
        self.assertNotIn('mtg', card_data.data_update_errors)

    def test_while_it_waits_the_pages_can_say_why(self):
        game = Game(failures=1)
        seen = []
        real_sleep = card_data.time.sleep
        with mock.patch.object(card_data.time, 'sleep', lambda seconds: seen.append(card_data.data_update_errors.get('mtg'))):
            self.run_update(game, retry=True)
        self.assertEqual(len(seen), 1)
        self.assertIn('Network is unreachable', seen[0])
        self.assertIn('trying again', seen[0])
        self.assertIsNotNone(real_sleep)

    def test_an_update_of_data_that_is_there_is_not_repeated(self):
        game = Game(failures=5, cards=500)
        self.run_update(game)  # the person's "Update card database"
        self.assertEqual(game.attempts, 1)
        game = Game(failures=5, cards=500)
        self.run_update(game, retry=True)  # even asked to retry: there is data to fall back on
        self.assertEqual(game.attempts, 1)

    def test_one_download_at_a_time(self):
        card_data.data_updates_running.add('mtg')
        try:
            self.assertFalse(card_data.start_card_data_update(Game(failures=0), retry=True))
        finally:
            card_data.data_updates_running.discard('mtg')


if __name__ == '__main__':
    unittest.main()
