#!/usr/bin/env python3
"""
Does core/image_index.hash_card give the bits the experiment's gallery has (out/phash_rgb_card/gallery.npz, made by embed.py with
OpenCV 4.14)? Every number measured in the experiment applies to the shipped fingerprint only if it does. Run in the REPOSITORY's venv
(another OpenCV: 5.x), so a version's effect shows too.

    venv/bin/python -I scripts/experiments/image_match/check_hash_parity.py [how many printings, default 600]
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
from card_scanner.core import image_index
from common import IMAGES, OUT

count = int(sys.argv[1]) if len(sys.argv) > 1 else 600
g = np.load(OUT / 'phash_rgb_card' / 'gallery.npz')
ids, vectors = list(g['ids']), g['vectors']
chosen = np.random.default_rng(2).choice(len(ids), count, replace=False)
differing = []
for i in chosen:
    rgb = cv2.cvtColor(cv2.imread(str(IMAGES / f'{ids[i]}.jpg')), cv2.COLOR_BGR2RGB)
    mine = np.frombuffer(image_index.hash_card(rgb), np.uint8)
    differing.append(int(np.unpackbits(mine ^ vectors[i]).sum()))
differing = np.array(differing)
print(f"OpenCV {cv2.__version__}, numpy {np.__version__}: {count} printings; identical fingerprints {int((differing == 0).sum())} "
      f"({100 * (differing == 0).mean():.1f}%); bits differing - mean {differing.mean():.3f}, largest {differing.max()} of {image_index.HASH_BITS} "
      f"(one bit = {2 / image_index.HASH_BITS:.4f} of similarity)")
