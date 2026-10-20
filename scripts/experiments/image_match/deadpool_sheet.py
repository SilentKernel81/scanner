#!/usr/bin/env python3
"""
The 15 Deadpool (Secret Lair "IFIYW") photos of the collection, for the owner to judge: camera picture | the printing the collection says
| the printing the image match picked. Rows where the two printings are the same are marked 'agree'.

    venv/bin/python -I deadpool_sheet.py [embedding folder name, default phash_rgb_card]
Writes data/logs/image_match/deadpool_1.jpg, deadpool_2.jpg (inside the project, git-ignored) and prints the rows.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
from common import IMAGES, OUT, printings, read

W, H, PER = 220, 308, 6
name = sys.argv[1] if len(sys.argv) > 1 else 'phash_rgb_card'
P = printings()
rows = [r for r in json.loads((OUT / name / 'rows.json').read_text())
        if r['kind'] == 'collection' and P[r['truth']]['set'] == 'sld' and P[r['truth']]['number'].startswith('IFIYW')]
rows.sort(key=lambda r: (P[r['truth']]['name'], P[r['truth']]['number']))
print(f'{len(rows)} Deadpool photos')
sheets = Path(__file__).resolve().parents[3] / 'data' / 'logs' / 'image_match'
sheets.mkdir(parents=True, exist_ok=True)
for s in range(0, len(rows), PER):
    group = rows[s:s + PER]
    sheet = np.zeros((H * len(group), W * 3, 3), np.uint8)
    for i, r in enumerate(group):
        for j, path in enumerate((r['path'], IMAGES / f"{r['truth']}.jpg", IMAGES / f"{r['got']}.jpg")):
            rgb = read(path)
            if rgb is not None:
                sheet[i * H:(i + 1) * H, j * W:(j + 1) * W] = cv2.resize(rgb, (W, H), interpolation=cv2.INTER_AREA)
        t, g = P[r['truth']], P[r['got']]
        print(f"sheet {s // PER + 1} row {i + 1}: collection says {t['name']} {t['number']}   image picked {g['name']} {g['number']}   {'agree' if r['truth'] == r['got'] else 'DIFFERENT'}")
    cv2.imwrite(str(sheets / f'deadpool_{s // PER + 1}.jpg'), cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
