#!/usr/bin/env python3
"""
Sanity check of the stored embeddings: recompute the vector of random gallery and evaluation pictures and
compare with what out/<name>/gallery.npz and eval.npz hold at that position. A cosine of ~1.0 means the ids
and vectors line up (no shifted rows after the resumable merges); anything lower is a bug in embed.py.
Also: a Scryfall picture searched in its own gallery must find itself (the identity test).

    venv/bin/python -I sanity.py <embedding name> [--model mnv3|dino] [--crop card|art] [--no-normalise]
"""
import argparse
import json
import random
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import torch
from common import IMAGES, OUT
from embed import Pictures, build


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name')
    ap.add_argument('--model', required=True)
    ap.add_argument('--crop', default='card')
    ap.add_argument('--no-normalise', action='store_true')
    a = ap.parse_args()
    net = build(a.model)[0]
    g = np.load(OUT / a.name / 'gallery.npz')
    e = np.load(OUT / a.name / 'eval.npz')
    ids, gv, ev = list(g['ids']), g['vectors'], e['vectors']
    random.seed(3)

    def fresh(items):
        data = Pictures(items, a)
        x = torch.stack([data[i][0] for i in range(len(items))])
        with torch.no_grad():
            return torch.nn.functional.normalize(net(x), dim=1).numpy()

    pick = random.sample(range(len(ids)), 100)
    now = fresh([{'path': str(IMAGES / f'{ids[i]}.jpg')} for i in pick])
    cos = (now * gv[pick]).sum(1)
    print(f'gallery rows recomputed: cosine to stored min {cos.min():.4f} mean {cos.mean():.4f}')

    items = json.loads((OUT / 'eval.json').read_text())
    pick_e = random.sample(range(len(items)), 100)
    now = fresh([items[i] for i in pick_e])
    cos = (now * ev[pick_e]).sum(1)
    print(f'eval rows recomputed: cosine to stored min {cos.min():.4f} mean {cos.mean():.4f}')

    # identity: each of those gallery pictures against the whole gallery
    q = gv[pick]
    top = np.argmax(q @ gv.T, axis=1)
    print(f'a gallery picture finds itself: {np.mean(top == np.array(pick)) * 100:.1f}% (should be 100)')

    # a degraded copy of the gallery picture (dark, blurred, tinted, a little shifted) - like a camera would see it
    import cv2
    from common import read
    degraded = []
    for i in pick[:60]:
        rgb = read(IMAGES / f'{ids[i]}.jpg').astype(np.float32)
        rgb = cv2.GaussianBlur(rgb, (0, 0), 2.0) * 0.55 * np.array([1.0, 0.92, 0.85])
        rgb = np.clip(rgb + np.random.default_rng(i).normal(0, 6, rgb.shape), 0, 255).astype(np.uint8)
        M = np.float32([[1, 0, 6], [0, 1, 5]])
        degraded.append(cv2.warpAffine(rgb, M, (rgb.shape[1], rgb.shape[0]), borderValue=(0, 0, 0)))
    tmp = OUT / 'degraded'
    tmp.mkdir(exist_ok=True)
    paths = []
    for n, d in enumerate(degraded):
        cv2.imwrite(str(tmp / f'{n}.jpg'), cv2.cvtColor(d, cv2.COLOR_RGB2BGR))
        paths.append({'path': str(tmp / f'{n}.jpg')})
    q = fresh(paths)
    top = np.argmax(q @ gv.T, axis=1)
    print(f'degraded copy (blur, dark, tint, noise, shift) finds the original: {np.mean(top == np.array(pick[:60])) * 100:.1f}%')


if __name__ == '__main__':
    main()
