#!/usr/bin/env python3
"""
One source tree's PrintingCheck.lookup and .check on a fixed list of real pictures, one JSON line each - so two versions of the code can be compared picture by
picture (compare_lookup_versions.py runs this twice, once on the previous code taken from git, once on the working tree).

    MTG_SCANNER_HOME=<repo> venv/bin/python -I lookup_snapshot.py --src <folder holding src/> --out <file.jsonl> [--easy 300]
--src: the folder whose src/ is put first on Python's path (the repository itself, or `git archive <commit> src config.yaml` extracted somewhere).
The pictures: the 15 Deadpool rescan captures, the AI-read pictures of the experiment and a seeded sample of the OCR-confirmed ones; resolved printing = the label.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument('--src', required=True)
ap.add_argument('--out', required=True)
ap.add_argument('--easy', type=int, default=300)
arguments = ap.parse_args()
sys.path.insert(0, str(Path(arguments.src) / 'src'))      # before anything of the project is imported
sys.path.insert(1, str(HERE))

import cv2
import numpy as np
from card_scanner.config import Config
from card_scanner.core import card_ocr
from card_scanner.core import printing_check as module
from card_scanner.core.database import CardDatabase
from common import HOME, OUT, read

print('source of printing_check:', module.__file__, file=sys.stderr)
database = CardDatabase()
ocr = card_ocr.CardOcr()
ocr.warm_up()
check = module.PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'shadow', 'all',
                             strip_reader=lambda rgb: module.read_strip(ocr, rgb))

rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = [(r, x) for k in ('collection', 'scanned') for r in rows if r['kind'] == k for x in [None]]
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
hard = [r for r, x in ordered if x['hard']]
easy = [r for r, x in ordered if not x['hard']]
rescan = sorted(f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_10') and 1037 <= int(f.name.split('_')[1]) <= 1051)
rng = np.random.default_rng(31)
pictures = [(str(f), None) for f in rescan] + [(r['path'], r['truth']) for r in hard] + \
           [(easy[i]['path'], easy[i]['truth']) for i in rng.choice(len(easy), min(arguments.easy, len(easy)), replace=False)]

with open(arguments.out, 'w') as out:
    for path, label in pictures:
        rgb = read(path)
        search = check._search(rgb)
        record = {'picture': Path(path).name, 'label': label,
                  'lookup': check.lookup(rgb, label, search),
                  'check': check.check(rgb, label, search) if label else None}
        out.write(json.dumps(record, sort_keys=True) + '\n')
print(f'{len(pictures)} pictures written to {arguments.out}', file=sys.stderr)
ocr.stop()
