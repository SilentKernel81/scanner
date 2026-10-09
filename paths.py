#!/usr/bin/env python3
"""
Where the program's files live

BASE_DIR holds what ships with the program (code, templates/, static/, ocr/, the default
config.yaml) and may be read-only. USER_DIR holds what belongs to the user: data/,
scanned_cards/, .env and an optional config.yaml with their own settings.

Run from a checkout, both are the same folder. A packaged program (an AppImage, a PyInstaller
binary) cannot write beside its code, so USER_DIR is ~/.local/share/mtg-scanner there
($XDG_DATA_HOME). MTG_SCANNER_HOME picks the folder in either case.

tool() finds the programs the app starts, which a packaged program may bring along.

Imported before anything else (app.py needs it to find .env) - keep it free of other imports
from this program.
"""

import os
import shutil
import sys
from pathlib import Path

APP_NAME = 'mtg-scanner'
BASE_DIR = Path(__file__).parent


def _packaged():
    """Whether the code runs from inside an AppImage or a PyInstaller binary"""
    if getattr(sys, 'frozen', False):
        return True
    # Not just "APPDIR is set": a terminal or editor that is itself an AppImage passes its
    # variables on to a checkout started from it
    app_dir = os.environ.get('APPDIR')
    return bool(app_dir) and Path(app_dir).resolve() in BASE_DIR.resolve().parents


def _user_dir():
    override = os.environ.get('MTG_SCANNER_HOME')
    if override:
        return Path(override).expanduser().absolute()
    if not _packaged():
        return BASE_DIR
    data_home = os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share'
    return Path(data_home) / APP_NAME


USER_DIR = _user_dir()


def tool(name):
    """
    Path of a program the app starts (node, v4l2-ctl): the copy shipped in bin/ beside the
    code (the AppImage brings its own Node.js), else the one on PATH; None when there is none
    """
    bundled = BASE_DIR / 'bin' / name
    return str(bundled) if bundled.is_file() else shutil.which(name)
