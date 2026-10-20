#!/usr/bin/env python3
"""
Evidence mode and a card the full-card OCR CONFIRMED: when the picture clearly looks like another printing than the one the OCR read ('disagree' - a Secret Lair card prints
the set and number of the card it is made from), the OCR's read should not stand alone. How often does that fire on cards whose printing the OCR got right, and what does the check
cost? Over every OCR-confirmed picture of the evaluation set (the labels ARE what the OCR read, so a real OCR misread shows as a disagree too: a sample is looked at by eye).

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/measure_contradiction.py [--sheet N]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

from card_scanner.config import Config
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import OUT, read

ap = argparse.ArgumentParser()
ap.add_argument('--sheet', type=int, default=25, help='how many disagreements to list for a look by eye')
arguments = ap.parse_args()

database = CardDatabase()
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'off', 'off')
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
easy = [r for r, x in ordered if not x['hard']]
print(f'{len(easy)} OCR-confirmed pictures')
counts, times, listed = {}, [], []
for n, row in enumerate(easy, 1):
    rgb = read(row['path'])
    started = time.perf_counter()
    search = check._search(rgb)
    times.append(time.perf_counter() - started)
    verdict = 'no index' if search is None else check.check(rgb, row['truth'], search)['verdict']
    counts[verdict] = counts.get(verdict, 0) + 1
    if verdict == 'disagree':
        result = check.check(rgb, row['truth'], search)
        read_card = database.get_card_by_id(row['truth']) or {}
        best = database.get_card_by_id(result['best']['id']) or {}
        listed.append({'picture': Path(row['path']).name, 'ocr_read': f"{read_card.get('name')} [{read_card.get('set_code')} {read_card.get('number')}]",
                       'picture_says': f"{best.get('name')} [{best.get('set_code')} {best.get('number')}]", 'gap': result['gap'], 'kind': result['kind']})
total = sum(counts.values())
print({k: f'{v} ({100 * v / total:.2f}%)' for k, v in sorted(counts.items())})
print(f"the search alone (two fingerprints + distances over the index): median {1000 * np.median(times):.1f} ms, p95 {1000 * np.percentile(times, 95):.1f} ms")
kinds = {}
for item in listed:
    kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
print('disagreements by kind (same_card = another printing of the card the OCR read; other_card = a different card):', kinds)
for item in listed[:arguments.sheet]:
    print('  ', item)
Path(REPO / 'data' / 'logs' / 'contradiction.json').write_text(json.dumps(listed, indent=1))
