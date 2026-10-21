#!/usr/bin/env python3
"""
The foil network without the OCR: can the set line be cut out at a FIXED place on the flat card, instead of where the OCR found it?

With the OCR switched off nothing finds the set line, so the foil network (core/foil_marker.py) cannot run and the foil / regular question goes to the vision AI (25 s on a cold model). This
measures how fixed that line is and what the network loses when it is cut at a fixed box:
  1. for a sample of the evaluation pictures the real OCR reads the strip once, and the set line's box is written down in the card's own coordinates (fractions of its height / width);
  2. the fixed box is the MEDIAN of those boxes over the TRAINING side of foil_train.py's split (labels are not used for it, only positions);
  3. on the held-out side, where the printing exists in both finishes (the marker is needed), the network answers twice: for the crop at the OCR's own box (what ships) and for the crop at
     the fixed box (and at a wider one), and each answer is compared with the stored finish.
Reads the evaluation data in ~/.cache/mtg-image-match (see common.py); writes data/logs/foil_fixed_boxes.json (the boxes, so the second part can be re-run without the OCR).

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/foil_fixed_crop.py [--per-side 1500] [--reuse]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

import cv2
from card_scanner.core import card_ocr, foil_marker
from common import HOME, OUT, printings, read

ap = argparse.ArgumentParser()
ap.add_argument('--per-side', type=int, default=1500)
ap.add_argument('--reuse', action='store_true')
arguments = ap.parse_args()
BOXES = REPO / 'data' / 'logs' / 'foil_fixed_boxes.json'
P = printings()
Y0, Y1, X0, X1 = foil_marker.STRIP


def card_box(rgb, lines):
    """The set line's box as fractions of the CARD: (y from, y to, x from, x to), or None. (The strip is enlarged for small cards: its pixels are divided by that scale.)"""
    box = foil_marker.set_line_box(lines)
    if box is None:
        return None
    height, width = rgb.shape[:2]
    scale = max(1.0, foil_marker.STRIP_CARD_HEIGHT / height)
    xs, ys = [p[0] / scale for p in box], [p[1] / scale for p in box]
    return [(height * Y0 + min(ys)) / height, (height * Y0 + max(ys)) / height, min(xs) / width, max(xs) / width]


