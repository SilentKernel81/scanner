#!/usr/bin/env python3
"""
What belongs to the person (data/, scanned_cards/, .env, their config.yaml) apart from what
ships with the program, and the one-copy-per-data-folder lock (paths.py, config_loader.py,
server/startup.py).

Run:  venv/bin/python tests/test_user_dir.py        (or with the others: -m unittest discover tests)
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
SRC = TOP / 'src'


def python(code, **env):
    """Run code in a fresh interpreter with the environment changed (paths.py reads it on import)"""
    run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=TOP,
                         env={**os.environ, 'PYTHONPATH': str(SRC), 'PYTHONDONTWRITEBYTECODE': '1', **env})
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


class UserDir(unittest.TestCase):
    def test_a_checkout_keeps_everything_in_the_top_folder(self):
        out = python("from card_scanner.paths import TOP, USER_DIR; print(TOP == USER_DIR)", APPDIR='')
        self.assertEqual(out, 'True')

    def test_a_chosen_home(self):
        with tempfile.TemporaryDirectory() as home:
            out = python("from card_scanner.paths import USER_DIR; print(USER_DIR)", MTG_SCANNER_HOME=home)
            self.assertEqual(out, str(Path(home).absolute()))
            out = python("from card_scanner.config import Config; print(Config.DATA_DIR, Config.IMAGES_DIR)",
                         MTG_SCANNER_HOME=home)
            self.assertEqual(out, f"{Path(home).absolute() / 'data'} {Path(home).absolute() / 'scanned_cards'}")

    def test_packaged_code_writes_in_the_users_data_home_not_beside_itself(self):
        with tempfile.TemporaryDirectory() as data_home:
            out = python("from card_scanner.paths import USER_DIR; print(USER_DIR)",
                         APPDIR=str(TOP.parent), XDG_DATA_HOME=data_home, MTG_SCANNER_HOME='')
            self.assertEqual(out, str(Path(data_home) / 'mtg-scanner'))

    def test_the_users_config_is_laid_over_the_shipped_one(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'config.yaml').write_text('flask:\n  port: 5999\n')
            out = python("from card_scanner.config import Config; print(Config.PORT, Config.HOST)", MTG_SCANNER_HOME=home)
            self.assertEqual(out.split()[0], '5999')       # theirs
            self.assertNotEqual(out.split()[1], 'None')    # the shipped one's host is still there

    def test_a_program_the_code_brings_is_found_before_the_one_on_the_path(self):
        out = python("from card_scanner.paths import tool; print(tool('sh'), tool('no-such-program-here'))")
        self.assertTrue(out.endswith('None'))
        self.assertTrue(out.split()[0].endswith('/sh'))


class InstanceLock(unittest.TestCase):
    def test_a_second_copy_on_the_same_data_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            code = """
import subprocess, sys
from card_scanner.server import startup
print(startup.lock_instance())
second = subprocess.run([sys.executable, '-c',
    'from card_scanner.server import startup; print(startup.lock_instance())'], capture_output=True, text=True)
print(second.stdout.strip() or second.stderr[-300:])
"""
            self.assertEqual(python(code, MTG_SCANNER_HOME=home).split(), ['True', 'False'])

    def test_the_lock_goes_with_the_process(self):
        with tempfile.TemporaryDirectory() as home:
            one = "from card_scanner.server import startup; print(startup.lock_instance())"
            self.assertEqual(python(one, MTG_SCANNER_HOME=home), 'True')
            self.assertEqual(python(one, MTG_SCANNER_HOME=home), 'True')  # the first has ended


if __name__ == '__main__':
    unittest.main()
