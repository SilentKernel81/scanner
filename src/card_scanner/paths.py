# ============================================================================
# FILE: paths.py
# Where the project's two folders are - said once, here, for the server and for a station.
#
#   TOP   the project's top folder: app.py, config.yaml, .env, and everything a person
#         keeps - data/, scanned_cards/, station_captures/. The Docker volumes, the systemd
#         units and every backup name these folders there (/app in the images).
#   USER_DIR  what belongs to the person who runs it: data/, scanned_cards/, station_captures/,
#         .env and an optional config.yaml laid over the shipped one. TOP in a checkout and in
#         the Docker images; the folder ~/.local/share/mtg-scanner when the program is
#         packaged (an AppImage cannot write beside its code), MTG_SCANNER_HOME chooses it.
#   CODE  the code: this folder, the package card_scanner. Also what only the code uses -
#         the server's pages (server/templates, server/static), the station's setup page.
#         (The OCR reader is not Python and lies beside the package: TOP/src/ocr.)
#
# Why a module of its own: the code used to find "the project's folder" from where each file
# happened to lie (Path(__file__).parent in config.py, config_loader.py, server/__init__.py).
# When files move, such a line keeps working and quietly means another folder - a server
# whose data folder is a new, empty one beside the code starts without complaint, on an
# empty collection. So: never build a path from __file__ anywhere else. Take TOP for data
# and settings, CODE for what belongs to the code (tests/test_paths.py checks every one).
#
# This file imports nothing of the project, so anything can import it first - also
# server/__init__.py, which runs before config.py. A station needs it too: it is in the
# COPY lines of the station images (deploy/Dockerfile).
# ============================================================================
import os
import shutil
import sys
from pathlib import Path

APP_NAME = 'mtg-scanner'
CODE = Path(__file__).resolve().parent  # src/card_scanner: the package, the folder this file is in
TOP = CODE.parent.parent                # the folder that holds src/, app.py, config.yaml, data/


def _packaged():
    """Whether the code runs from inside an AppImage or a PyInstaller binary"""
    if getattr(sys, 'frozen', False):
        return True
    # Not just "APPDIR is set": a terminal or editor that is itself an AppImage passes its
    # variables on to a checkout started from it
    app_dir = os.environ.get('APPDIR')
    return bool(app_dir) and Path(app_dir).resolve() in TOP.resolve().parents


def _user_dir():
    override = os.environ.get('MTG_SCANNER_HOME')
    if override:
        return Path(override).expanduser().absolute()
    if not _packaged():
        return TOP
    data_home = os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share'
    return Path(data_home) / APP_NAME


USER_DIR = _user_dir()


def tool(name):
    """
    Path of a program the code starts (node, v4l2-ctl): the copy shipped in bin/ beside the
    code (the AppImage brings its own Node.js), else the one on PATH; None when there is none
    """
    bundled = TOP / 'bin' / name
    return str(bundled) if bundled.is_file() else shutil.which(name)
