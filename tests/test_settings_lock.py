"""
The settings password (server/lock.py): without one nothing is locked; with one, the AI keys, restart,
network access, debug mode and the backups' restore / upload / download / delete need the unlocked
cookie - routes answer 401 {"locked": true}, Socket.IO events an `error` event. Flask's test client and
a temporary password file; nothing real is touched (the guarded actions are refused before they run).

    venv/bin/python -m unittest discover tests -p test_settings_lock.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's routes
from card_scanner.server import lock  # noqa: E402
from card_scanner.server.state import app, socketio  # noqa: E402
from card_scanner.station.station_login import Login  # noqa: E402

BASE = 'http://localhost'
GUARDED = [('post', '/api/restart'), ('post', '/api/quit'), ('post', '/api/network_access'),
           ('post', '/api/backups/x/restore'), ('post', '/api/backups/upload'), ('get', '/api/backups/x/download'),
           ('delete', '/api/backups/x'), ('post', '/api/backups/full')]


class SettingsLock(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = Path(self.folder.name) / 'settings_login.json'
        patch = mock.patch.object(lock, 'login', Login(file=self.file, variable='TEST_SETTINGS_PASSWORD',
                                                       cookie=lock.COOKIE, session_seconds=lock.SESSION_SECONDS))
        patch.start()
        self.addCleanup(patch.stop)
        # An unlocked restart would really stop the process (a timer sends it SIGTERM): never here
        stop = mock.patch.object(sys.modules['card_scanner.server.program'], '_stop_soon')
        self.stopped = stop.start()
        self.addCleanup(stop.stop)
        self.client = app.test_client()

    def call(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url=BASE, **kwargs)

    def set_password(self, password='correct horse'):
        answer = self.call('post', '/api/settings_password', json={'password': password})
        self.assertEqual(answer.status_code, 200, answer.get_json())

    def test_nothing_is_locked_without_a_password(self):
        status = self.call('get', '/api/settings_lock').get_json()
        self.assertEqual((status['has_password'], status['unlocked']), (False, True))
        for method, path in GUARDED:
            self.assertNotEqual(self.call(method, path).status_code, 401, path)
        self.stopped.assert_called_once()      # the restart was allowed (and stubbed)

    def test_with_a_password_every_guarded_route_is_locked(self):
        self.set_password()
        stranger = app.test_client()
        for method, path in GUARDED:
            answer = getattr(stranger, method)(path, base_url=BASE)
            self.assertEqual(answer.status_code, 401, path)
            self.assertTrue(answer.get_json()['locked'], path)
        self.stopped.assert_not_called()       # the locked restart did nothing

    def test_the_listings_and_scanning_stay_open(self):
        self.set_password()
        stranger = app.test_client()
        for path in ('/api/backups', '/api/backups/full', '/api/ai_credentials', '/api/stats', '/api/settings_lock'):
            self.assertNotEqual(stranger.get(path, base_url=BASE).status_code, 401, path)

    def test_the_right_password_unlocks_and_a_wrong_one_does_not(self):
        self.set_password('correct horse')
        other = app.test_client()
        wrong = other.post('/api/settings_lock', json={'password': 'nope'}, base_url=BASE)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(other.post('/api/backups/x/restore', base_url=BASE).status_code, 401)
        self.lock_reset()                                   # the wait after a wrong attempt is not what is tested
        right = other.post('/api/settings_lock', json={'password': 'correct horse'}, base_url=BASE)
        self.assertEqual(right.status_code, 200)
        self.assertIn('HttpOnly', right.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', right.headers['Set-Cookie'])
        self.assertNotEqual(other.post('/api/backups/x/restore', base_url=BASE).status_code, 401)

    def lock_reset(self):
        lock.login.failures, lock.login.locked_until = 0, 0.0

    def test_wrong_passwords_make_the_next_try_wait(self):
        self.set_password()
        other = app.test_client()
        other.post('/api/settings_lock', json={'password': 'nope'}, base_url=BASE)
        again = other.post('/api/settings_lock', json={'password': 'correct horse'}, base_url=BASE)
        self.assertEqual(again.status_code, 429)

    def test_locking_again_removes_the_cookie(self):
        self.set_password()
        self.assertNotEqual(self.call('post', '/api/backups/x/restore').status_code, 401)   # setting it logged this browser in
        self.call('delete', '/api/settings_lock')
        self.client.delete_cookie(lock.COOKIE, domain='localhost')
        self.assertEqual(self.call('post', '/api/backups/x/restore').status_code, 401)

    def test_changing_or_removing_the_password_needs_the_current_one(self):
        self.set_password('first password')
        stranger = app.test_client()
        self.assertEqual(stranger.post('/api/settings_password', json={'password': 'second one!'}, base_url=BASE).status_code, 401)
        self.assertEqual(stranger.post('/api/settings_password', json={'password': ''}, base_url=BASE).status_code, 401)
        self.set_password('second password')                # this browser is unlocked: allowed
        self.lock_reset()
        self.assertEqual(self.call('post', '/api/settings_lock', json={'password': 'second password'}).status_code, 200)
        self.assertEqual(self.call('post', '/api/settings_password', json={'password': ''}).status_code, 200)
        self.assertFalse(self.file.exists())
        self.assertNotEqual(stranger.post('/api/backups/x/restore', base_url=BASE).status_code, 401)   # nothing is locked now
        self.assertFalse(self.call('get', '/api/settings_lock').get_json()['has_password'])

    def test_a_short_password_is_refused_and_it_is_stored_only_as_a_hash(self):
        self.assertEqual(self.call('post', '/api/settings_password', json={'password': 'short'}).status_code, 400)
        self.assertFalse(self.file.exists())
        self.set_password('a long enough one')
        self.assertNotIn('a long enough one', self.file.read_text())
        self.assertEqual(oct(self.file.stat().st_mode & 0o777), '0o600')

    def test_the_password_from_the_environment_cannot_be_changed_here(self):
        with mock.patch.dict('os.environ', {'TEST_SETTINGS_PASSWORD': 'from the compose file'}):
            fixed = Login(file=self.file, variable='TEST_SETTINGS_PASSWORD', cookie=lock.COOKIE)
        with mock.patch.object(lock, 'login', fixed):
            self.assertTrue(self.call('get', '/api/settings_lock').get_json()['fixed'])
            self.assertEqual(self.call('post', '/api/settings_lock', json={'password': 'from the compose file'}).status_code, 200)
            self.assertEqual(self.call('post', '/api/settings_password', json={'password': ''}).status_code, 400)
            self.assertEqual(self.call('post', '/api/settings_password', json={'password': 'another long one'}).status_code, 400)

    def test_the_two_guarded_events_answer_locked(self):
        self.set_password()
        stranger = socketio.test_client(app, flask_test_client=app.test_client())
        stranger.get_received()
        stranger.emit('save_ai_credential', {'provider': 'gemini', 'value': 'secret-key'})
        stranger.emit('toggle_debug_mode', {'enabled': True})
        errors = [e for e in stranger.get_received() if e['name'] == 'error']
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e['args'][0].get('locked') for e in errors))
        stranger.disconnect()

    def test_an_unlocked_browser_may_use_the_events(self):
        self.set_password()
        client = socketio.test_client(app, flask_test_client=self.client)      # carries this browser's cookie
        client.get_received()
        with mock.patch.object(sys.modules['card_scanner.server.ai_events'], 'save_credential') as save:
            client.emit('save_ai_credential', {'provider': 'gemini', 'value': 'k'})
        save.assert_called_once()
        client.disconnect()


if __name__ == '__main__':
    unittest.main()
