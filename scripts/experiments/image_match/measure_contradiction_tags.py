#!/usr/bin/env python3
"""
For the OCR-confirmed pictures where the picture clearly looks like another printing (measure_contradiction.py's list), how was the printing the OCR read MATCHED - by set +
number (the printed line, name checked), or by name + number (a fuzzier name, no set read)? A garbled name that fuzzily matched another card (the Deadpool captures: 'Lightning' #042,
'under DEADLY DiSPUTe 1' -> Costly Plunder) is a weaker confirmation than set + number, and a foil's glare on a correctly read card is not.
Reads the 82 pictures again with the real OCR reader (the OCR's own read, with its set code, is not kept in the evaluation rows) and the real card database.

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/measure_contradiction_tags.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

from card_scanner import games
from card_scanner.core import card_ocr
from card_scanner.core.database import CardDatabase
from common import OUT, read


class Settings:
    def get(self, key, default=None):
        return default


database = CardDatabase()
games.init(database, Settings())
game = games.active()
ocr = card_ocr.CardOcr()
ocr.warm_up()
paths = {Path(r['path']).name: r['path'] for r in json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())}
listed = json.loads((REPO / 'data' / 'logs' / 'contradiction.json').read_text())
counts = {}
detail = []
for item in listed:
    info = ocr.read_card(read(paths[item['picture']]), 'mtg')
    card = game.confirmed_card(info['name'], info['collector_number'], info['set_code']) if info else None
    tag = card['match'] if card else 'not confirmed (the label is the read of an earlier run)'
    key = (item['kind'], tag)
    counts[key] = counts.get(key, 0) + 1
    detail.append({**item, 'ocr_name': (info or {}).get('name'), 'ocr_set': (info or {}).get('set_code'), 'tag': tag})
for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(f'{n:3}  {key[0]:10} {key[1]}')
print()
for d in detail:
    if d['tag'] != 'set_number':
        print(' ', d['kind'], d['tag'], '| OCR read:', d['ocr_name'], d['ocr_set'], '| label:', d['ocr_read'], '| picture:', d['picture_says'])
Path(REPO / 'data' / 'logs' / 'contradiction_tags.json').write_text(json.dumps(detail, indent=1))
ocr.stop()
