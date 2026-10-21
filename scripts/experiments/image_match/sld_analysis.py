#!/usr/bin/env python3
"""
How do the methods do on Secret Lair (set 'sld'), and on the Deadpool 'IFIYW-n' cards in particular?
Reads strip_items.json (strip.py) and the printings; the labels of these cards are the least certain ones
(see show_errors.py), so the numbers are against the labels as they are, not against a verified truth.

    venv/bin/python -I sld_analysis.py <embedding folder name>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, printings

name = sys.argv[1]
P = printings()
items = json.loads((OUT / name / 'strip_items.json').read_text())


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def show(title, rows):
    if not rows:
        print(f'{title}: none')
        return
    n = len(rows)
    both = [x for x in rows if x['both']]
    print(f"{title}: n={n}  image top-1 {pct(sum(x['image'] for x in rows), n)}%  top-5 {pct(sum(x['in_top5'] for x in rows), n)}%  "
          f"strip alone {pct(sum(x['strip_only'] for x in rows), n)}%  strip+image agree {pct(len(both), n)}% (right {pct(sum(x['both_right'] for x in both), len(both))}% of those)")


for kind in ('collection', 'scanned'):
    rows = [x for x in items if x['kind'] == kind]
    sld = [x for x in rows if P[x['truth']]['set'] == 'sld']
    deadpool = [x for x in sld if P[x['truth']]['number'].startswith('IFIYW')]
    print(f'\n== {kind}: {len(rows)} pictures, {len(sld)} Secret Lair ({pct(len(sld), len(rows))}%), {len(deadpool)} of them IFIYW (Deadpool)')
    show('  everything else', [x for x in rows if x not in sld])
    show('  Secret Lair, not IFIYW', [x for x in sld if x not in deadpool])
    show('  IFIYW (Deadpool)', deadpool)
    print(f"  IFIYW printings in the labels: {len({x['truth'] for x in deadpool})}; same-card variants the image has to tell apart: "
          f"{sum(1 for x in deadpool if x['cls'] == 'other_art')} pictures end as 'same card, other art'")
