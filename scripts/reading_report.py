#!/usr/bin/env python3
"""
What the reading log says, from data/logs/reading.jsonl:

    python scripts/reading_report.py [--show 30] [--log PATH]

How many captures were read and in which configuration; what settled them (the picture alone, the picture and the strip, the full-card OCR,
the AI, nothing); how often each guard refused the picture's decision; the review's picks (was the picture's best match the one the person
chose?) joined to the guard that sent the card there; seconds per step; and the captures to look at by eye. The work is done by
src/card_scanner/core/reading/report.py; this file only starts it (a file inside a package cannot be run by its path).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core.reading import report  # noqa: E402

if __name__ == "__main__":
    report.main()