def collect():
    items = [i for i in json.loads((OUT / 'eval.json').read_text()) if i['kind'] in ('collection', 'scanned') and i['finish'] in ('regular', 'foil')]
    rng = np.random.default_rng(5)
    held = [i for i in items if int(hashlib.md5(i['truth'].encode()).hexdigest(), 16) % 5 == 0]
    train = [i for i in items if int(hashlib.md5(i['truth'].encode()).hexdigest(), 16) % 5 != 0]
    chosen = [('held', i) for i in rng.choice(held, min(arguments.per_side, len(held)), replace=False)] \
        + [('train', i) for i in rng.choice(train, min(arguments.per_side, len(train)), replace=False)]
    # the Deadpool rescan (finish known from the physical cards): #1037-1041 regular, 1042-1051 foil
    rescan = {int(f.name.split('_')[1]): f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_')}
    chosen += [('rescan', {'path': str(rescan[n]), 'finish': 'regular' if n < 1042 else 'foil', 'truth': None}) for n in range(1037, 1052)]
    ocr = card_ocr.CardOcr()
    ocr.warm_up()
    rows = []
    for n, (side, item) in enumerate(chosen):
        rgb = read(item['path'])
        if rgb is None:
            continue
        lines = ocr.read_lines(foil_marker.strip_of(rgb)) or []
        line = foil_marker.set_line(lines)
        both = bool(item['truth']) and 'nonfoil' in P[item['truth']]['finishes'] and bool({'foil', 'etched'} & set(P[item['truth']]['finishes']))
        rows.append({'side': side, 'path': str(item['path']), 'finish': item['finish'], 'both': both or side == 'rescan', 'box': card_box(rgb, lines),
                     'symbol': foil_marker.symbol_read(line) if line else 'unknown', 'height': int(rgb.shape[0]), 'width': int(rgb.shape[1])})
        if n % 500 == 0:
            print(n, len(chosen), flush=True)
    ocr.stop()
    BOXES.write_text(json.dumps(rows))
    return rows


def crop_at(rgb, box, margin=foil_marker.MARGIN):
    """The network's input cut at a box given in the card's fractions: through the same strip (enlarged as in training), the same margin of a quarter of the line's height"""
    strip = foil_marker.strip_of(rgb)
    sh, sw = strip.shape[:2]
    y0, y1, x0, x1 = box
    ys, xs = [(y - Y0) / (Y1 - Y0) * sh for y in (y0, y1)], [x / X1 * sw for x in (x0, x1)]
    pad = margin * (ys[1] - ys[0])
    part = strip[max(int(ys[0] - pad), 0):int(ys[1] + pad), max(int(xs[0] - pad), 0):int(xs[1] + pad)]
    if part.size == 0:
        return None
    return cv2.resize(cv2.cvtColor(part, cv2.COLOR_RGB2GRAY), (foil_marker.CROP_W, foil_marker.CROP_H), interpolation=cv2.INTER_AREA)


rows = json.loads(BOXES.read_text()) if arguments.reuse and BOXES.exists() else collect()
found = [r for r in rows if r['box']]
print(f'{len(rows)} pictures; the OCR found a set line on {len(found)} ({100 * len(found) / len(rows):.1f}%)')

# ---- how fixed is the line? (the training side only)
train = np.array([r['box'] for r in found if r['side'] == 'train'])
names = ['y from', 'y to', 'x from', 'x to']
print('\nthe set line in card fractions (training side, n=%d):' % len(train))
for k, name in enumerate(names):
    v = train[:, k]
    print(f'  {name}: median {np.median(v):.4f}  p5 {np.percentile(v, 5):.4f}  p25 {np.percentile(v, 25):.4f}  p75 {np.percentile(v, 75):.4f}  p95 {np.percentile(v, 95):.4f}')
fixed = [float(np.median(train[:, k])) for k in range(4)]
height = fixed[1] - fixed[0]
print(f'  -> the fixed box: y {fixed[0]:.4f}-{fixed[1]:.4f}, x {fixed[2]:.4f}-{fixed[3]:.4f} (the line is {100 * height:.2f}% of the card high)')
centers = (train[:, 0] + train[:, 1]) / 2
print(f'  the line\'s vertical centre varies by +/-{100 * np.std(centers):.2f}% of the card height (std), a line is {100 * height:.2f}% high - '
      f'{100 * np.mean(np.abs(centers - np.median(centers)) < height / 2):.0f}% of the centres lie within half a line of the median')

# ---- the network, at the OCR's box and at fixed boxes
marker = foil_marker.load(True)
wide = [fixed[0] - 0.4 * height, fixed[1] + 0.4 * height, fixed[2] - 0.02, fixed[3] + 0.02]
variants = {'at the OCR box (ships)': None, 'fixed box': fixed, 'fixed box, taller and wider': wide}


def decide(p):
    return 'foil' if p >= foil_marker.SURE else 'non-foil' if p <= 1 - foil_marker.SURE else 'unknown'


def evaluate(selection, title):
    print(f'\n{title}: {len(selection)} cards (the printing exists in both finishes)')
    for name, box in variants.items():
        answered = right = missing = 0
        for r in selection:
            rgb = read(r['path'])
            use = r['box'] if box is None else box
            crop = crop_at(rgb, use) if use else None
            if crop is None:
                missing += 1
                continue
            answer = decide(marker.probability(crop))
            if answer != 'unknown':
                answered += 1
                right += (answer == 'foil') == (r['finish'] != 'regular')
        n = len(selection)
        print(f'  {name:30} answered {answered:4} ({100 * answered / n:5.1f}%), right {right:4} ({100 * right / max(answered, 1):5.1f}% of those), no crop {missing}')


held = [r for r in rows if r['side'] == 'held' and r['both']]
evaluate(held, 'held-out cards, all of them')
evaluate([r for r in held if r['symbol'] == 'unknown'], 'held-out cards whose marker the OCR did NOT read (what the network is for today)')
evaluate([r for r in rows if r['side'] == 'rescan'], 'the 15 Deadpool captures (a phone; confetti foils)')
