#!/usr/bin/env python3
"""
Regression vectors for core/reading/decide.py, captured from the PREVIOUS implementation (PrintingCheck.lookup as committed in a84dcb9) BEFORE it was rebuilt on
`decide`: for real pictures, the evidence that lookup used (the picture's five nearest printings with their set, number, oracle id and finishes; the strip's number
and set) and the answer it gave (path, printing, lead). tests/test_reading_decide.py feeds the evidence to the new `decide_printing` and compares - a file made by
running the new code would only test itself. Ids and similarities only: no picture goes into the repository.

    venv/bin/python -I scripts/experiments/image_match/make_reading_vectors.py       (the repository's venv; real data/image_index, database and OCR reader)
Run on the code of a84dcb9: it refuses to run if printing_check.py differs from that commit (the vectors must come from the old implementation).
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np

REFERENCE = 'a84dcb9'
changed = subprocess.run(['git', 'diff', '--quiet', REFERENCE, '--', 'src/card_scanner/core/printing_check.py'], cwd=REPO).returncode
if changed:
    sys.exit(f'printing_check.py is not the code of {REFERENCE}: the vectors must be captured from the implementation before the refactor')

from card_scanner.config import Config
from card_scanner.core import card_ocr
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck, read_strip
from common import HOME, OUT, read

TARGET = REPO / 'tests' / 'data' / 'reading_regression_cases.json'
database = CardDatabase()
ocr = card_ocr.CardOcr()
ocr.warm_up()
live_strip = {}                                              # the strip of the picture being looked at: read once, used by both the evidence and the old lookup
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'shadow', 'all',
                      strip_reader=lambda rgb: live_strip['value'])


def candidate(index, row, similarity):
    card = database.get_card_by_id(index.ids[int(row)]) or {}
    return {'id': index.ids[int(row)], 'similarity': float(similarity), 'set': card.get('set_code'), 'number': card.get('number'),
            'oracle_id': card.get('oracle_id'), 'finishes': card.get('finishes') or []}


def case(group, n, rgb):
    search = check._search(rgb)
    if search is None:
        return None
    upright = cv2.rotate(rgb, cv2.ROTATE_180) if search['rotated'] else rgb       # as the old lookup turned it before reading the strip
    number, set_code = read_strip(ocr, upright)
    live_strip['value'] = (number, set_code)
    answer = check.lookup(rgb, None, search)                                         # the OLD implementation's answer
    picture = {'rotated': bool(search['rotated']),
               'candidates': [candidate(search['index'], row, sim) for row, sim in zip(search['top'], search['sims'])]}
    return {'name': f'{group}-{n:03d}', 'picture': picture, 'strip': {'number': number, 'set': set_code},
            'expected': {'path': answer['path'], 'printing_id': answer['printing']['id'] if answer['printing'] else None,
                         'similarity': answer['printing']['similarity'] if answer['printing'] else None, 'lead': answer['lead']}}


rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = [r for k in ('collection', 'scanned') for r in rows if r['kind'] == k]
hard = [r['path'] for r, x in zip(ordered, items) if x['hard']]
easy = [r['path'] for r, x in zip(ordered, items) if not x['hard']]
rescan = sorted(str(f) for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_10') and 1037 <= int(f.name.split('_')[1]) <= 1051)
rng = np.random.default_rng(21)
groups = [('rescan', rescan), ('hard', [hard[i] for i in rng.choice(len(hard), 60, replace=False)]),
          ('easy', [easy[i] for i in rng.choice(len(easy), 60, replace=False)])]
cases = []
for group, paths in groups:
    for n, path in enumerate(paths):
        item = case(group, n, read(path))
        if item:
            cases.append(item)
TARGET.write_text(json.dumps({'_note': f'Captured from PrintingCheck.lookup as committed in {REFERENCE}, before it was rebuilt on core/reading/decide.py: the evidence it used and '
                                       'the answer it gave, for real pictures (ids and similarities only). scripts/experiments/image_match/make_reading_vectors.py made it.',
                              'cases': cases}, indent=0))
by_path = {}
for c in cases:
    by_path[c['expected']['path']] = by_path.get(c['expected']['path'], 0) + 1
print(f'{len(cases)} cases written to {TARGET.relative_to(REPO)} ({TARGET.stat().st_size / 1024:.0f} KB); paths: {by_path}')
ocr.stop()
