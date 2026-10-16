"""
scripts/backup.sh makes a backup only its user can read: it holds the API keys (.env,
data/api_keys.env, data/secret_key). Run in a copy of a made-up project in a temporary folder -
never the real data.

    venv/bin/python -m unittest discover tests -p test_backup_script.py
"""
import http.server
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'backup.sh'


@unittest.skipUnless(shutil.which('bash') and shutil.which('tar') and shutil.which('curl'), 'needs bash, tar and curl')
class BackupIsPrivate(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.project, self.home = self.folder / 'project', self.folder / 'home'
        (self.project / 'scripts').mkdir(parents=True)
        (self.project / 'data').mkdir()
        self.home.mkdir()
        shutil.copy(SCRIPT, self.project / 'scripts' / 'backup.sh')
        (self.project / '.env').write_text('GEMINI_API_KEY=secret\n')
        (self.project / 'data' / 'api_keys.env').write_text('OPENAI_API_KEY=secret\n')
        (self.project / 'data' / 'secret_key').write_text('abc')

    def run_script(self):
        environment = {**os.environ, 'HOME': str(self.home), 'SCANNER_URL': 'http://127.0.0.1:1'}   # no server: files are snapshot on their own
        return subprocess.run(['bash', str(self.project / 'scripts' / 'backup.sh')], env=environment,
                              capture_output=True, text=True, cwd=str(self.project), umask=0o022)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_the_folder_and_the_archive_are_the_owners_alone(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        backups = self.home / 'scanner-backups'
        archives = list(backups.glob('scanner-backup-*.tar.gz'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(self.mode(backups), 0o700)
        self.assertEqual(self.mode(archives[0]), 0o600)

    def test_an_older_backup_in_the_folder_is_tightened_too(self):
        backups = self.home / 'scanner-backups'
        backups.mkdir()
        old = backups / 'scanner-backup-20200101_000000.tar.gz'
        old.write_bytes(b'x')
        old.chmod(0o644)
        backups.chmod(0o755)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.mode(old), 0o600)
        self.assertEqual(self.mode(backups), 0o700)


class FakeServer:
    """A stand-in for a server with a settings password: /api/settings_lock gives the cookie,
    /api/backups/full needs it (else 401 locked) and answers with a snapshot's id"""
    PASSWORD = 'correct horse'

    def __init__(self):
        outer = self
        self.full_asked_with_cookie = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, body, cookie=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                if cookie:
                    self.send_header('Set-Cookie', cookie)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}') if length else {}
                if self.path == '/api/settings_lock':
                    if body.get('password') == outer.PASSWORD:
                        return self.reply(200, {'success': True}, 'settings_session=abc; Path=/')
                    return self.reply(403, {'success': False})
                if self.path == '/api/backups/full':
                    has_cookie = 'settings_session=abc' in (self.headers.get('Cookie') or '')
                    outer.full_asked_with_cookie.append(has_cookie)
                    if not has_cookie:
                        return self.reply(401, {'success': False, 'locked': True})
                    return self.reply(200, {'success': True, 'backup': {'id': 'SNAP', 'missing': []}})
                self.reply(404, {})

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(shutil.which('bash') and shutil.which('tar') and shutil.which('curl'), 'needs bash, tar and curl')
class BackupWithASettingsPassword(BackupIsPrivate):
    """A server with a settings password still gets a coordinated snapshot, when the script knows it"""

    def setUp(self):
        super().setUp()
        snapshot = self.project / 'data' / 'backups' / 'full' / 'SNAP'     # what the server's full backup wrote
        snapshot.mkdir(parents=True)
        (snapshot / 'inventory.db').write_bytes(b'x')
        (snapshot / 'manifest.json').write_text('{}')
        self.server = FakeServer()
        self.addCleanup(self.server.close)

    def run_with(self, **extra):
        environment = {**os.environ, 'HOME': str(self.home), 'SCANNER_URL': self.server.url, **extra}
        environment.pop('SCANNER_SETTINGS_PASSWORD', None) if 'SCANNER_SETTINGS_PASSWORD' not in extra else None
        return subprocess.run(['bash', str(self.project / 'scripts' / 'backup.sh')], env=environment,
                              capture_output=True, text=True, cwd=str(self.project))

    def test_the_password_from_the_environment_is_used(self):
        done = self.run_with(SCANNER_SETTINGS_PASSWORD=FakeServer.PASSWORD)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('took a coordinated snapshot (SNAP)', done.stdout)
        self.assertEqual(self.server.full_asked_with_cookie, [True])

    def test_the_password_in_dot_env_is_used_too(self):
        (self.project / '.env').write_text(f'GEMINI_API_KEY=secret\nSCANNER_SETTINGS_PASSWORD={FakeServer.PASSWORD}\n')
        done = self.run_with()
        self.assertIn('took a coordinated snapshot (SNAP)', done.stdout)

    def test_without_the_password_the_script_says_what_is_missing(self):
        done = self.run_with()
        self.assertEqual(done.returncode, 0, done.stderr)               # the backup is still made, file by file
        self.assertIn('settings password', done.stdout)
        self.assertNotIn('took a coordinated snapshot', done.stdout)

    def test_a_wrong_password_is_said(self):
        done = self.run_with(SCANNER_SETTINGS_PASSWORD='wrong')
        self.assertIn('was not accepted', done.stdout)
        self.assertNotIn('took a coordinated snapshot', done.stdout)


if __name__ == '__main__':
    unittest.main()
