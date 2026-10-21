#!/usr/bin/env python3
"""
The Python reference of the reading (ppocr_reference.py) against light-ocr itself, on the same pixels (docs/plans/ANDROID_OCR_PLAN.md, step 1).
light-ocr is started as the server starts it (src/ocr/server.mjs, provider cpu) but is sent each picture as PNG - the server's CardOcr sends a JPEG of
quality 95, which would be other pixels than the reference reads. Per picture, of the whole card and of its bottom-left strip (foil_marker.strip_of):
  - the lines each reader found, paired by the overlap of their boxes (IoU >= 0.5): lines only one found, paired lines whose text differs, and how far
    the paired boxes' corners are apart;
  - what the server's parsers make of each reader's lines: card_ocr.parse_magic (name, collector number, set code, foil) of the card; parse_strip
    (number, set) and foil_marker.symbol_read of the strip's set line (foil_marker.set_line).
Step 1 is done when the parses agree on at least 98% of the pictures and every difference is explained (scripts/experiments/ocr_port/README.md).

    venv/bin/python scripts/experiments/ocr_port/compare_with_light_ocr.py <folder> [<folder> ...] [--limit N] [--out report.json]
"""
import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(HERE))

from card_scanner.core import card_ocr, foil_marker  # noqa: E402
from card_scanner.paths import tool  # noqa: E402
from ppocr_reference import Reader  # noqa: E402


class LightOcr:
    """light-ocr as a subprocess (server.mjs's protocol), sent PNGs"""

    def __init__(self):
        self.process = subprocess.Popen([tool('node'), 'server.mjs', 'cpu'], cwd=str(REPO / 'src/ocr'), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        ready = json.loads(self.process.stdout.readline())
        assert ready.get('ready'), ready
        self.next = 0

    def read_lines(self, rgb):
        ok, png = cv2.imencode('.png', cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        self.next += 1
        self.process.stdin.write(json.dumps({'id': self.next, 'image': base64.b64encode(png.tobytes()).decode()}) + '\n')
        self.process.stdin.flush()
        answer = json.loads(self.process.stdout.readline())
        assert answer.get('id') == self.next, answer
        if 'error' in answer:
            raise RuntimeError(answer['error'])
        return answer['lines']

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)


def iou(a, b):
    """The overlap of two quads' bounding rectangles"""
    ax0, ay0, ax1, ay1 = min(p[0] for p in a), min(p[1] for p in a), max(p[0] for p in a), max(p[1] for p in a)
    bx0, by0, bx1, by1 = min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b)
    w, h = max(0.0, min(ax1, bx1) - max(ax0, bx0)), max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = w * h
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def pair(ours, theirs):
    """Lines paired by IoU >= 0.5 (greedy, best first): (pairs, only ours, only theirs)"""
    candidates = sorted(((iou(a['box'], b['box']), i, j) for i, a in enumerate(ours) for j, b in enumerate(theirs)), reverse=True)
    used_a, used_b, pairs = set(), set(), []
    for score, i, j in candidates:
        if score < 0.5 or i in used_a or j in used_b:
            continue
        used_a.add(i); used_b.add(j)
        pairs.append((ours[i], theirs[j]))
    return pairs, [l for i, l in enumerate(ours) if i not in used_a], [l for j, l in enumerate(theirs) if j not in used_b]


def magic(lines, rgb):
    read = card_ocr.parse_magic(lines, rgb.shape[1], rgb.shape[0]) or {}
    return {k: read.get(k) for k in ('name', 'collector_number', 'set_code', 'foil')}


def strip_read(lines):
    number, set_code = card_ocr.parse_strip(lines)
    line = foil_marker.set_line(lines)
    return {'number': number, 'set': set_code, 'symbol': foil_marker.symbol_read(line) if line else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folders', nargs='+')
    ap.add_argument('--limit', type=int, default=0, help='at most this many pictures per folder (0: all)')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    reader, light = Reader(), LightOcr()
    report, totals = [], {'pictures': 0, 'card_parse_same': 0, 'strip_parse_same': 0, 'lines_same_text': 0, 'lines_paired': 0, 'lines_unpaired': 0}
    times = {'reference': 0.0, 'light_ocr': 0.0}
    for folder in args.folders:
        files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
        if args.limit:
            files = files[:args.limit]
        for path in files:
            bgr = cv2.imread(str(path))
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            entry = {'picture': str(path)}
            for part, image in (('card', rgb), ('strip', foil_marker.strip_of(rgb))):
                t = time.perf_counter(); ours = reader.read_lines(image); times['reference'] += time.perf_counter() - t
                t = time.perf_counter(); theirs = light.read_lines(image); times['light_ocr'] += time.perf_counter() - t
                pairs, only_ours, only_theirs = pair(ours, theirs)
                differ = [(a['text'], b['text']) for a, b in pairs if a['text'] != b['text']]
                corner = max((max(max(abs(pa[0] - pb[0]), abs(pa[1] - pb[1])) for pa, pb in zip(a['box'], b['box'])) for a, b in pairs), default=0.0)
                parse_ours = magic(ours, image) if part == 'card' else strip_read(ours)
                parse_theirs = magic(theirs, image) if part == 'card' else strip_read(theirs)
                entry[part] = {'lines': [len(ours), len(theirs)], 'only_reference': [l['text'] for l in only_ours], 'only_light_ocr': [l['text'] for l in only_theirs],
                               'texts_differ': differ, 'worst_corner_px': round(float(corner), 1), 'parse_reference': parse_ours, 'parse_light_ocr': parse_theirs,
                               'parse_same': parse_ours == parse_theirs}
                totals['lines_paired'] += len(pairs)
                totals['lines_same_text'] += len(pairs) - len(differ)
                totals['lines_unpaired'] += len(only_ours) + len(only_theirs)
            totals['pictures'] += 1
            totals['card_parse_same'] += entry['card']['parse_same']
            totals['strip_parse_same'] += entry['strip']['parse_same']
            report.append(entry)
            if not (entry['card']['parse_same'] and entry['strip']['parse_same']):
                print(f"DIFFERS {path.name}: card {entry['card']['parse_reference']} / {entry['card']['parse_light_ocr']}; "
                      f"strip {entry['strip']['parse_reference']} / {entry['strip']['parse_light_ocr']}", flush=True)
    light.close()
    n = max(1, totals['pictures'])
    print(f"\n{totals['pictures']} pictures: the card's parse the same for {totals['card_parse_same']} ({100 * totals['card_parse_same'] / n:.1f}%), "
          f"the strip's for {totals['strip_parse_same']} ({100 * totals['strip_parse_same'] / n:.1f}%); "
          f"{totals['lines_same_text']} of {totals['lines_paired']} paired lines the same text, {totals['lines_unpaired']} lines only one reader found; "
          f"per picture (card + strip): reference {times['reference'] / n:.2f} s, light-ocr {times['light_ocr'] / n:.2f} s")
    if args.out:
        Path(args.out).write_text(json.dumps({'totals': totals, 'seconds': times, 'pictures': report}, indent=1, ensure_ascii=False))


if __name__ == '__main__':
    main()
