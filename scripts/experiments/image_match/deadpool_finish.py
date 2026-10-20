#!/usr/bin/env python3
"""
Do the paired Deadpool variants (IFIYW-n and IFIYW-n+5) differ by finish? Prints, per collection photo: the printing the collection says,
the finish stored for the photo, the finishes Scryfall lists for that printing, and the sibling variant's finishes.
    venv/bin/python -I deadpool_finish.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, printings

P = printings()
by_number = {p['number']: p for p in P.values() if p['set'] == 'sld' and p['number'].startswith('IFIYW')}
for number in sorted(by_number, key=lambda n: int(n.split('-')[1])):
    p = by_number[number]
    print(f"{number:9} {p['name']:24} finishes {p['finishes']}  frame_effects {p['frame_effects']}  promo {p['promo']}")
print()
for it in json.loads((OUT / 'eval.json').read_text()):
    t = P[it['truth']]
    if it['kind'] == 'collection' and t['set'] == 'sld' and t['number'].startswith('IFIYW'):
        print(f"photo labelled {t['number']:9} {t['name']:24} stored finish {it['finish']:8} printing's finishes {t['finishes']}")
