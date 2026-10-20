#!/usr/bin/env python3
"""
Is the foil classifier reading the marker, or just the set? The crop of the set line contains the set code, and in this collection
some sets are nearly all foil. A baseline that guesses by set alone (the finish most common in that set among the TRAINING cards)
is scored on exactly the cards the classifier was tested on (same hash split by card). If it gets close to the classifier's
accuracy, the classifier's number says little about the marker.

    venv/bin/python -I foil_baseline.py
"""
import collections
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from common import OUT, printings

P = printings()
d = np.load(OUT / 'foil_crops.npz')
label, truth, read_marker, both = d['label'], d['truth'], d['read_marker'], d['both']
test = np.array([int(hashlib.md5(t.encode()).hexdigest(), 16) % 5 == 0 for t in truth])
sets = np.array([P[t]['set'] for t in truth])

foil_share = collections.defaultdict(lambda: [0, 0])      # per set, in the TRAINING cards: [foil, all]
for s, l in zip(sets[~test], label[~test]):
    foil_share[s][0] += l
    foil_share[s][1] += 1
overall = label[~test].mean()
guess = np.array([(foil_share[s][0] / foil_share[s][1] if s in foil_share else overall) > 0.5 for s in sets]).astype(int)

for title, mask in (('all test crops', np.ones(len(label), bool)), ('marker needed', both),
                    ('marker needed AND unread by the OCR', both & (read_marker == 'unknown'))):
    m = mask & test
    print(f'{title}: n={int(m.sum())}  guessing by set alone {100 * (guess[m] == label[m]).mean():.1f}% right;  always "not foil" {100 * (label[m] == 0).mean():.1f}%')

# how mixed are the sets? (a set with both finishes in the data cannot be guessed)
mixed = [s for s, (f, n) in foil_share.items() if 0 < f < n]
print(f'\nsets in training: {len(foil_share)}; with both foil and non-foil pictures: {len(mixed)}')
unread_test = test & both & (read_marker == 'unknown')
in_mixed = np.array([s in mixed for s in sets])
m = unread_test & in_mixed
print(f'unread test cards in sets that have both finishes: n={int(m.sum())} of {int(unread_test.sum())}; set-guess right {100 * (guess[m] == label[m]).mean():.1f}%')
