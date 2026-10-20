#!/usr/bin/env python3
"""
Calibration for the printing check (core/printing_check.py, shadow mode). The rule: the printing the server resolved (R) should look like the
capture AS WELL AS the best match does; GAP = similarity(best match) - similarity(R). A small gap = they agree (a reprint with the same art
has almost the same hash); a large gap = the picture looks like something else. What gap separates the two?

Two groups, by the colour hash (phash_rgb_card), R = the printing the label says:
  RIGHT  every collection / scanned picture whose label is not known to be wrong (the labels came from OCR / the AI: a few hidden errors
         make the false-alarm rate slightly pessimistic)
  WRONG  labels KNOWN to be wrong: the AI's misreads among the hard cases (frozen truth differs from the label) and the five Deadpool
         collection photos the owner confirmed were filed under the wrong entry
Reports the gap's percentiles for each and, per threshold, how many RIGHT would be flagged (false alarms) and how many WRONG caught.

    venv/bin/python -I calibrate_gap.py      (the experiment venv)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from common import OUT, TRUTH, printings

NAME = 'phash_rgb_card'
P = printings()
g, e = np.load(OUT / NAME / 'gallery.npz'), np.load(OUT / NAME / 'eval.npz')
ids = list(g['ids'])
row = {i: n for n, i in enumerate(ids)}
items = json.loads((OUT / 'eval.json').read_text())
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
relabel = json.loads((TRUTH / 'hard_relabel.json').read_text())
BITS = g['vectors'].shape[1] * 8

# hamming distance of one query to the whole gallery, through a lookup table on bytes
popcount = np.array([bin(i).count('1') for i in range(256)], np.uint8)
gallery = g['vectors']


def similarities(query):
    return 1.0 - 2.0 * popcount[np.bitwise_xor(gallery, query)].sum(axis=1, dtype=np.int32) / BITS


# the Deadpool collection photos filed under the confetti entry although the picture is the plain one (owner-confirmed)
deadpool_wrong = set()
for it in items:
    t = P[it['truth']]
    if it['kind'] == 'collection' and t['set'] == 'sld' and t['number'].startswith('IFIYW-') and int(t['number'].split('-')[1]) > 5:
        deadpool_wrong.add(it['path'])        # candidates: decided below by the picture itself (both photos of a pair are here)

right, wrong = [], []
for it, query, ok in zip(items, e['vectors'], e['ok']):
    if not ok or it['kind'] not in ('collection', 'scanned') or it['truth'] not in row:
        continue
    sim = similarities(query)
    gap = float(sim.max() - sim[row[it['truth']]])
    name = Path(it['path']).name
    verdict = relabel.get(name)
    known_wrong = (it['kind'] == 'scanned' and name in frozen and frozen[name] != it['truth']) or verdict in ('image', 'neither')
    if it['path'] in deadpool_wrong:
        # of the two photos filed under a confetti entry, the one the picture says is the plain foil is the mislabelled one
        known_wrong = known_wrong or ids[int(sim.argmax())] != it['truth']
    (wrong if known_wrong else right).append(gap)
right, wrong = np.array(right), np.array(wrong)
print(f'RIGHT labels: {len(right)} pictures; WRONG labels: {len(wrong)} pictures')
for title, gaps in (('RIGHT', right), ('WRONG', wrong)):
    print(f'{title:5} gap percentiles (0 = R is the best match): ' + '  '.join(f'p{p}={np.percentile(gaps, p):.3f}' for p in (0, 50, 90, 95, 99, 99.9, 100)))
print('\nthreshold   false alarms among RIGHT        WRONG caught')
for t in (0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15):
    print(f'  {t:5.2f}    {100 * (right >= t).mean():6.2f}% ({int((right >= t).sum()):>4} of {len(right)})      '
          f'{100 * (wrong >= t).mean():6.1f}% ({int((wrong >= t).sum())} of {len(wrong)})')
