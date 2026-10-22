#!/usr/bin/env python3
"""
Where real captures fall against the Magic back (core/card_back.py; step 1 of docs/plans/CARD_BACK_STOP_PLAN.md): card_back_similarity of every picture in the BACKS
folders (captures of face-down cards) and of every picture in the FRONTS folders (captures of card fronts), and how many of each a threshold lets through.
CARD_BACK_MIN_SIMILARITY goes between the two: under the lowest back, over the highest front.

    venv/bin/python -I scripts/experiments/image_match/card_back_measure.py --backs DIR [DIR...] --fronts DIR [DIR...]
Measured 2026-10-21, fronts only (no capture of a back existed yet): the 16,881 saved captures of the development machine (this checkout's scanned_cards/ and
~/.cache/mtg-image-match/truth/{captures,rescan_cards,scanned_cards}) reach at most 0.219, 99.9% below 0.177.
"""
import argparse, sys
from pathlib import Path
import cv2, numpy as np
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
from card_scanner.core import card_back

ap = argparse.ArgumentParser()
ap.add_argument('--backs', nargs='*', default=[])
ap.add_argument('--fronts', nargs='*', default=[])
arguments = ap.parse_args()


def scores(folders):
    """[(similarity, path)] of every readable .jpg / .png under the folders, highest first"""
    rows = []
    for folder in folders:
        for path in sorted(p for p in Path(folder).rglob('*') if p.suffix.lower() in ('.jpg', '.jpeg', '.png')):
            bgr = cv2.imread(str(path))
            if bgr is not None and min(bgr.shape[:2]) >= 50:
                rows.append((card_back.card_back_similarity(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), str(path)))
    return sorted(rows, reverse=True)


backs, fronts = scores(arguments.backs), scores(arguments.fronts)
for name, rows, shown in (("BACKS", backs, 50), ('FRONTS', fronts, 10)):
    if rows:
        values = np.array([r[0] for r in rows])
        print(f"{name}: {len(rows)} pictures, min {values.min():.3f}, median {np.median(values):.3f}, max {values.max():.3f}")
        for s, p in (rows[-shown:] if name == 'BACKS' else rows[:shown]):            # the backs nearest a front, the fronts nearest a back
            print(f"   {s:.3f} {p}")
print(f"\nat CARD_BACK_MIN_SIMILARITY = {card_back.CARD_BACK_MIN_SIMILARITY}: "
      f"{sum(s >= card_back.CARD_BACK_MIN_SIMILARITY for s, _ in backs)} of {len(backs)} backs stop the camera, "
      f"{sum(s >= card_back.CARD_BACK_MIN_SIMILARITY for s, _ in fronts)} of {len(fronts)} fronts would")
