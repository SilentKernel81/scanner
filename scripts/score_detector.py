#!/usr/bin/env python3
"""
Scores the card outline detector on labelled camera frames (data/frames/, see
scripts/label_frames.py): for each frame the detector's outline is compared with the label by
intersection over union (IoU: 1.0 = the same area; the card's edge placed 1% of its width off
all round is about 0.96).

    venv/bin/python scripts/score_detector.py [--frames data/frames] [--station rpi-scanner]
                                              [--worst 8] [--json out.json]

Three numbers are given per frame:
  stored    the outline the station found when the frame was taken (its note) - what happened
  replay    the outline the code in this checkout finds on the frame now - what a change does
  vs label  both against what the person said the card's edge is

The replay runs the detector on the whole frame, as a camera that sends no small copy of its
own does; it does not follow the previous frame's outline (`previous`), which the live station
does, so replay and stored differ a little even for unchanged code - compare replay with replay
when judging a change. Nothing is written except with --json.

Frames labelled "no_card" / "unsure" are left out; a frame the detector finds no outline on
counts as IoU 0 in the replay (a miss) and is listed.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.station.object_detector import ObjectDetector  # noqa: E402


def iou(a, b, shape):
    """Intersection over union of two quadrilaterals, drawn at the frame's pixels"""
    if a is None or b is None:
        return 0.0
    first = np.zeros(shape, np.uint8)
    second = np.zeros(shape, np.uint8)
    cv2.fillPoly(first, [np.round(np.asarray(a, float)).astype(np.int32)], 1)
    cv2.fillPoly(second, [np.round(np.asarray(b, float)).astype(np.int32)], 1)
    union = int((first | second).sum())
    return float((first & second).sum()) / union if union else 0.0


def load(folder, station=None):
    """(note, label, picture path) of each labelled frame, oldest first"""
    found = []
    for label_path in sorted(Path(folder).glob('*.label.json'), key=lambda p: p.stat().st_mtime):
        stem = label_path.name[:-len('.label.json')]
        try:
            label = json.loads(label_path.read_text())
            note = json.loads((Path(folder) / f'{stem}.json').read_text())
        except (OSError, ValueError):
            continue
        picture = Path(folder) / f'{stem}.jpg'
        if label.get('status') != 'labelled' or not label.get('corners') or not picture.is_file():
            continue
        if station and note.get('station') != station:
            continue
        found.append((stem, note, label, picture))
    return found


def score(folder, station=None, detector=None):
    """One row per labelled frame (see the module's text); the detector is replaced in tests"""
    detector = detector or ObjectDetector()
    rows = []
    for stem, note, label, picture in load(folder, station):
        bgr = cv2.imread(str(picture))
        if bgr is None:
            continue
        frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        shape = frame.shape[:2]
        started = time.perf_counter()
        _, _, _, corners = detector.detect(frame)
        took = time.perf_counter() - started
        rows.append({
            'stem': stem, 'station': note.get('station'), 'number': note.get('number'),
            'mode': 'fixed' if note.get('fixed_area') else 'outline',
            'stored': iou(note.get('corners'), label['corners'], shape),
            'replay': iou(corners, label['corners'], shape),
            'found': corners is not None, 'stored_found': bool(note.get('corners')),
            'ms': round(took * 1000, 1),
        })
    return rows


def summary(rows, title):
    """One line each for the stored and the replayed outlines of a group of rows"""
    if not rows:
        return
    print(f"\n{title}: {len(rows)} frames")
    for key, text in (('stored', 'stored (the station, then)'), ('replay', 'replay (this code, now)')):
        values = np.array([r[key] for r in rows])
        found = sum(1 for r in rows if (r['found'] if key == 'replay' else r['stored_found']))
        print(f"  {text:28s} mean IoU {values.mean():.3f}  median {np.median(values):.3f}  worst {values.min():.3f}"
              f"  >=0.95: {int((values >= 0.95).sum()):3d}  >=0.90: {int((values >= 0.90).sum()):3d}"
              f"  no outline: {len(rows) - found}")
    print(f"  replay time: {np.mean([r['ms'] for r in rows]):.0f} ms a frame on this machine")


def main():
    parser = argparse.ArgumentParser(description="Score the outline detector against labelled frames")
    parser.add_argument('--frames', default='data/frames')
    parser.add_argument('--station', help="only this station's frames")
    parser.add_argument('--worst', type=int, default=8, help="list this many worst replayed frames")
    parser.add_argument('--json', help="also write every row to this file")
    args = parser.parse_args()
    if not Path(args.frames).is_dir():
        sys.exit(f"{args.frames} is not a folder - give the one with the .jpg / .json / .label.json files (--frames)")
    rows = score(args.frames, args.station)
    if not rows:
        sys.exit("no labelled frames there (label them with scripts/label_frames.py first)")
    summary(rows, "All")
    for station in sorted({r['station'] for r in rows}):
        for mode in ('outline', 'fixed'):
            summary([r for r in rows if r['station'] == station and r['mode'] == mode], f"{station}, {mode} mode")
    worst = sorted(rows, key=lambda r: r['replay'])[:args.worst]
    print(f"\nWorst {len(worst)} replayed:")
    for r in worst:
        print(f"  {r['station']} #{r['number']:<4} {r['mode']:7s} replay {r['replay']:.3f}  stored {r['stored']:.3f}"
              f"{'' if r['found'] else '  (no outline)'}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))


if __name__ == '__main__':
    main()
