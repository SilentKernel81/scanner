#!/usr/bin/env python3
"""
Starts a camera station:   python station.py --server http://192.168.1.20:5000

(or with nothing after it: the station's setup page, http://<this machine>:8080, asks for the
server. Every option: python station.py --help)

The station itself is the package src/card_scanner/station/ - station_client.py there is the
program, and its first lines say what a station does. This file only starts it, as app.py
starts the server: a file inside a package cannot be run by its path (Python would not find
the package it belongs to), so what is run lies here, in the top folder.

The station restarts itself from its setup page by running this same command line again
(station_client.main: os.execv with sys.argv) - which is this file, so nothing about that
depends on where the code is.
"""
import sys
from pathlib import Path

# The code is in src/, beside this file. Python only puts this file's own folder on its
# path, so src/ is put first here, before anything of the project is imported.
SRC = str(Path(__file__).resolve().parent / 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from card_scanner.station.station_client import main  # noqa: E402 - after the path is set

if __name__ == "__main__":
    main()
