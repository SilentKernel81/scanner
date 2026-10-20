#!/usr/bin/env python3
"""
The fine-tune's in-training "quick check" (finetune.quick_validation) applied to the colour hash, for a reference: 400 held-out printings,
each degraded by the training's own augmentation, looked up among 3,000 held-out + 3,000 training printings; right = the nearest picture has
the same visual class (art + frame + border + effects + language). Same sampling as the quick check (same seed, same lists).

    venv/bin/python -I quick_baseline.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
from common import IMAGES, OUT, prepare, printings, read
from embed import phash_bits
from finetune import PRE, augment, visual_key

P = printings()
held = sorted(json.loads((OUT / 'ft_heldout.json').read_text()))
held_set = set(held)
g = np.load(OUT / 'phash_rgb_card' / 'gallery.npz')
all_ids = list(g['ids'])
row = {i: n for n, i in enumerate(all_ids)}
train = sorted(i for i in all_ids if i not in held_set)
rng = np.random.default_rng(1)
gallery_ids = held[:3000] + train[:3000]
queries = [held[i] for i in rng.choice(len(held), min(400, len(held)), replace=False)]


def pm(v):
    return (np.unpackbits(v, axis=1).astype(np.float32) * 2 - 1) / np.sqrt(768)


gallery = pm(g['vectors'][[row[i] for i in gallery_ids]])
q = []
for qid in queries:
    rgb = cv2.resize(read(IMAGES / f'{qid}.jpg'), PRE, interpolation=cv2.INTER_AREA)
    q.append(pm(phash_bits(prepare(augment(rgb, rng), 'card', True), 'phash_rgb')[None])[0])
top = (np.array(q) @ gallery.T).argmax(1)
same = [visual_key(P[gallery_ids[t]]) == visual_key(P[qid]) for t, qid in zip(top, queries)]
exact = [gallery_ids[t] == qid for t, qid in zip(top, queries)]
print(f'colour hash on the quick check: same visual class {100 * np.mean(same):.1f}%, the very printing {100 * np.mean(exact):.1f}%')
