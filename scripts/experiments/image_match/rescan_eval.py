#!/usr/bin/env python3
"""
The Deadpool rescan (2026-10-20, captures #1037-#1051 of the phone, one round): 15 pictures whose version is known from the physical cards,
in the order the owner scanned them:
    1037-1041 regular,  1042-1046 foil,  1047-1051 confetti foil;   within each group the cards run Deadly Dispute, Lightning Bolt,
    Lightning Greaves, Sol Ring, Thrill of Possibility.
Scryfall entries (set SLD): regular and foil are the SAME entry IFIYW-n (Deadly Dispute 1, Lightning Bolt 2, Thrill of Possibility 3,
Lightning Greaves 4, Sol Ring 5); the owner's "confetti foil" is taken to be the foil-only entry IFIYW-(n+5) (an inference: the pictures
of those entries show the confetti, and every collection photo labelled 6-10 is stored as foil).

For each picture: the nearest Scryfall pictures by the descriptor, and a direct test of whether the picture can tell the entry n from n+5:
is it more similar to the Scryfall picture of n or of n+5?

    venv/bin/python -I rescan_eval.py [descriptor folder name, default phash_rgb_card]
"""
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from common import HOME, IMAGES, OUT, prepare, printings, read
from embed import hog_vector, phash_bits

name = sys.argv[1] if len(sys.argv) > 1 else 'phash_rgb_card'
P = printings()
CARDS = ['Deadly Dispute', 'Lightning Bolt', 'Lightning Greaves', 'Sol Ring', 'Thrill of Possibility']
NUMBER = {'Deadly Dispute': 1, 'Lightning Bolt': 2, 'Thrill of Possibility': 3, 'Lightning Greaves': 4, 'Sol Ring': 5}
VERSIONS = ['regular', 'foil', 'confetti foil']
by_number = {p['number']: p['id'] for p in P.values() if p['set'] == 'sld' and p['number'].startswith('IFIYW')}
files = {int(f.name.split('_')[1]): f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_')}

g = np.load(OUT / name / 'gallery.npz')
ids = list(g['ids'])
row = {i: n for n, i in enumerate(ids)}
is_hash = g['vectors'].dtype == np.uint8
gallery = (np.unpackbits(g['vectors'], axis=1).astype(np.float32) * 2 - 1) / np.sqrt(g['vectors'].shape[1] * 8) if is_hash else g['vectors'].astype(np.float32)


def vector(rgb):
    rgb = prepare(rgb, 'card', True)
    if name.startswith('phash_rgb'):
        return (np.unpackbits(phash_bits(rgb, 'phash_rgb')).astype(np.float32) * 2 - 1) / np.sqrt(768)
    if name.startswith('hog'):
        return hog_vector(rgb, 'hog')
    raise SystemExit('this script embeds the hash and HOG only')


print(f'descriptor {name}; similarity 1.0 = identical\n')
print(f'{"#":>5} {"version":13} {"card":22} {"truth":9} {"nearest three (number sim)":58} {"truth rank":>10} {"sim to n / n+5":>16}  n-or-n+5')
correct_variant = total = 0
for k, number in enumerate(range(1037, 1052)):
    version, card = VERSIONS[k // 5], CARDS[k % 5]
    n = NUMBER[card]
    truth_number = f'IFIYW-{n + 5 if version == "confetti foil" else n}'
    truth = by_number[truth_number]
    v = vector(read(files[number]))
    sims = gallery @ v
    order = np.argsort(-sims)[:3]
    rank = int((sims > sims[row[truth]]).sum()) + 1
    sim_n, sim_n5 = float(sims[row[by_number[f'IFIYW-{n}']]]), float(sims[row[by_number[f'IFIYW-{n + 5}']]])
    picks_n5 = sim_n5 > sim_n
    right = picks_n5 == (version == 'confetti foil')
    correct_variant += right
    total += 1
    top = '  '.join(f"{P[ids[j]]['number']} {sims[j]:.3f}" for j in order)
    print(f'{number:>5} {version:13} {card:22} {truth_number:9} {top:58} {rank:>10} {sim_n:>7.3f}/{sim_n5:<7.3f}  {"right" if right else "WRONG"}')
print(f'\nthe picture is closer to the right one of the pair (n for regular and foil, n+5 for confetti): {correct_variant} of {total}')
print('by version:', {v: sum(1 for k in range(15) if VERSIONS[k // 5] == v) for v in VERSIONS})
