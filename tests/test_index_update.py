"""
The picture index's button (Settings > Storage): server/card_data.py - index_status, start_index_update, the three /api/image_index routes - and what it needs of
core/image_index.build (a progress callback and a cancel switch). Synthetic pictures and stand-ins; nothing is downloaded and no real data is touched.

    venv/bin/python -m unittest discover tests -p test_index_update.py
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's routes
from card_scanner.core import image_index  # noqa: E402
from card_scanner.server import lock  # noqa: E402
from card_scanner.server.state import app  # noqa: E402
from card_scanner.station.station_login import Login  # noqa: E402
from test_printing_check import write_pictures  # noqa: E402

card_data = sys.modules['card_scanner.server.card_data']
BASE = 'http://localhost'


class Build(unittest.TestCase):
    """core/image_index.build: progress after every printing, and a cancel that keeps what was done and can be continued"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.pictures = self.root / 'pictures'
        self.pictures.mkdir()
        write_pictures(self.pictures, range(60))
        self.printings = [(f'p{n}', None) for n in range(60)]

    def build(self, **kw):
        return image_index.build(self.printings, self.root / 'index', image_dir=self.pictures, fetch=False, log=lambda m: None, workers=1, **kw)

    def test_progress_is_reported_after_every_printing(self):
        calls = []
        self.build(progress=lambda done, total: calls.append((done, total)))
        self.assertEqual(calls, [(n, 60) for n in range(1, 61)])

    def test_without_progress_or_cancel_it_is_as_before(self):
        self.assertEqual(self.build(), (60, 60, 0))

    def test_a_cancel_keeps_what_was_done_and_a_second_run_goes_on_from_there(self):
        cancel = threading.Event()
        logged = []
        kept, new, skipped = image_index.build(self.printings, self.root / 'index', image_dir=self.pictures, fetch=False, log=logged.append, workers=1,
                                               progress=lambda done, total: cancel.set() if done >= 3 else None, cancel=cancel)
        self.assertLess(new, 60)                                   # it stopped
        self.assertGreaterEqual(new, 3)
        self.assertEqual(skipped, 0)                               # what it left undone is not "without a picture"
        self.assertEqual(kept, new)
        self.assertTrue(any('Stopped on request' in line for line in logged))
        self.assertEqual(len(image_index.ImageIndex.load(self.root / 'index')), new)      # written, readable
        # again, not cancelled: the rest, and the whole is the whole
        kept2, new2, skipped2 = self.build()
        self.assertEqual((kept2, new2 + new, skipped2), (60, 60, 0))

    def test_the_result_says_how_the_run_ended_and_is_still_a_plain_triple(self):
        done = self.build()
        self.assertEqual((done.reason, tuple(done)), ('done', (60, 60, 0)))
        kept, new, skipped = done                                  # callers unpack it as ever
        cancel = threading.Event()
        cancel.set()
        stopped = self.build(cancel=cancel)
        self.assertEqual(stopped.reason, 'cancelled')

    def test_a_cancel_set_before_it_starts_does_nothing_and_writes_nothing_wrong(self):
        cancel = threading.Event()
        cancel.set()
        kept, new, skipped = self.build(cancel=cancel)
        self.assertEqual((kept, new, skipped), (0, 0, 0))


