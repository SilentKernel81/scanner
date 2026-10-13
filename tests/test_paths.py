#!/usr/bin/env python3
"""
Where the programs look for their data, their settings, their pages and the OCR reader.

Run:  venv/bin/python tests/test_paths.py        (or with the others: -m unittest discover tests)

Why this exists: the code finds the project's folders from where its own files are
(config.py, config_loader.py, server/__init__.py, station_setup.py). When files move - the
split of app.py into server/, the move into src/ (docs/plans/SRC_MOVE_PLAN.md) - such a line
keeps working and quietly means another folder. The worst case is a server whose data folder
is a new, empty one beside the code: it starts, shows no cards, and whatever is scanned then
lands in the wrong place. Nothing else would notice that, so this test does.

It is written to say the same thing before and after such a move:
  - what a person keeps (the collection, the captures, config.yaml, .env) is in the top
    folder, the one that holds app.py and that the Docker volumes and the systemd units name;
  - what belongs to the code (the pages, their files, the OCR reader, the station's setup
    page) is *there* - asked as "does the file exist where the program will look", not as
    "is it in this folder", because that folder is what a move changes.

Nothing is started and no database is opened: the modules are only imported, as
tests/test_app_surface.py does (importing the server sets up logging, i.e. creates data/logs).
"""
import subprocess
import sys
import unittest
from pathlib import Path

# The top folder: tests/ stays in it whatever happens to the code, so it is found from here
TOP = Path(__file__).resolve().parent.parent
# The code is beside app.py today and in src/ after the move; with this the same file tests both
SRC = TOP / 'src'
sys.path.insert(0, str(SRC if SRC.is_dir() else TOP))


class DataAndSettings(unittest.TestCase):
    """What must stay in the top folder: the volumes, the units and every backup name it there"""

    def test_the_data_folders_are_in_the_top_folder(self):
        from card_scanner.config import Config
        self.assertEqual(Config.DATA_DIR, TOP / 'data')                          # collection, settings, logs
        self.assertEqual(Config.IMAGES_DIR, TOP / 'scanned_cards')               # the server's captures
        self.assertEqual(Config.STATION_CAPTURES_DIR, TOP / 'station_captures')  # a station's unsent ones
        # The card database is a file inside data/ (its name comes from config.yaml)
        self.assertEqual(Config.DATABASE_FILE.parent, TOP / 'data')

    def test_config_yaml_is_read_from_the_top_folder(self):
        # config.py's values all come from this one loader: the file it opened is the question
        from card_scanner.config_loader import get_config_loader
        self.assertEqual(get_config_loader().config_file, TOP / 'config.yaml')

    def test_the_server_takes_env_and_logs_from_the_top_folder(self):
        # server.ROOT is where .env is loaded from and where data/logs is created
        from card_scanner import server
        self.assertEqual(server.ROOT, TOP)


class WhatBelongsToTheCode(unittest.TestCase):
    """Files the programs open by path: each must be where the program looks for it"""

    def test_flask_finds_the_pages_and_their_files(self):
        from card_scanner.server.state import app
        # template_folder is given relative to the app's root_path; static_folder is absolute
        templates = Path(app.root_path) / app.template_folder
        for page in ('stations.html', 'scanner.html', 'collection.html', '_topbar.html'):
            self.assertTrue((templates / page).is_file(), f"{page} is not in {templates}")
        static = Path(app.static_folder)
        for name in ('js/scanner.js', 'js/common.js', 'css/style.css', 'fonts/roboto-latin.woff2'):
            self.assertTrue((static / name).is_file(), f"{name} is not in {static}")
        # The pages load Socket.IO's client from here, never from a CDN (its version is in its name)
        self.assertTrue(list((static / 'js' / 'vendor').glob('socket.io-*.min.js')),
                        f"Socket.IO's client is not in {static / 'js' / 'vendor'}")
        # The address the pages use for these files: unchanged by where they lie on disk
        self.assertEqual(app.static_url_path, '/static')

    def test_the_ocr_reader_is_where_it_is_started(self):
        # card_ocr starts `node server.mjs` with this folder as its working folder. A server that
        # does not find it reports "OCR not installed" and reads every card with the AI instead
        from card_scanner.core import card_ocr
        self.assertTrue((card_ocr.OCR_DIR / 'server.mjs').is_file(), f"server.mjs is not in {card_ocr.OCR_DIR}")
        self.assertTrue((card_ocr.OCR_DIR / 'package.json').is_file(), f"package.json is not in {card_ocr.OCR_DIR}")

    def test_the_station_finds_its_setup_page(self):
        from card_scanner.station import station_setup
        self.assertTrue(station_setup.PAGE_FILE.is_file(), f"{station_setup.PAGE_FILE} does not exist")


class NoCodeLeftInTheTopFolder(unittest.TestCase):
    """The top folder holds two Python files: app.py starts the server, station.py a camera station"""

    @unittest.skipUnless(SRC.is_dir(), "the code is not in src/ yet (docs/plans/SRC_MOVE_PLAN.md)")
    def test_only_the_two_launchers_are_in_the_top_folder(self):
        # Why it matters: `python -c "from config import Config"`, as the scripts run it from the
        # top folder, finds a module lying there before the one in src/ - an old config.py left
        # behind, or a new module put there out of habit, would be the one imported.
        # Asked of Git, so that it is about the project's files and not about what happens to
        # lie in this checkout; without Git (a copy made by rsync) the folder itself is listed.
        try:
            listed = subprocess.run(['git', 'ls-files', '*.py'], cwd=TOP, capture_output=True, text=True, check=True)
            files = [name for name in listed.stdout.split() if '/' not in name]
        except (OSError, subprocess.CalledProcessError):
            files = sorted(path.name for path in TOP.glob('*.py'))
        self.assertEqual(files, ['app.py', 'station.py'])


if __name__ == '__main__':
    unittest.main()
