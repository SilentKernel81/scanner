"""
The support file and what feeds it: core/support.py (what goes in, what is cleaned), card_scanner/diagnostics.py (the build, crash capture, bounded logs), server/logs.py
(route errors reach a file), server/support_api.py. Every secret, address and name here is made up, in a temporary folder: no real data is read.

    venv/bin/python -m unittest discover tests -p test_support.py
"""
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

TOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOP / 'src'))
sys.path.insert(0, str(TOP))

from card_scanner import diagnostics  # noqa: E402
from card_scanner.core import support  # noqa: E402

API_KEY = 'made-up-api-key-0123456789abcdef'
ENV_TOKEN = 'made-up-station-token-987654'
SECRET_KEY = 'made-up-cookie-secret-abcdefgh'
PAIR_KEY = 'MadeUpPairKeyValue_1234567890'
LOGIN_HASH = 'scrypt$made-up-hash-value-000111'
HOME_USER = 'exampleperson'


class Installed(unittest.TestCase):
    """A made-up installation in a temporary folder"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.user_dir = Path(folder.name) / 'install'
        self.data = self.user_dir / 'data'
        self.logs = self.data / 'logs'
        self.logs.mkdir(parents=True)
        (self.user_dir / '.env').write_text(f'GEMINI_API_KEY={API_KEY}\nVISION_AI_PROVIDER=gemini\n# a comment\nEMPTY_API_KEY=\n')
        (self.data / 'secret_key').write_text(SECRET_KEY)
        (self.data / 'settings_login.json').write_text(json.dumps({'hash': LOGIN_HASH}))
        (self.data / 'settings.json').write_text(json.dumps({'reading_mode': 'evidence', 'ai_provider': 'local', 'not_on_the_list': 'private value'}))
        (self.data / 'stations.json').write_text(json.dumps({'desk-cam': {
            'name': 'Desk camera', 'location': 'Private shelf', 'captures': 7, 'last_seen': '2026-11-01',
            'settings': {'focus_value': 410, 'dropper': {'enabled': True, 'transport': 'wifi', 'pair_key': PAIR_KEY,
                                                         'feeder': {'ip': '192.168.1.77', 'mac': 'aa:bb:cc:dd:ee:01'}}}}}))
        patches = [mock.patch.object(support, name, value) for name, value in (
            ('USER_DIR', self.user_dir), ('TOP', self.user_dir), ('DATA_DIR', self.data), ('LOG_DIR', self.logs), ('BUNDLE_DIR', self.data / 'support'))]
        patches += [mock.patch.object(support.socket, 'gethostname', return_value='example-laptop'),
                    mock.patch.object(support.getpass, 'getuser', return_value=HOME_USER),
                    mock.patch.dict(os.environ, {'SCANNER_STATION_TOKEN': ENV_TOKEN})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def bundle(self):
        out = io.BytesIO()
        support.write_bundle(out)
        with zipfile.ZipFile(out) as archive:
            return {name: archive.read(name).decode() for name in archive.namelist()}


class Cleaning(Installed):
    def clean(self, text):
        return support.RedactionMap().clean(text)

    def test_every_secret_is_removed_by_its_value(self):
        for secret in (API_KEY, ENV_TOKEN, SECRET_KEY, PAIR_KEY, LOGIN_HASH):
            cleaned = self.clean(f'an error mentioning {secret} here')
            self.assertNotIn(secret, cleaned)
            self.assertIn('<secret>', cleaned)

    def test_card_names_survive(self):
        for line in ('Standard Bearer', 'Bearer of the Heavens', 'Searching database for: Ring-bearer', "Gimli's Axe #130", 'token: Treasure'):
            self.assertEqual(self.clean(line), line)

    def test_an_authorization_header_goes_but_a_card_called_bearer_stays(self):
        cleaned = self.clean('Authorization: Bearer abc123def456 for Standard Bearer')
        self.assertNotIn('abc123def456', cleaned)
        self.assertIn('Standard Bearer', cleaned)

    def test_shapes_of_keys(self):
        for key in ('sk-abcdefghijklmnopqrstuvwx', 'AIzaSyabcdefghijklmnopqrstuvwxyz'):
            self.assertNotIn(key, self.clean(f'failed with {key}'))

    def test_url_credentials_and_queries(self):
        cleaned = self.clean('HTTPConnectionPool: http://someone:hunter2hunter@ollama.example:11434/api/chat?key=abc&x=1 failed')
        for private in ('someone', 'hunter2hunter', 'key=abc'):
            self.assertNotIn(private, cleaned)
        self.assertIn('ollama.example:11434/api/chat', cleaned)

    def test_addresses_get_stable_aliases(self):
        cleaned = self.clean('from 192.168.1.20 to 10.0.0.5, again 192.168.1.20, public 8.8.8.8')
        self.assertEqual(cleaned, 'from private-ip-1 to private-ip-2, again private-ip-1, public ip-1')

    def test_loopback_and_what_is_no_address_stay(self):
        self.assertEqual(self.clean('127.0.0.1 0.0.0.0 999.1.2.3 v1.2.3 driver 615.78.08'), '127.0.0.1 0.0.0.0 999.1.2.3 v1.2.3 driver 615.78.08')

    def test_mac_email_host_and_user(self):
        cleaned = self.clean(f'aa:bb:cc:dd:ee:01 someone@example.org on example-laptop as {HOME_USER}')
        self.assertEqual(cleaned, 'mac-1 <email> on host-1 as <user>')

    def test_home_folders(self):
        self.assertEqual(self.clean(f'{self.user_dir}/data/logs/app.log'), '<data folder>/data/logs/app.log')
        self.assertEqual(self.clean('File "/home/otherperson/app/x.py", line 3'), 'File "/home/<user>/app/x.py", line 3')
        self.assertEqual(self.clean('/Users/otherperson/x'), '/home/<user>/x')
        self.assertEqual(self.clean(r'C:\Users\otherperson\x'), r'C:\Users\<user>\x')

    def test_whole_paths_only(self):
        with mock.patch.object(support, 'USER_DIR', Path('/app')), mock.patch.object(support.Path, 'home', return_value=Path('/root')):
            redaction = support.RedactionMap()
        self.assertEqual(redaction.clean('/app/data/x /application /usr/app/y /root/z /rootless'), '<data folder>/data/x /application /usr/app/y ~/z /rootless')

    def test_a_user_without_a_home(self):
        with mock.patch.object(support.Path, 'home', side_effect=RuntimeError('no home')):
            self.assertEqual(support.RedactionMap().clean('plain text'), 'plain text')

    def test_the_persons_own_machines_get_an_alias_public_services_stay(self):
        cleaned = self.clean("http://my-server:11434/api and HTTPConnectionPool(host='nas.local', port=80) and https://api.scryfall.com/x and http://localhost:5000/")
        self.assertEqual(cleaned, "http://host-1:11434/api and HTTPConnectionPool(host='host-2', port=80) and https://api.scryfall.com/x and http://localhost:5000/")

    def test_a_four_part_version_in_the_manifest_stays(self):
        with mock.patch.object(support, '_libraries', return_value={'opencv-python': '4.11.0.86'}):
            self.assertEqual(json.loads(self.bundle()['manifest.json'])['libraries'], {'opencv-python': '4.11.0.86'})

    def test_the_capture_key_goes(self):
        self.assertNotIn('c8754c', self.clean('{"capture_key": "c8754c741d", "name": "Forest"}'))

    def test_a_short_value_is_never_taken_for_a_secret(self):
        (self.user_dir / '.env').write_text('SHORT_KEY=abc\n')
        self.assertEqual(self.clean('abc abcdef'), 'abc abcdef')


class Bundle(Installed):
    def log(self, name, text):
        (self.logs / name).write_text(text)

    def test_nothing_secret_anywhere_in_it(self):
        self.log('app.log', f'key {API_KEY}, pair {PAIR_KEY}, at 192.168.1.77 by {HOME_USER} on example-laptop in {self.user_dir}\n')
        everything = '\n'.join(self.bundle().values())
        for private in (API_KEY, ENV_TOKEN, SECRET_KEY, PAIR_KEY, LOGIN_HASH, '192.168.1.77', 'aa:bb:cc:dd:ee:01', HOME_USER, 'example-laptop', str(self.user_dir),
                        'Private shelf', 'private value'):
            self.assertNotIn(private, everything, private)

    def test_settings_and_stations_are_by_name(self):
        files = self.bundle()
        settings = json.loads(files['settings.json'])
        self.assertEqual(settings['settings'], {'reading_mode': 'evidence', 'ai_provider': 'local'})
        self.assertEqual(settings['keys_present'], {'GEMINI_API_KEY': True, 'EMPTY_API_KEY': False})
        self.assertTrue(settings['settings_password_set'])
        station = json.loads(files['stations.json'])['desk-cam']
        self.assertEqual(station['settings'], {'focus_value': 410, 'dropper': {'enabled': True, 'transport': 'wifi'}})

    def test_only_logs_on_the_list(self):
        self.log('app.log', 'kept\n')
        self.log('someones_notes.log', 'not on the list\n')
        self.assertEqual([name for name in self.bundle() if name.startswith('logs/')], ['logs/app.log'])

    def test_the_newest_part_in_whole_lines_also_from_the_rotated_file(self):
        self.log('app.log.1', 'older line\n')
        self.log('app.log', 'newer line\n')
        self.assertEqual(self.bundle()['logs/app.log'], 'older line\nnewer line\n')
        self.log('app.log', 'x' * 50 + '\n' + 'last line\n')
        self.assertEqual(support._tail(self.logs / 'app.log', limit=20), 'last line\n')

    def test_databases_by_count_read_only_and_never_their_rows(self):
        path = self.data / 'scan_inventory.db'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE inventory (name TEXT)')
            connection.execute("INSERT INTO inventory VALUES ('A private card row')")
            connection.execute('CREATE TABLE schema_migrations (name TEXT)')
            connection.execute("INSERT INTO schema_migrations VALUES ('m1')")
        connection.close()
        before = path.read_bytes()
        files = self.bundle()
        info = json.loads(files['databases.json'])['scan_inventory.db']
        self.assertEqual((info['rows']['inventory'], info['migrations']), (1, ['m1']))
        self.assertNotIn('A private card row', '\n'.join(files.values()))
        self.assertEqual(path.read_bytes(), before)

    def test_a_damaged_database_or_settings_file_does_not_stop_it(self):
        (self.data / 'cards_database.db').write_bytes(b'not a database')
        (self.data / 'settings.json').write_text('{broken')
        files = self.bundle()
        self.assertIn('error', json.loads(files['databases.json'])['cards_database.db'])
        self.assertEqual(json.loads(files['settings.json'])['settings'], {})

    def test_a_secret_that_survives_means_no_file(self):
        with mock.patch.object(support, 'collect', return_value=([('logs/app.log', f'leaked {API_KEY}')], [API_KEY])):
            with self.assertRaises(support.SupportError):
                support.write_bundle(io.BytesIO())

    def test_the_command_line_leaves_a_private_file_and_keeps_three(self):
        for second in range(5):
            with mock.patch.object(support, 'bundle_name', return_value=f'card-scanner-support-2026-01-01_00-00-0{second}.zip'), \
                    mock.patch('builtins.print'):
                self.assertEqual(support.command_line(), 0)
        made = sorted(p.name for p in (self.data / 'support').iterdir())
        self.assertEqual(made, [f'card-scanner-support-2026-01-01_00-00-0{n}.zip' for n in (2, 3, 4)])
        self.assertEqual((self.data / 'support' / made[0]).stat().st_mode & 0o777, 0o600)

    def test_a_failed_file_leaves_nothing(self):
        with mock.patch.object(support, 'write_bundle', side_effect=support.SupportError('no')), mock.patch('builtins.print'):
            self.assertEqual(support.command_line(), 1)
        self.assertEqual(list((self.data / 'support').iterdir()), [])


class WithoutTheProgram(unittest.TestCase):
    def test_it_imports_neither_the_config_nor_the_server(self):
        code = ("import sys; sys.path.insert(0, 'src'); import card_scanner.core.support; "
                "print([m for m in sys.modules if m in ('card_scanner.config', 'cv2', 'flask') or m.startswith('card_scanner.server')])")
        done = subprocess.run([sys.executable, '-c', code], cwd=TOP, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), '[]', done.stderr)

    def test_the_launcher_makes_the_file_with_a_broken_config(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'config.yaml').write_text('this: [is not: yaml')
            done = subprocess.run([sys.executable, 'app.py', '--support-bundle'], cwd=TOP, capture_output=True, text=True, timeout=30,
                                  env={**os.environ, 'MTG_SCANNER_HOME': home})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(list((Path(home) / 'data' / 'support').glob('*.zip'))), 1)


class Build(unittest.TestCase):
    def setUp(self):
        diagnostics.build_info.cache_clear()
        self.addCleanup(diagnostics.build_info.cache_clear)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.stamp = Path(folder.name) / 'build_stamp.txt'
        patch = mock.patch.object(diagnostics, 'STAMP_FILE', self.stamp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_filled_stamp(self):
        self.stamp.write_text('a' * 40 + ' ' + 'b' * 40 + ' 2026-11-23T10:00:00-06:00 modified\n')
        info = diagnostics.build_info()
        self.assertEqual((info['commit'], info['tree'], info['modified'], info['source']), ('a' * 40, 'b' * 40, True, 'stamp'))

    def test_an_unfilled_stamp_without_git_is_unknown(self):
        self.stamp.write_text('$Format:%H %T %cI$\n')
        with mock.patch.object(diagnostics, '_from_git', return_value=None):
            self.assertEqual(diagnostics.build_info()['commit'], 'unknown')
            self.assertEqual(diagnostics.describe_build(), 'unknown build')

    def test_git_archive_fills_the_shipped_stamp(self):
        self.assertIn('export-subst', (TOP / '.gitattributes').read_text())
        self.assertIn('$Format:', (TOP / 'src/card_scanner/build_stamp.txt').read_text())

    def test_both_station_images_copy_the_shared_files(self):
        dockerfile = (TOP / 'deploy' / 'Dockerfile').read_text()
        for name in ('src/card_scanner/diagnostics.py', 'src/card_scanner/build_stamp.txt'):
            self.assertEqual(dockerfile.count(name), 2, name)


class Bounded(unittest.TestCase):
    def test_a_long_file_moves_aside_a_short_one_stays(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ocr.log'
            path.write_bytes(b'x' * 10)
            diagnostics.bound_log(path, 20)
            self.assertTrue(path.exists())
            path.write_bytes(b'x' * 30)
            diagnostics.bound_log(path, 20)
            self.assertFalse(path.exists())
            self.assertEqual((Path(folder) / 'ocr.log.1').stat().st_size, 30)
            diagnostics.bound_log(Path(folder) / 'missing.log', 20)          # never raises


class Crashes(unittest.TestCase):
    """In a process of its own: the hooks are for a whole program"""

    def run_program(self, body):
        with tempfile.TemporaryDirectory() as home:
            code = "import sys, threading; sys.path.insert(0, 'src'); from card_scanner import diagnostics; diagnostics.install_crash_capture('test')\n" + body
            subprocess.run([sys.executable, '-c', code], cwd=TOP, capture_output=True, timeout=30, env={**os.environ, 'MTG_SCANNER_HOME': home})
            return (Path(home) / 'data' / 'logs' / 'crash-test.log').read_text()

    def test_a_failure_while_the_server_loads_is_caught_by_the_launcher(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as shadow:
            (Path(shadow) / 'flask.py').write_text("raise ImportError('FORCED_LOAD_FAILURE')\n")
            done = subprocess.run([sys.executable, 'app.py'], cwd=TOP, capture_output=True, text=True, timeout=30,
                                  env={**os.environ, 'MTG_SCANNER_HOME': home, 'PYTHONPATH': shadow})
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('FORCED_LOAD_FAILURE', (Path(home) / 'data' / 'logs' / 'crash-server.log').read_text())

    def test_an_exception_in_the_main_thread(self):
        self.assertIn('ValueError: main broke', self.run_program("raise ValueError('main broke')"))

    def test_an_exception_in_another_thread(self):
        text = self.run_program("t = threading.Thread(target=lambda: 1 / 0, name='worker-x'); t.start(); t.join()")
        self.assertIn('thread worker-x', text)
        self.assertIn('ZeroDivisionError', text)

    def test_a_fatal_signal_would_be_written_there(self):
        # Armed, on this file, and able to write to it - without crashing anything. A real fault here left a core dump and a desktop crash notification at every test run
        text = self.run_program("import faulthandler; assert faulthandler.is_enabled(); faulthandler.dump_traceback(file=diagnostics._captured['test'], all_threads=True)")
        self.assertIn('Current thread', text)

    def test_ctrl_c_is_no_crash(self):
        self.assertNotIn('uncaught', self.run_program('raise KeyboardInterrupt'))


class RouteErrors(unittest.TestCase):
    def test_flask_and_the_others_write_to_a_file(self):
        import app as launcher  # noqa: F401 - sets up the log files
        for name in ('app', 'backups', 'cleanup'):
            files = [h for h in logging.getLogger(name).handlers if isinstance(h, logging.FileHandler)]
            self.assertTrue(files, name)
            self.assertEqual(Path(files[0].baseFilename).name, 'app.log')

    def test_the_root_file_takes_warnings_only(self):
        import app as launcher  # noqa: F401
        files = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler) and h.baseFilename.endswith('errors.log')]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].level, logging.WARNING)


class Routes(Installed):
    def setUp(self):
        super().setUp()
        import app as launcher  # noqa: F401
        from card_scanner.server.state import app
        self.client = app.test_client()

    def test_preview_then_file(self):
        (self.logs / 'app.log').write_text(f'{API_KEY}\n')
        files = self.client.get('/api/support/preview', base_url='http://localhost').get_json()['files']
        self.assertIn({'name': 'logs/app.log', 'bytes': len(API_KEY) + 1}, files)
        answer = self.client.get('/api/support/bundle', base_url='http://localhost')
        self.assertEqual(answer.status_code, 200)
        self.assertIn('card-scanner-support-', answer.headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(answer.data)) as archive:
            self.assertNotIn(API_KEY, archive.read('logs/app.log').decode())
        answer.close()

    def test_a_refused_file_is_never_handed_over(self):
        with mock.patch.object(support, 'write_bundle', side_effect=support.SupportError('a secret')):
            answer = self.client.get('/api/support/bundle', base_url='http://localhost')
        self.assertEqual((answer.status_code, answer.mimetype), (500, 'application/json'))

    def test_both_need_the_password_when_one_is_set(self):
        lock = sys.modules['card_scanner.server.lock']
        with mock.patch.object(lock, 'unlocked', return_value=False):
            for route in ('/api/support/preview', '/api/support/bundle'):
                self.assertEqual(self.client.get(route, base_url='http://localhost').status_code, 401, route)


if __name__ == '__main__':
    unittest.main()
