#!/usr/bin/env python3
"""Exits when run_all.py has finished every stage (so a waiting Claude Code session is woken up)"""
import sys
import time
from pathlib import Path

MARKER = sys.argv[1] if len(sys.argv) > 1 else 'ALL STAGES FINISHED'   # e.g. 'ALL STAGES FINISHED (variants)'

STATUS = Path.home() / '.cache' / 'mtg-image-match' / 'out' / 'run_all.status'

while True:
    try:
        text = STATUS.read_text()
    except OSError:
        text = ''
    if MARKER in text:
        print(text.strip())
        break
    time.sleep(120)
