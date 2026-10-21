#!/usr/bin/env python3
"""
What the printing check (shadow mode) has found, from data/logs/printing_check.jsonl:

    python scripts/printing_check_report.py [--show 30]

How many cards it looked at, how many looked like the printing the server added, and the latest ones that did not (the card that
was added, what the picture looks like). The work is done by src/card_scanner/core/printing_check.py; this file only starts its
command line (a file inside a package cannot be run by its path).
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

if __name__ == "__main__":
    runpy.run_module('card_scanner.core.printing_check', run_name='__main__')
