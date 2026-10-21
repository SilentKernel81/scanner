#!/usr/bin/env python3
"""
The contradiction rule measured the way it runs: for every OCR-confirmed picture of the evaluation set, TODAY's OCR read, TODAY's card lookup (Game.confirmed_card), and the picture's
verdict on that card (PrintingCheck.check). The stored labels are what an older run read and are not the printing the live pipeline resolves now (13 of the 82 'disagreements' of
measure_contradiction.py were old digit misreads the current lookup gets right). Counts by (verdict, kind of the picture's alternative, how the OCR's printing was matched,
whether it is the stored label). Writes data/logs/contradiction_live.json, resumable (--resume).

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/measure_contradiction_live.py [--limit N] [--resume]
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

from card_scanner import games
from card_scanner.config import Config
from card_scanner.core import card_ocr
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import OUT, read

ap = argparse.ArgumentParser()
ap.add_argument('--limit', type=int, default=0)
ap.add_argument('--resume', action='store_true')
arguments = ap.parse_args()
target = REPO / 'data' / 'logs' / 'contradiction_live.json'


class Settings:
    def get(self, key, default=None):
        return default


database = CardDatabase()
games.init(database, Settings())
game = games.active()
ocr = card_ocr.CardOcr()
ocr.warm_up()
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'off', 'off')
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
easy = [r for r, x in zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items) if not x['hard']]
if arguments.limit:
    easy = easy[:arguments.limit]
done = json.loads(target.read_text()) if arguments.resume and target.exists() else {}
started = time.time()
for n, row in enumerate(easy, 1):
    name = Path(row['path']).name
    if name in done:
        continue
    rgb = read(row['path'])
    info = ocr.read_card(rgb, 'mtg')
    card = game.confirmed_card(info['name'], info['collector_number'], info['set_code']) if info else None
    if not card:
        done[name] = {'confirmed': False}
    else:
        result = check.check(rgb, card['id'])
        done[name] = {'confirmed': True, 'tag': card['match'], 'is_label': card['id'] == row['truth'], 'verdict': result['verdict'] if result else None,
                      'kind': result['kind'] if result else None, 'gap': result['gap'] if result else None, 'ocr_name': info['name'], 'ocr_set': info['set_code'],
                      'card': f"{card['name']} [{card['set_code']} {card['number']}]", 'best': result['best']['id'] if result else None}
    if n % 500 == 0:
        target.write_text(json.dumps(done))
        print(f'{n}/{len(easy)} {time.time() - started:.0f}s', file=sys.stderr, flush=True)
target.write_text(json.dumps(done))
confirmed = [v for v in done.values() if v['confirmed']]
print(f'{len(done)} pictures, {len(confirmed)} confirmed by the live OCR + lookup')
counts = {}
for v in confirmed:
    key = (v['verdict'], v['kind'], v['tag'], 'label' if v['is_label'] else 'NOT the label')
    counts[key] = counts.get(key, 0) + 1
for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(f'{n:6}  {key}')
ocr.stop()
