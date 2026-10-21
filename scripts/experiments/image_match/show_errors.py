#!/usr/bin/env python3
"""
Looks at the pictures behind the no-AI pipeline's mistakes (rule A: strip and image agree on the printing, otherwise
the image alone if it leads by 0.05). For each mistake a row of three pictures: the camera picture, the printing the
labels say it is (TRUTH), and the printing the image match found first. If the first two look different, the label is wrong,
not the pipeline.

    venv/bin/python -I show_errors.py <embedding folder name> [--count 12]
Writes out/<name>/errors_<group>.jpg and prints what each row is.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
from common import IMAGES, OUT, printings, read

LEAD = 0.05
W, H = 220, 308


def rule_a_wrong(x):
    """Is this picture added automatically by rule A, and to the wrong printing?"""
    if x['both']:
        return not x['both_right']
    return x['margin'] >= LEAD and not x['image']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name')
    ap.add_argument('--count', type=int, default=12)
    ap.add_argument('--sheet-dir', default=str(Path(__file__).resolve().parents[3] / 'data' / 'logs' / 'image_match'),
                    help='where the sheets go: inside the project (git-ignored data/logs/) so they can be opened from it')
    a = ap.parse_args()
    sheets = Path(a.sheet_dir)
    sheets.mkdir(parents=True, exist_ok=True)
    P = printings()
    rows = json.loads((OUT / a.name / 'rows.json').read_text())
    items = json.loads((OUT / a.name / 'strip_items.json').read_text())
    ordered = [r for k in ('collection', 'scanned') for r in rows if r['kind'] == k]     # the order strip.py walked them in
    if len(ordered) != len(items):
        raise SystemExit(f'{len(ordered)} rows against {len(items)} strip results: cannot pair them')
    groups = {'collection': [], 'hard': []}
    for r, x in zip(ordered, items):
        if rule_a_wrong(x):
            if x['kind'] == 'collection':
                groups['collection'].append((r, x))
            if x['hard']:
                groups['hard'].append((r, x))
    for group, found in groups.items():
        print(f'\n== {group}: {len(found)} wrong additions; showing {min(a.count, len(found))}')
        picked = found[:a.count]
        sheet = np.zeros((H * len(picked), W * 3, 3), np.uint8)
        for i, (r, x) in enumerate(picked):
            names = []
            for j, path in enumerate((r['path'], IMAGES / f"{r['truth']}.jpg", IMAGES / f"{r['got']}.jpg")):
                rgb = read(path)
                if rgb is not None:
                    sheet[i * H:(i + 1) * H, j * W:(j + 1) * W] = cv2.resize(rgb, (W, H), interpolation=cv2.INTER_AREA)
            t, g = P[r['truth']], P[r['got']]
            print(f"row {i + 1}: truth {t['name']} [{t['set']} {t['number']}]  |  image's first {g['name']} [{g['set']} {g['number']}]  |  class {r['class']}  |  strip read number+set {x['read_number'] and x['read_set']}")
        cv2.imwrite(str(sheets / f'errors_{a.name}_{group}.jpg'), cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])


if __name__ == '__main__':
    main()
