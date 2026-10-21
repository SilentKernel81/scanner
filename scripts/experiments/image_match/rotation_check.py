#!/usr/bin/env python3
"""
Orientation: what does an upside-down card do to the lookup? Compares rows.json of the normal run with the 180-degree run
(embed.py --rotate180, then evaluate.py <name>_rot180) and answers the question that decides the phone interface: does a
rotated card get a WRONG answer with a clear lead (so the app would add it by itself), or a weak one (so it would go to review)?

    venv/bin/python -I rotation_check.py <name, e.g. phash_rgb_card>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT

name = sys.argv[1]

# The remedy: look the picture up in BOTH orientations and keep the one whose best match is more similar. For a card that is
# upright, how often would the upside-down lookup score higher and flip the answer? (The rows of the two runs line up one to one.)
up = json.loads((OUT / name / 'rows.json').read_text())
down = json.loads((OUT / f'{name}_rot180' / 'rows.json').read_text())
pairs = [(u, d) for u, d in zip(up, down) if u['path'] == d['path'] and u['kind'] in ('collection', 'scanned')]
flipped = [u for u, d in pairs if d['sims'][0] > u['sims'][0]]
best_right = sum((d if d['sims'][0] > u['sims'][0] else u)['class'] == 'correct' for u, d in pairs)
print(f'both orientations looked up, the more similar kept: {len(flipped)} of {len(pairs)} upright pictures would flip '
      f'({100 * len(flipped) / len(pairs):.2f}%); top-1 right {100 * best_right / len(pairs):.1f}% (upright alone: '
      f'{100 * sum(u["class"] == "correct" for u, d in pairs) / len(pairs):.1f}%)\n')

for label, folder in (('upright', name), ('upside down', f'{name}_rot180')):
    rows = json.loads((OUT / folder / 'rows.json').read_text())
    rows = [r for r in rows if r['kind'] in ('collection', 'scanned')]
    n = len(rows)
    right = sum(r['class'] == 'correct' for r in rows)
    lead = lambda r: r['sims'][0] - r['sims'][1]
    wrong_leading = sum(r['class'] != 'correct' and lead(r) >= 0.05 for r in rows)
    wrong_card_leading = sum(r['class'] == 'wrong_card' and lead(r) >= 0.05 for r in rows)
    added = sum(lead(r) >= 0.05 for r in rows)
    print(f'{label:12} n={n}  top-1 right {100 * right / n:.1f}%  top-5 has it {100 * sum(r["in_top5"] for r in rows) / n:.1f}%  '
          f'would be added by the image alone (lead >= 0.05): {100 * added / n:.1f}%, of which wrong {100 * wrong_leading / max(added, 1):.1f}% '
          f'({wrong_card_leading} the wrong CARD)')
