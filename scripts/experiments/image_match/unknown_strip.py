#!/usr/bin/env python3
"""
What it costs to refuse a decision by the picture alone when the strip names a printing the card data does NOT have (a Codex review of 2026-10-20: a card newer than the
card data - absent from the data and from the index alike - would otherwise be added as its best match; the strip naming an unknown printing is the sign of it, but an OCR
misread of the collector line looks the same). Every evaluation picture through live_read with the real OCR reader and the picture alone allowed: of the 'image' decisions,
how many have a strip with a number and a set the card data lacks, and were they right or wrong.

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/unknown_strip.py [--easy 1500]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

from card_scanner.config import Config
from card_scanner.core import card_ocr, foil_marker
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import OUT, TRUTH, read

ap = argparse.ArgumentParser()
ap.add_argument('--easy', type=int, default=1500)
arguments = ap.parse_args()
database = CardDatabase()
ocr = card_ocr.CardOcr()
ocr.warm_up()
net = foil_marker.load(True)
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/x.jsonl'), 'off', 'off')
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
rng = np.random.default_rng(17)
easy = [(r['path'], r['truth']) for r, x in ordered if not x['hard']]
pictures = [(r['path'], frozen[Path(r['path']).name]) for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
pictures += [easy[i] for i in rng.choice(len(easy), min(arguments.easy, len(easy)), replace=False)]
image = unknown = []
image, unknown = [], []
for path, truth in pictures:
    reading = check.live_read(read(path), None, ocr, net, True)
    if not reading or reading['path'] != 'image':
        continue
    strip = reading['strip']
    image.append(reading['card']['id'] == truth)
    if strip['number'] and strip['set'] and not database.get_card_by_set_number(strip['set'], strip['number']):
        unknown.append((Path(path).name, strip['set'], strip['number'], reading['card']['name'], reading['card']['id'] == truth))
print(f'{len(pictures)} pictures; decisions by the picture alone: {len(image)} ({sum(image)} matching the stored label)')
print(f'of those, the strip named a printing the card data lacks: {len(unknown)} - the rule would refuse them (they go to OCR / the AI / review)')
for u in unknown:
    print('   ', u)
ocr.stop()
