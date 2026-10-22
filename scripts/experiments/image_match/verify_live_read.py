#!/usr/bin/env python3
"""
reading.mode 'evidence' on real pictures, end to end as the live path runs it (PrintingCheck.live_read with the live-style OCR reader and the foil network):
how many cards are decided by the picture and the strip, how many of those are the WRONG printing (against the frozen adjudicated truth of the 128 AI-read pictures
and the OCR-confirmed labels of a seeded sample), what is left for the AI, and what it costs in time per card.

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/verify_live_read.py [--easy 300] [--out file.json]
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

import numpy as np
from card_scanner.config import Config
from card_scanner.core import card_ocr, foil_marker
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import OUT, TRUTH, read

ap = argparse.ArgumentParser()
ap.add_argument('--easy', type=int, default=300)
ap.add_argument('--no-image-alone', action='store_true', help='measure without the picture alone: only the decisions the strip confirms (the script by default allows the picture alone, like the shipped setting)')
ap.add_argument('--out', default=str(REPO / 'data' / 'logs' / 'live_read.json'))
arguments = ap.parse_args()

database = CardDatabase()
ocr = card_ocr.CardOcr()
ocr.warm_up()
net = foil_marker.load(True)
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'off', 'off')

rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
hard = [(r, frozen.get(Path(r['path']).name)) for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
easy_rows = [r for r, x in ordered if not x['hard']]
rng = np.random.default_rng(31)
easy = [(easy_rows[i], easy_rows[i]['truth']) for i in rng.choice(len(easy_rows), min(arguments.easy, len(easy_rows)), replace=False)]

report = {}
for name, group in (('hard (the AI read these)', hard), ('easy (OCR confirmed these)', easy)):
    paths, wrong, times, rejected = {'strong': 0, 'image': 0, 'none': 0}, [], [], 0
    for row, truth in group:
        rgb = read(row['path'])
        started = time.perf_counter()
        reading = check.live_read(rgb, None, ocr, net, not arguments.no_image_alone)
        times.append(time.perf_counter() - started)
        if reading is None:
            paths['none'] += 1
            continue
        paths[reading['path']] += 1
        if reading['card']['id'] != truth:
            wrong.append({'picture': Path(row['path']).name, 'chose': reading['card']['id'], 'truth': truth, 'path': reading['path']})
    n = len(group)
    decided = paths['strong'] + paths['image']
    report[name] = {'pictures': n, **paths, 'decided': decided, 'decided_pct': round(100 * decided / n, 1), 'wrong': len(wrong), 'wrong_list': wrong,
                    'ms_median': round(1000 * float(np.median(times)), 1), 'ms_p95': round(1000 * float(np.percentile(times, 95)), 1)}
    r = report[name]
    print(f"{name}: {n} pictures; decided {decided} ({r['decided_pct']}%: strong {paths['strong']}, picture alone {paths['image']}), "
          f"left for the AI {paths['none']}; WRONG {len(wrong)}; per card {r['ms_median']} ms median, {r['ms_p95']} ms p95")
    for w in wrong[:10]:
        print('   wrong:', w)
Path(arguments.out).write_text(json.dumps(report, indent=1))
ocr.stop()
