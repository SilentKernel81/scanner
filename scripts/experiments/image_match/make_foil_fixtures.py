#!/usr/bin/env python3
"""
Writes tests/data/foil_marker_crops.npz: 16 real set-line crops (8 foil, 8 regular; 32 x 128 grey, a collector line - no card art) that the
shipped network never trained on (the test side of foil_train.py's split by card) and is confident about, one per card, so
tests/test_foil_marker.py can check the shipped weights AND the preprocessing of core/foil_marker.py on real examples.

    ~/.cache/mtg-image-match/venv/bin/python -I scripts/experiments/image_match/make_foil_fixtures.py
"""
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import numpy as np
import torch
from common import OUT
from export_foil import foil_net, normalise   # (importing it re-exports the model; harmless)

TARGET = HERE.parents[2] / 'tests' / 'data' / 'foil_marker_crops.npz'
d = np.load(OUT / 'foil_crops.npz')
crops, label, truth, both = d['crops'], d['label'], d['truth'], d['both']
test = np.array([int(hashlib.md5(t.encode()).hexdigest(), 16) % 5 == 0 for t in truth])
model = foil_net()
model.load_state_dict(torch.load(OUT / 'foil_net.pt', map_location='cpu'))
model.eval()
with torch.no_grad():
    prob = torch.softmax(model(torch.from_numpy(normalise(crops))), 1)[:, 1].numpy()

rng = np.random.default_rng(5)
picked, seen = [], set()
for wanted in (1, 0):
    candidates = [i for i in rng.permutation(len(crops))
                  if test[i] and both[i] and label[i] == wanted and (prob[i] > 0.97 if wanted else prob[i] < 0.03) and truth[i] not in seen]
    for i in candidates[:8]:
        picked.append(i)
        seen.add(truth[i])
TARGET.parent.mkdir(exist_ok=True)
np.savez_compressed(TARGET, crops=crops[picked], foil=label[picked].astype(np.uint8), probability=prob[picked].astype(np.float32))
print('written', TARGET, len(picked), 'crops;', int(label[picked].sum()), 'foil;', round(TARGET.stat().st_size / 1024, 1), 'KB')
