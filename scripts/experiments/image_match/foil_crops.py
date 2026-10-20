#!/usr/bin/env python3
"""
Step for the foil question: cut out the SET LINE of every picture ("MSH ★ EN" - the star / dot between the set code and the
language is the foil marker) so a small classifier can learn to tell foil from non-foil by it. Runs in the repository's venv
(it needs the project's OCR reader to find the line).

    venv/bin/python -I foil_crops.py
Writes out/foil_crops.npz: crops (n, 32, 128) uint8 grey, label (1 foil / 0 regular), the truth printing id, kind,
what the OCR itself read of the marker, and whether the printing exists in both finishes (only then is the marker needed).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
from card_scanner.core import card_ocr
from common import OUT, printings, read
from strip import crop, parse_strip

H, W = 32, 128


def set_line_box(lines):
    """The box of the line parse_strip takes the set code from (the second matching line), or None"""
    number_seen = False
    for line in sorted(lines, key=lambda l: l['box'][0][1]):
        text = line['text'].strip()
        if not number_seen:
            m = card_ocr.MAGIC_NUMBER.match(text)
            digits = m and (m.group(1) or m.group(2))
            if (digits and sum(c.isdigit() for c in digits) >= 2) or card_ocr.MAGIC_NUMBER_OF_TOTAL.match(text):
                number_seen = True
                continue
        if card_ocr.MAGIC_SET.match(text):
            return line['box']
    return None


def main():
    P = printings()
    items = [i for i in json.loads((OUT / 'eval.json').read_text()) if i['kind'] in ('collection', 'scanned') and i['finish'] in ('regular', 'foil')]
    ocr = card_ocr.CardOcr()
    ocr.warm_up()
    crops, label, truth, kind, read_marker, both, t0 = [], [], [], [], [], [], time.time()
    for n, it in enumerate(items):
        rgb = read(it['path'])
        if rgb is None:
            continue
        strip = crop(rgb)
        lines = ocr.read_lines(strip) or []
        box = set_line_box(lines)
        if box is None:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        pad = 0.25 * (max(ys) - min(ys))
        part = strip[max(int(min(ys) - pad), 0):int(max(ys) + pad), max(int(min(xs) - pad), 0):int(max(xs) + pad)]
        if part.size == 0:
            continue
        grey = cv2.cvtColor(part, cv2.COLOR_RGB2GRAY)
        crops.append(cv2.resize(grey, (W, H), interpolation=cv2.INTER_AREA))
        label.append(1 if it['finish'] == 'foil' else 0)
        truth.append(it['truth'])
        kind.append(it['kind'])
        read_marker.append(parse_strip(lines, rgb.shape[0])[2])
        f = P[it['truth']]['finishes']
        both.append('nonfoil' in f and bool({'foil', 'etched'} & set(f)))
        if n % 500 == 0:
            print(n, len(items), f'{time.time() - t0:.0f}s', flush=True)
    np.savez(OUT / 'foil_crops.npz', crops=np.array(crops), label=np.array(label), truth=np.array(truth), kind=np.array(kind),
             read_marker=np.array(read_marker), both=np.array(both))
    print('saved', len(crops), 'crops of', len(items))
    ocr.stop()


if __name__ == '__main__':
    main()
