#!/usr/bin/env python3
"""
Capture pictures the server has kept:   python scripts/cleanup.py --stats
                                        python scripts/cleanup.py --days 7 [--dry-run] | --all

The work is done by src/card_scanner/core/cleanup.py, which the server also uses for its own
clean-up in the background; this file only starts its command line (a file inside a package
cannot be run by its path).
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

if __name__ == "__main__":
    # The module's command line is the part under its `if __name__ == "__main__"`: run it as
    # that. (Nothing else has imported the module in this process, so it is not loaded twice.)
    runpy.run_module('card_scanner.core.cleanup', run_name='__main__')