class Downloads(unittest.TestCase):
    """build's download loop, with requests.get stood in for (it had no test at all: every other test builds from a folder of pictures)"""

    class Answer:
        def __init__(self, status, headers=None, content=b''):
            self.status_code, self.headers, self.content = status, headers or {}, content
            self.ok = status == 200

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name) / 'index'
        self.printings = [(f'p{n}', f'https://cards.example/p{n}.jpg') for n in range(8)]

    def run_build(self, cancel, workers=2):
        """build in a thread; returns (thread, result list)"""
        result = []
        thread = threading.Thread(target=lambda: result.append(image_index.build(self.printings, self.directory, fetch=True, workers=workers,
                                                                                 log=lambda m: None, cancel=cancel)), daemon=True)
        thread.start()
        return thread, result

    def test_a_stop_ends_workers_that_are_waiting_out_a_429_and_no_request_follows_it(self):
        # The review of 2026-10-20: the cancel was only looked at when a printing STARTED, so workers sleeping after a 429 (a minute or more) ignored it
        # and the page stayed on "Stopping" with the build's lock held. Here every answer is "wait ten minutes".
        calls = []
        cancel = threading.Event()

        def refused(url, headers=None, timeout=None):
            calls.append(url)
            return self.Answer(429, {'Retry-After': '600'})
        with mock.patch('requests.get', refused):
            thread, result = self.run_build(cancel)
            for _ in range(200):                       # both workers have been told to wait (two requests, each refused)
                if len(calls) >= 2:
                    break
                threading.Event().wait(0.02)
            self.assertGreaterEqual(len(calls), 2)
            at_stop = len(calls)
            cancel.set()
            thread.join(5)                             # ten minutes of waiting must end in well under that
            self.assertFalse(thread.is_alive(), 'the build did not stop while its workers waited after a 429')
            self.assertEqual(len(calls), at_stop, 'a request was sent after the stop')
        self.assertEqual(result[0][1], 0)              # nothing new: it was refused throughout

    def test_a_stop_before_a_workers_turn_sends_nothing(self):
        calls = []
        cancel = threading.Event()
        cancel.set()
        with mock.patch('requests.get', lambda *a, **kw: calls.append(a) or self.Answer(200)):
            thread, result = self.run_build(cancel)
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])

    def refusing(self, status=403):
        return lambda url, headers=None, timeout=None: self.Answer(status, {'Retry-After': '1'})

    def test_a_scryfall_that_keeps_refusing_ends_the_run_as_refused_with_what_was_done_kept(self):
        # The review of 2026-10-20: this ended like a success. Four refusals in a row stop the run (one second of waiting each here, instead of a minute)
        self.printings = self.printings[:1]
        with mock.patch('requests.get', self.refusing()):
            built = image_index.build(self.printings, self.directory, fetch=True, workers=1, log=lambda m: None)
        self.assertEqual(built.reason, 'refused')
        self.assertEqual(tuple(built), (0, 0, 1))                 # nothing new; the printing it could not fetch is not in the index

    def test_without_a_stop_a_429_is_still_waited_out_and_the_picture_comes(self):
        # the loop's normal path is as it was: a wait of Retry-After seconds (here almost none), then the same address again
        import cv2
        import numpy as np
        ok, jpeg = cv2.imencode('.jpg', (np.random.default_rng(1).integers(0, 256, (680, 488, 3))).astype(np.uint8))
        answers = [self.Answer(429, {'Retry-After': '1'}), self.Answer(200, content=jpeg.tobytes())]
        calls = []

        def get(url, headers=None, timeout=None):
            calls.append(url)
            return answers.pop(0) if answers else self.Answer(200, content=jpeg.tobytes())
        self.printings = self.printings[:1]
        with mock.patch('requests.get', get):
            kept, new, skipped = image_index.build(self.printings, self.directory, fetch=True, workers=1, log=lambda m: None)
        self.assertEqual((kept, new, skipped), (1, 1, 0))
        self.assertEqual(calls, ['https://cards.example/p0.jpg'] * 2)      # the refused one was asked for again


class FakeDatabase:
    def __init__(self, printings):
        self.printings = printings

    def printing_images(self):
        return list(self.printings)


class FakeIndex:
    built = '2026-10-20 12:00:00'

    def __init__(self, ids):
        self.ids = set(ids)

    def row(self, printing):
        return 0 if printing in self.ids else None


