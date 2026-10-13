# ============================================================================
# FILE: paths.py
# Where the project's two folders are - said once, here, for the server and for a station.
#
#   TOP   the project's top folder: app.py, config.yaml, .env, and everything a person
#         keeps - data/, scanned_cards/, station_captures/. The Docker volumes, the systemd
#         units and every backup name these folders there (/app in the images).
#   SRC   the code: this folder. Also what only the code uses - the server's pages
#         (server/templates, server/static), the OCR reader (ocr/), the station's setup page.
#
# Why a module of its own: the code used to find "the project's folder" from where each file
# happened to lie (Path(__file__).parent in config.py, config_loader.py, server/__init__.py).
# When files move, such a line keeps working and quietly means another folder - a server
# whose data folder is a new, empty one beside the code starts without complaint, on an
# empty collection. So: never build a path from __file__ anywhere else. Take TOP for data
# and settings, SRC for what belongs to the code (tests/test_paths.py checks every one).
#
# This file imports nothing of the project, so anything can import it first - also
# server/__init__.py, which runs before config.py. A station needs it too: it is in the
# COPY lines of the station images (deploy/Dockerfile).
# ============================================================================
from pathlib import Path

SRC = Path(__file__).resolve().parent  # the code: the folder this file is in
TOP = SRC.parent                       # the folder that holds src/, app.py, config.yaml, data/
