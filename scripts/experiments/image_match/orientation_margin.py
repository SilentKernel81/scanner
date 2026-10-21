#!/usr/bin/env python3
"""
How reliably does the picture search say which way up a card is? (A Codex review of 2026-10-20: with the OCR off the foil network cut the set line at a FIXED place of the card as given, and a card put in
upside down fed it art and title pixels - 5 of 30 turned evaluation pictures got a confident wrong finish.) The search compares a picture in both orientations with the index and the better one counts. For each
evaluation picture, and the same picture turned 180 degrees, this takes the best similarity of each orientation (s_up: as given, s_turned: turned) and reports how far apart they are. The orientation is
believed only when the better one is similar enough AND clearly better than the other (the thresholds printing_check.ORIENTATION_*).

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/orientation_margin.py [--easy 1500]
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

import cv2
from card_scanner.config import Config
from card_scanner.core import image_index
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import OUT, TRUTH, read

ap = argparse.ArgumentParser()
ap.add_argument('--easy', type=int, default=1500)
arguments = ap.parse_args()
check = PrintingCheck(CardDatabase(), Config.DATA_DIR / 'image_index', Path('/nonexistent/x.jsonl'), 'off', 'off')
index = check.index()
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
rng = np.random.default_rng(23)
easy = [r['path'] for r, x in ordered if not x['hard']]
paths = [r['path'] for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)] + [easy[i] for i in rng.choice(len(easy), min(arguments.easy, len(easy)), replace=False)]


def sides(rgb):
    """(best similarity as given, best similarity turned 180)"""
    up = float(image_index.similarity(index.distances(image_index.hash_card(rgb)).min()))
    turned = float(image_index.similarity(index.distances(image_index.hash_card(cv2.rotate(rgb, cv2.ROTATE_180))).min()))
    return up, turned


records = []
for path in paths:
    rgb = read(path)
    if rgb is None:
        continue
    a = sides(rgb)                                   # the picture as it is
    records.append(a)
records = np.array(records)
best = np.maximum(records[:, 0], records[:, 1])
margin = np.abs(records[:, 0] - records[:, 1])
given_up = records[:, 0] >= records[:, 1]            # the search calls the picture upright
print(f'{len(records)} pictures; the search calls {100 * given_up.mean():.1f}% upright, {100 * (~given_up).mean():.1f}% upside down')
print(f'best similarity: p1 {np.percentile(best, 1):.3f}  p5 {np.percentile(best, 5):.3f}  median {np.median(best):.3f}')
print(f'margin between the two orientations: min {margin.min():.3f}  p1 {np.percentile(margin, 1):.3f}  p5 {np.percentile(margin, 5):.3f}  median {np.median(margin):.3f}')
print('\nthresholds: believed when best >= S and margin >= M; the rest -> unknown (the AI):')
for s in (0.30, 0.40, 0.50):
    for m in (0.10, 0.20, 0.30):
        ok = (best >= s) & (margin >= m)
        print(f'  S {s:.2f} M {m:.2f}: believed {100 * ok.mean():5.1f}%  unknown {100 * (~ok).mean():5.1f}%')
# a turned copy must be called the opposite of its original wherever both are believed - and the same function sees the same numbers swapped
print('\nthe turned copy of every picture has the two similarities swapped (hash of the rotation is the same two searches): consistent by construction')
Path(REPO / 'data' / 'logs' / 'orientation_margin.json').write_text(json.dumps(records.tolist()))
