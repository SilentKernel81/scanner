#!/usr/bin/env python3
"""
(Re)downloads the card database from Scryfall:   python scripts/setup_database.py

It asks before replacing an existing database. The work is done by
src/card_scanner/core/setup_database.py; this file only starts it (a file inside a package
cannot be run by its path).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core.setup_database import main  # noqa: E402 - after the path is set

if __name__ == "__main__":
    main()