class State(unittest.TestCase):
    """Patches the module's database, printing check and state; each test starts with no build running"""

    def setUp(self):
        self.database = FakeDatabase([('a', 'http://x/a'), ('b', 'http://x/b'), ('c', 'http://x/c'), ('d', None)])    # d has no picture
        self.index = FakeIndex(['a', 'b', 'zzz'])                                                                    # zzz: no longer in the card data
        self.check = mock.Mock(index=mock.Mock(side_effect=lambda: self.index), index_folder=Path('/nonexistent/index'))
        for name, value in (('database', self.database), ('printing_check', self.check)):
            patcher = mock.patch.object(card_data, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        card_data.index_state.update(running=False, stopping=False, done=0, total=0, error=None, result=None)
        card_data.index_cancel.clear()
        self.addCleanup(lambda: card_data.index_state.update(running=False, stopping=False, done=0, total=0, error=None, result=None))


class Status(State):
    def test_it_counts_the_printings_with_a_picture_and_those_in_the_index(self):
        status = card_data.index_status()
        self.assertEqual((status['printings'], status['indexed'], status['missing']), (3, 2, 1))     # d is not counted; zzz is not either
        self.assertEqual(status['built'], '2026-10-20 12:00:00')
        self.assertFalse(status['running'])

    def test_without_an_index_every_printing_is_missing(self):
        self.index = None
        status = card_data.index_status()
        self.assertEqual((status['indexed'], status['missing'], status['built']), (0, 3, None))


class Background(State):
    """start_index_update with a build that waits for the test"""

    @staticmethod
    def result(kept=3, new=1, skipped=0, reason='done'):
        built = image_index.BuildResult((kept, new, skipped))
        built.reason = reason
        return built

    def fake_build(self, release, results=None, error=None):
        seen = {}

        def build(printings, folder, fetch=True, log=None, progress=None, cancel=None, **kw):
            seen.update(printings=printings, folder=folder, fetch=fetch, cancel=cancel)
            progress(1, 4)
            release.wait(10)
            if error:
                raise error
            if results is not None:
                return results
            return self.result(reason='cancelled' if cancel is not None and cancel.is_set() else 'done')     # as the real build says how it ended
        return build, seen

    def wait_until_idle(self):
        for _ in range(200):
            if not card_data.index_state['running']:
                return
            threading.Event().wait(0.02)
        self.fail('the build did not end')

    def test_a_build_runs_reports_progress_and_records_how_it_ended(self):
        release = threading.Event()
        build, seen = self.fake_build(release)
        with mock.patch.object(card_data.image_index, 'build', build):
            self.assertTrue(card_data.start_index_update())
            for _ in range(200):
                if card_data.index_state['done']:
                    break
                threading.Event().wait(0.02)
            status = card_data.index_status()
            self.assertEqual((status['running'], status['done'], status['total']), (True, 1, 4))
            release.set()
            self.wait_until_idle()
        status = card_data.index_status()
        self.assertEqual(status['result'], {'printings': 3, 'new': 1, 'skipped': 0, 'stopped': False})
        self.assertIsNone(status['error'])
        self.assertEqual((seen['printings'], seen['folder'], seen['fetch']), (self.database.printings, Path('/nonexistent/index'), True))

    def test_a_second_build_is_refused_while_one_runs(self):
        release = threading.Event()
        build, _ = self.fake_build(release)
        with mock.patch.object(card_data.image_index, 'build', build):
            self.assertTrue(card_data.start_index_update())
            self.assertFalse(card_data.start_index_update())
            release.set()
            self.wait_until_idle()

    def test_a_failure_is_recorded_and_a_new_build_can_start(self):
        release = threading.Event()
        release.set()
        build, _ = self.fake_build(release, error=RuntimeError('Scryfall is down'))
        with mock.patch.object(card_data.image_index, 'build', build):
            card_data.start_index_update()
            self.wait_until_idle()
            self.assertEqual(card_data.index_status()['error'], 'Scryfall is down')
            self.assertTrue(card_data.start_index_update())            # an error does not block the next try
            self.wait_until_idle()

    def test_a_build_started_elsewhere_is_said_so(self):
        release = threading.Event()
        release.set()
        build, _ = self.fake_build(release, error=image_index.BuildRunning('Another build of this index is running'))
        with mock.patch.object(card_data.image_index, 'build', build):
            card_data.start_index_update()
            self.wait_until_idle()
        self.assertIn('Another build', card_data.index_status()['error'])
        self.assertIn('command line', card_data.index_status()['error'])

    def test_stopping_reaches_the_build_and_is_recorded(self):
        release = threading.Event()
        build, seen = self.fake_build(release)
        with mock.patch.object(card_data.image_index, 'build', build):
            card_data.start_index_update()
            for _ in range(200):
                if seen:
                    break
                threading.Event().wait(0.02)
            answer = app.test_client().post('/api/image_index/stop', base_url=BASE).get_json()
            self.assertTrue(answer['stopping'])
            self.assertTrue(seen['cancel'].is_set())                  # the very event the build was given
            release.set()
            self.wait_until_idle()
        self.assertTrue(card_data.index_status()['result']['stopped'])


class Refused(State):
    """The button's build against a Scryfall that keeps refusing (real build, requests.get stood in for): the page must be told it did not finish"""

    def test_an_exhausted_403_is_an_incomplete_build_with_a_retry_message_not_a_success(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.check.index_folder = Path(folder.name) / 'index'
        self.database.printings = [('a', 'http://x/a'), ('b', 'http://x/b')]
        refusing = lambda url, headers=None, timeout=None: Downloads.Answer(403, {'Retry-After': '1'})
        with mock.patch('requests.get', refusing):
            self.assertTrue(card_data.start_index_update())
            for _ in range(600):
                if not card_data.index_state['running']:
                    break
                threading.Event().wait(0.05)
        answer = app.test_client().get('/api/image_index', base_url=BASE).get_json()
        self.assertFalse(answer['running'])
        self.assertIn('Scryfall kept refusing', answer['error'])        # the page shows this, not "updated"
        self.assertIn('press the button again later', answer['error'])
        self.assertEqual((answer['result']['new'], answer['result']['stopped']), (0, False))
        # and the next press is allowed: a refusal is not a lock
        with mock.patch('requests.get', lambda *a, **kw: Downloads.Answer(404)):
            self.assertTrue(card_data.start_index_update())
            for _ in range(200):
                if not card_data.index_state['running']:
                    break
                threading.Event().wait(0.05)
        self.assertIsNone(card_data.index_status()['error'])            # a run that is not refused clears it


class Routes(State):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.login = Login(file=Path(self.folder.name) / 'settings_login.json', variable='TEST_SETTINGS_PASSWORD', cookie=lock.COOKIE,
                           session_seconds=lock.SESSION_SECONDS)
        patcher = mock.patch.object(lock, 'login', self.login)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.test_client()
        self.release = threading.Event()
        self.release.set()
        patcher = mock.patch.object(card_data.image_index, 'build', lambda *a, progress=None, **kw: Background.result())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_state_is_open_to_everyone(self):
        self.login.set_password('correct horse')
        answer = self.client.get('/api/image_index', base_url=BASE)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(set(answer.get_json()), {'success', 'printings', 'indexed', 'missing', 'built', 'running', 'stopping', 'done', 'total', 'error', 'result'})

    def test_starting_and_stopping_need_the_settings_password_when_there_is_one(self):
        self.login.set_password('correct horse')
        for path in ('/api/image_index/update', '/api/image_index/stop'):
            answer = self.client.post(path, base_url=BASE)
            self.assertEqual(answer.status_code, 401, path)
            self.assertTrue(answer.get_json()['locked'])
        self.assertFalse(card_data.index_state['running'])             # nothing started

    def test_without_a_password_it_starts_and_a_second_start_is_a_conflict(self):
        with mock.patch.object(card_data.image_index, 'build', lambda *a, progress=None, cancel=None, **kw: (self.release.wait(10), Background.result())[1]):
            self.release.clear()
            first = self.client.post('/api/image_index/update', base_url=BASE)
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.get_json()['running'])
            second = self.client.post('/api/image_index/update', base_url=BASE)
            self.assertEqual(second.status_code, 409)
            self.assertFalse(second.get_json()['success'])
            self.release.set()
            for _ in range(200):
                if not card_data.index_state['running']:
                    break
                threading.Event().wait(0.02)

    def test_nothing_to_start_without_card_data(self):
        with mock.patch.object(card_data, 'database', None):
            self.assertEqual(self.client.post('/api/image_index/update', base_url=BASE).status_code, 503)


if __name__ == '__main__':
    unittest.main()
