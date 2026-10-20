#!/usr/bin/env python3
"""
"Card first, then picture": once the CARD is known (what the strip names, even when a Secret Lair card prints the base printing's set
and number), how well does the picture alone choose among that card's printings? The card is taken from the label here (an upper bound:
the strip names the right card for 97-99% of pictures), and only that card's printings are candidates, nearest hash wins.

    venv/bin/python -I oracle_rerank.py [embedding folder name, default phash_rgb_card]
Reported for: the Deadpool IFIYW photos, Secret Lair in general, and everything - against the plain image lookup over all 112,765.
'exact' = the very printing; 'same look' = a printing with the same illustration, frame and border (indistinguishable by picture, the
collector number decides those).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from common import OUT, printings

name = sys.argv[1] if len(sys.argv) > 1 else 'phash_rgb_card'
P = printings()
g, e = np.load(OUT / name / 'gallery.npz'), np.load(OUT / name / 'eval.npz')
ids = list(g['ids'])
row = {i: n for n, i in enumerate(ids)}
items = json.loads((OUT / 'eval.json').read_text())
rows = {r['path']: r for r in json.loads((OUT / name / 'rows.json').read_text())}


def pm(v):
    """a hash as +-1 floats; real vectors (HOG, networks) as they are"""
    return np.unpackbits(v, axis=1).astype(np.float32) * 2 - 1 if v.dtype == np.uint8 else v.astype(np.float32)


gallery_pm = pm(g['vectors'])
queries = pm(e['vectors'])
by_oracle = {}
for n, i in enumerate(ids):
    by_oracle.setdefault(P[i]['oracle_id'], []).append(n)


def look(p):
    return (p['illustration_id'] or p['oracle_id'], p['frame'], p['border'], tuple(sorted(p['frame_effects'] or [])), p['lang'])


groups = {'Deadpool IFIYW': [], 'Secret Lair, not IFIYW': [], 'all other': []}
for q, it, ok in zip(queries, items, e['ok']):
    if not ok or it['kind'] not in ('collection', 'scanned') or it['truth'] not in row:
        continue
    t = P[it['truth']]
    cand = by_oracle[t['oracle_id']]
    best = ids[cand[int(np.argmax(gallery_pm[cand] @ q))]]
    plain = rows.get(it['path'])
    key = 'Deadpool IFIYW' if t['set'] == 'sld' and t['number'].startswith('IFIYW') else 'Secret Lair, not IFIYW' if t['set'] == 'sld' else 'all other'
    groups[key].append((best == it['truth'], look(P[best]) == look(t), plain['got'] == it['truth'] if plain else None, len(cand)))

print(f'{"group":24} {"n":>5} {"plain top-1":>12} {"card known: exact":>18} {"same look":>10} {"printings of the card (mean)":>30}')
for key, rs in groups.items():
    if rs:
        n = len(rs)
        plain = [r[2] for r in rs if r[2] is not None]
        print(f'{key:24} {n:>5} {100 * np.mean(plain):>11.1f}% {100 * sum(r[0] for r in rs) / n:>17.1f}% {100 * sum(r[1] for r in rs) / n:>9.1f}% {np.mean([r[3] for r in rs]):>30.1f}')
