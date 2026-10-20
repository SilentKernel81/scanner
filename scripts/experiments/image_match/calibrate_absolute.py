#!/usr/bin/env python3
"""
How good must the best match be, in absolute terms, before the picture alone may add a card? (A Codex review of 2026-10-20: the lead over the second printing is
relative - with an index that lacks the card's printing, an unrelated best match can still lead.) For every real picture of the evaluation set the best match's
similarity is measured twice: with the true printing in the index (what a covered card looks like) and with it REMOVED (a printing newer than the index, or a stale
index: what an uncovered card looks like), together with the lead the second case has. The gate is chosen from these two distributions.

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/calibrate_absolute.py
"""
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
from common import OUT, TRUTH, read

index = image_index.ImageIndex.load(Config.DATA_DIR / 'image_index')
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
pictures = [(r['path'], frozen.get(Path(r['path']).name)) for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
rng = np.random.default_rng(31)
easy = [(r['path'], r['truth']) for r, x in ordered if not x['hard']]
pictures += [easy[i] for i in rng.choice(len(easy), 300, replace=False)]

covered, uncovered, lead_uncovered = [], [], []
for path, truth in pictures:
    rgb = read(path)
    up = index.distances(image_index.hash_card(rgb))
    down = index.distances(image_index.hash_card(cv2.rotate(rgb, cv2.ROTATE_180)))
    distances = down if down.min() < up.min() else up
    row = index.row(truth)
    if row is None:
        continue
    sims = image_index.similarity(distances)
    covered.append(float(sims[row]) if int(np.argmax(sims)) == row else float(sims.max()))
    others = np.delete(sims, row)                      # the index without the true printing
    top2 = np.sort(others)[-2:]
    uncovered.append(float(top2[1]))
    lead_uncovered.append(float(top2[1] - top2[0]))

def summary(name, v):
    v = np.array(v)
    print(f"{name}: n={len(v)} min {v.min():.3f}  p1 {np.percentile(v, 1):.3f}  p5 {np.percentile(v, 5):.3f}  median {np.median(v):.3f}  p95 {np.percentile(v, 95):.3f}  max {v.max():.3f}")

summary('best similarity, printing IN the index    ', covered)
summary('best similarity, printing REMOVED         ', uncovered)
summary('lead over the second, printing REMOVED    ', lead_uncovered)
covered, uncovered, lead_uncovered = map(np.array, (covered, uncovered, lead_uncovered))
print('\ngate  covered kept   uncovered rejected   uncovered that would ALSO pass the 0.05 lead and the gate')
for gate in (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9):
    print(f"{gate:.2f}  {100 * np.mean(covered >= gate):5.1f}%       {100 * np.mean(uncovered < gate):5.1f}%                {np.sum((uncovered >= gate) & (lead_uncovered >= 0.05))}")
