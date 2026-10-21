#!/usr/bin/env python3
"""
Where does the no-AI pipeline struggle? Breaks strip_items.json (one row per picture, from strip.py) down by whether the
strip gave number AND set, by hard case (read by the AI today), and by finish.

    venv/bin/python -I noai_analysis.py <embedding folder name, e.g. phash_card>
"""
import collections
import json
import sys
from pathlib import Path

OUT = Path.home() / '.cache' / 'mtg-image-match' / 'out'
name = sys.argv[1]
items = json.loads((OUT / name / 'strip_items.json').read_text())


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def table(title, rows):
    print(f'\n== {title}: n={len(rows)}')
    if not rows:
        return
    print(f"   image top-1 right {pct(sum(x['image'] for x in rows), len(rows))}%, truth in top-5 {pct(sum(x['in_top5'] for x in rows), len(rows))}%, "
          f"strip gave number+set {pct(sum(x['read_number'] and x['read_set'] for x in rows), len(rows))}%, "
          f"strip+image agree (strong) {pct(sum(x['both'] for x in rows), len(rows))}%")
    for m in (0.0, 0.02, 0.05, 0.1):
        auto = [x for x in rows if x['margin'] >= m]
        print(f"   image alone, lead >= {m}: {pct(len(auto), len(rows))}% of them added, {pct(sum(x['image'] for x in auto), len(auto))}% of those right")


for kind in ('collection', 'scanned'):
    rows = [x for x in items if x['kind'] == kind]
    table(f'{kind}: all', rows)
    table(f'{kind}: strip and image did NOT agree (the cases the strong path cannot settle)', [x for x in rows if not x['both']])
    table(f'{kind}: strip gave no number or no set', [x for x in rows if not (x['read_number'] and x['read_set'])])
table('hard cases (read by the AI today)', [x for x in items if x['hard']])

if 'strip_unique' in items[0]:
    # Three ways to decide without AI. 'right' is the printing the system would add == the truth.
    #   A  the strip and an image candidate agree on set AND number (exact printing); else the image alone if it leads
    #   B  the strip names ONE printing and the image's candidates include that CARD (same name): add the strip's printing;
    #      else the image alone if it leads; else review
    #   C  the strip names one printing: add it, no image check (what the OCR alone does today); else image if it leads
    def rule(x, name, lead):
        if name == 'A' and x['both']:
            return True, x['both_right']
        if name == 'A2' and x['both'] and x.get('both_art_is_top1_art'):     # A, but the strip's printing must also have the image's art
            return True, x['both_right']
        if name == 'B' and x['strip_unique'] and x['strip_card_in_top5']:
            return True, x['strip_only']
        if name == 'C' and x['strip_unique']:
            return True, x['strip_only']
        if x['margin'] >= lead:
            return True, x['image']
        return False, None

    print('\n== no-AI rules: share added automatically / how many of those are the right printing / left for review')
    groups = [('collection', [x for x in items if x['kind'] == 'collection']), ('scanned', [x for x in items if x['kind'] == 'scanned']),
              ('hard cases', [x for x in items if x['hard']])]
    for title, rows in groups:
        print(f'  {title} (n={len(rows)})')
        for name in ('A', 'A2', 'C'):
            for lead in (0.05, 9):       # 9: the image alone is never trusted (strip only, the rest is review)
                made = [rule(x, name, lead) for x in rows]
                auto = [ok for a, ok in made if a]
                wrong = len(auto) - sum(auto)
                print(f"    rule {name}, image alone {'if it leads by 0.05' if lead < 1 else 'never'}: added {pct(len(auto), len(rows))}%, "
                      f"right {pct(sum(auto), len(auto))}% ({wrong} wrong), review {pct(len(rows) - len(auto), len(rows))}%")

print('\n== foil marker, by true finish (collection + scanned)')
for finish in ('regular', 'foil', 'surge'):
    rows = [x for x in items if x['foil_true'] == finish]
    if rows:
        c = collections.Counter(x['foil_read'] for x in rows)
        print(f"   {finish:8} n={len(rows):>5}  marker read as foil {pct(c['foil'], len(rows))}%  as non-foil {pct(c['non-foil'], len(rows))}%  not read {pct(c['unknown'], len(rows))}%")
if 'finishes_both' in items[0]:
    needs = [x for x in items if x['finishes_both']]
    unread = [x for x in needs if x['foil_read'] == 'unknown']
    print(f"   printings that exist in BOTH foil and non-foil (the marker is the only way to tell): {pct(len(needs), len(items))}% of pictures;"
          f" of those the marker is unread on {pct(len(unread), len(needs))}%  = {pct(len(unread), len(items))}% of all pictures need a foil answer from somewhere else")
