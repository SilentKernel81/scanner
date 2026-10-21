#!/usr/bin/env python3
"""
Does a printing survive the trip the evidence-mode reading sends it on? The reading hands the server the database's own name, collector number and set code of the
printing it decided on; the server then looks the card up again by exactly those (Game.identify) and adds it when the match is confirmed. If that lookup can return
ANOTHER printing (or an unconfirmed one) for some printing, the reading would add the wrong card - or send a right one to review - without the decision being at fault.

Every printing of the card database (or --limit N of them, seeded) is put through it. Read-only on the database.
    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/verify_roundtrip.py [--limit N] [--out file.json]
"""
import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))

from card_scanner import games
from card_scanner.config import Config
from card_scanner.core.database import CardDatabase

ap = argparse.ArgumentParser()
ap.add_argument('--limit', type=int, default=0)
ap.add_argument('--out', default=str(REPO / 'data' / 'logs' / 'roundtrip.json'))
arguments = ap.parse_args()


class Settings:                      # games.init asks only for the saved choice of game
    def get(self, key, default=None):
        return default


database = CardDatabase()
games.init(database, Settings())
game = games.active()
rows = sqlite3.connect(f'file:{Config.DATABASE_FILE}?mode=ro', uri=True).execute('select id from cards').fetchall()
ids = [r[0] for r in rows]
if arguments.limit:
    ids = random.Random(5).sample(ids, min(arguments.limit, len(ids)))
failures, started = [], time.time()
for n, printing_id in enumerate(ids, 1):
    card = database.get_card_by_id(printing_id)
    found = game.identify(card['name'], card['number'], card['set_code'], ai_model='roundtrip')
    ok = bool(found) and found.get('id') == printing_id and game.is_confirmed(found)
    if not ok:
        failures.append({'id': printing_id, 'name': card['name'], 'set': card['set_code'], 'number': card['number'],
                         'found': found.get('id') if found else None, 'match': found.get('match') if found else None})
    if n % 5000 == 0:
        print(f'{n}/{len(ids)} {len(failures)} failures {time.time() - started:.0f}s', file=sys.stderr)
Path(arguments.out).write_text(json.dumps({'checked': len(ids), 'failures': failures}, indent=1))
print(f'{len(ids)} printings in {time.time() - started:.0f} s; {len(failures)} do not come back as themselves, confirmed')
