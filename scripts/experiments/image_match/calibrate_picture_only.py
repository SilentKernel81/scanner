#!/usr/bin/env python3
"""
PICTURE ONLY (OCR switched off: no strip is read, live_read gets no reader): what the picture alone decides on the same pictures as calibrate_strong.py, and how many of those are wrong.
(Copied from calibrate_strong.py, 2026-10-20; its text below is that script's.)
How similar to the picture is the printing a 'strong' decision picks, on real pictures? (A Codex review of 2026-10-20: the similarity floor guarded only the picture-alone path,
so a collector line that names one of the five nearest printings was enough for 'strong' even when every candidate looked unlike the capture - a direct probe gave 'strong' at 0.10.)
Every picture goes through PrintingCheck.live_read as the server runs it (the real OCR reader for the strip and the foil network, the real index), with the picture alone ON so that
both paths show; what is recorded is the path and the similarity of the printing chosen, against the truth. The floor for 'strong' is chosen from this: as high as it can be without
losing correct cards.

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/calibrate_strong.py [--easy 1500]
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
ocr = None
pass
net = foil_marker.load(True)
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'off', 'off')
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
rng = np.random.default_rng(17)
easy = [(r['path'], r['truth']) for r, x in ordered if not x['hard']]
pictures = [(r['path'], frozen[Path(r['path']).name]) for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
pictures += [easy[i] for i in rng.choice(len(easy), min(arguments.easy, len(easy)), replace=False)]
record = {'strong': [], 'image': []}
wrong_cards = []
none = 0
for path, truth in pictures:
    reading = check.live_read(read(path), None, None, net, True)
    if reading is None:
        none += 1
        continue
    record[reading['path']].append((reading['similarity'], reading['card']['id'] == truth))
    if reading['card']['id'] != truth:
        wrong_cards.append((Path(path).name, reading['card']['name'], reading['card']['set_code'], reading['card']['number'], round(reading['similarity'], 3)))
print(f'{len(pictures)} pictures; no decision: {none}')
for name, values in record.items():
    sims = np.array([s for s, _ in values])
    wrong = [s for s, ok in values if not ok]
    if not len(sims):
        continue
    print(f"{name}: {len(sims)} decisions, wrong {len(wrong)}; similarity min {sims.min():.3f} p1 {np.percentile(sims, 1):.3f} p5 {np.percentile(sims, 5):.3f} median {np.median(sims):.3f}")
    for floor in (0.2, 0.25, 0.3, 0.35, 0.4, 0.5):
        below = sims < floor
        print(f"   floor {floor:.2f}: would refuse {int(below.sum()):4} ({100 * below.mean():.2f}%) of these, of which wrong ones: {sum(1 for s, ok in values if s < floor and not ok)}")
Path(REPO / 'data' / 'logs' / 'picture_only.json').write_text(json.dumps(record))
pass

for w in wrong_cards[:12]:
    print('differs from the label:', w)
