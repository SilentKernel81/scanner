#!/usr/bin/env python3
"""
Starts the scanner server:   python app.py

The server itself is the package server/ - its __init__.py says what is where. This file
stays because everything that starts the server runs exactly `python app.py` (the Docker
image, scripts/start.sh, the systemd unit), and so that `import app` still gives the Flask
app and Socket.IO (tests/test_app_surface.py).
"""
from server.startup import main
from server.state import app, socketio  # noqa: F401 - for whoever imports this module

if __name__ == "__main__":
    main()
