#!/usr/bin/env python3
"""
What the collection holds for the Deadpool Secret Lair cards (from the read-only copy of 2026-10-19, truth/captures.json): per entry
(printing + finish) the photos it has, and for each photo what the colour hash's nearest Scryfall picture says. If the owner has one regular,
one foil and one confetti foil copy of each of the five cards, each card should be: IFIYW-n regular x1, IFIYW-n foil x1, IFIYW-(n+5) foil x1.

    venv/bin/python -I deadpool_entries.py
"""
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, TRUTH, printings

P = printings()
captures = json.loads((TRUTH / 'captures.json').read_text())
rows = {Path(r['path']).name: r for r in json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text()) if r['kind'] == 'collection'}
entries = collections.defaultdict(list)
for c in captures:
    p = P.get(c['card_id'])
    if p and p['set'] == 'sld' and p['number'].startswith('IFIYW'):
        r = rows.get(c['file'])
        entries[(c['inv_id'], p['name'], p['number'], c['finish'])].append((c['file'], P[r['got']]['number'] if r else '(photo missing)'))
print(f'{"entry id":>8} {"card":22} {"collection says":16} {"finish":8} photos  what the picture looks like')
for (inv, name, number, finish), photos in sorted(entries.items(), key=lambda kv: (kv[0][1], int(kv[0][2].split('-')[1]), kv[0][3])):
    print(f'{inv:>8} {name:22} {number:16} {finish:8} {len(photos):>6}  {", ".join(look for _, look in photos)}')
