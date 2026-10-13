#!/usr/bin/env python3
"""
Starts the scanner server:   python app.py

The server itself is the package src/card_scanner/server/ - its __init__.py says what is where. This file
stays because everything that starts the server runs exactly `python app.py` (the Docker
image, scripts/start.sh, the systemd unit), and so that `import app` still gives the Flask
app and Socket.IO (tests/test_app_surface.py).

It and station.py (a camera station) are the only Python files in the project's top folder
(tests/test_paths.py keeps it so): all the code is the package card_scanner, in src/.
"""
import sys
from pathlib import Path

# The code is in src/, beside this file. Python only puts this file's own folder on its
# path, so src/ is put first here, before anything of the project is imported.
SRC = str(Path(__file__).resolve().parent / 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from card_scanner.server.startup import main  # noqa: E402 - after the path is set
from card_scanner.server.state import app, socketio  # noqa: E402,F401 - for whoever imports this module

if __name__ == "__main__":
    main()
