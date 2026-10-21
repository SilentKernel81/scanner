#!/usr/bin/env python3
"""
Runs ON THE SERVER (sent through ssh, read-only): the most recent settled captures, so a rescan can be found and labelled by order.

    ssh user@192.168.1.23 python3 - 60 < scripts/experiments/image_match/server_recent.py
Prints one line per capture, oldest first: settled time, sender, station number, capture id, what the server decided, which reader.
"""
import json
import sqlite3
import sys
from pathlib import Path

count = int(sys.argv[1]) if len(sys.argv) > 1 else 60
db = sqlite3.connect(f"file:{Path.home() / 'scanner-server' / 'data' / 'pending_captures.db'}?mode=ro", uri=True)
rows = db.execute('select settled_at, sender, number, capture_id, outcome from settled_captures order by settled_at desc limit ?', (count,)).fetchall()
for settled_at, sender, number, capture_id, outcome in reversed(rows):
    o = json.loads(outcome)
    card = o.get('card') or {}
    read = o.get('read') or {}
    print(f"{settled_at} {sender:>14} #{number:<5} {capture_id[:8]}  {o.get('status'):7} {card.get('name', '-')[:26]:26} {card.get('set', '-')[:22]:22} "
          f"{card.get('number', '-'):>9} {card.get('finish', '-'):8} read by {read.get('reader', '-')}")
