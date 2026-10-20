#!/usr/bin/env python3
"""
Contact sheets of every picture the AI read today ('hard cases') where the image's best match differs from the stored label, to be
judged by eye: columns = the camera picture | the LABEL's printing | the IMAGE's first printing. The verdicts go into
truth/hard_relabel.json (written by hand after looking): {"<picture file name>": "image" | "label" | "unsure"}.

    venv/bin/python -I hard_sheets.py <embedding folder name, e.g. phash_rgb_card>
Writes data/logs/image_match/hard_<n>.jpg (inside the project, git-ignored) and prints a numbered index.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
from common import IMAGES, OUT, printings, read

W, H, PER_SHEET = 200, 280, 8
SHEETS = Path(__file__).resolve().parents[3] / 'data' / 'logs' / 'image_match'

name = sys.argv[1]
P = printings()
rows = json.loads((OUT / name / 'rows.json').read_text())
items = json.loads((OUT / name / 'strip_items.json').read_text())
ordered = [r for k in ('collection', 'scanned') for r in rows if r['kind'] == k]      # the order strip.py walked them in
assert len(ordered) == len(items), (len(ordered), len(items))
todo = [r for r, x in zip(ordered, items) if x['hard'] and r['class'] != 'correct']
print(f'{len(todo)} of {sum(x["hard"] for x in items)} hard cases have an image answer that differs from the label')
SHEETS.mkdir(parents=True, exist_ok=True)
for s in range(0, len(todo), PER_SHEET):
    group = todo[s:s + PER_SHEET]
    sheet = np.zeros((H * len(group), W * 3, 3), np.uint8)
    for i, r in enumerate(group):
        for j, path in enumerate((r['path'], IMAGES / f"{r['truth']}.jpg", IMAGES / f"{r['got']}.jpg")):
            rgb = read(path)
            if rgb is not None:
                sheet[i * H:(i + 1) * H, j * W:(j + 1) * W] = cv2.resize(rgb, (W, H), interpolation=cv2.INTER_AREA)
        t, g = P[r['truth']], P[r['got']]
        print(f"sheet {s // PER_SHEET + 1} row {i + 1}: {Path(r['path']).name}  label {t['name']} [{t['set']} {t['number']}]  image {g['name']} [{g['set']} {g['number']}]  ({r['class']})")
    cv2.imwrite(str(SHEETS / f'hard_{s // PER_SHEET + 1}.jpg'), cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 88])
