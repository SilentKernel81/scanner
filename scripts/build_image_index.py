#!/usr/bin/env python3
"""
The image index: a 96-byte fingerprint of every printing's picture, in data/image_index/ (~15 MB), for the printing check:

    python scripts/build_image_index.py                       # download what is missing from Scryfall (polite: 10 requests a second)
    python scripts/build_image_index.py --images FOLDER       # take pictures named <Scryfall id>.jpg from a folder first
    python scripts/build_image_index.py --no-fetch --images FOLDER     # ... and download nothing
    python scripts/build_image_index.py --limit 200           # a trial run: only 200 new printings

A first build downloads about 12 GB of pictures (one at a time they are hashed and thrown away) and takes hours; later
runs only do the printings that are new since. It can be stopped and started again. The work is done by
src/card_scanner/core/image_index.py; this file only starts its command line (a file inside a package cannot be run by
its path).
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

if __name__ == "__main__":
    runpy.run_module('card_scanner.core.image_index', run_name='__main__')
