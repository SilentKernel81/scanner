#!/usr/bin/env python3
"""
The fine-tune against the colour hash, point by point, with the bar written into docs/plans/IMAGE_MATCH_EXPERIMENT.md BEFORE the run:
  1. a review rate lower by >= 0.3 points on BOTH the collection and the scanned set (rule A2 + image alone if it leads by 0.05), with at
     most 10% more wrong additions in each; OR frame top-1 at least 3 points higher
  2. on the held-out cards (the Spider-Man set + 5% of the visual classes), top-1 not more than 1 point below the hash's
  3. on the corrected hard cases: review rate not higher than the hash's, and no wrong addition
All three must hold for "pass". Writes out/ft_verdict.txt too.

    venv/bin/python -I compare_ft.py [fine-tune name, default ft_card] [baseline name, default phash_rgb_card]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT

ft = sys.argv[1] if len(sys.argv) > 1 else 'ft_card'
base = sys.argv[2] if len(sys.argv) > 2 else 'phash_rgb_card'
lines = []


def say(text=''):
    print(text)
    lines.append(text)


def load(name, file):
    return json.loads((OUT / name / file).read_text())


def a2(name, kind):
    """(review %, wrong additions, added) under rule A2 + image alone if it leads by 0.05"""
    rows = [x for x in load(name, 'strip_items.json') if x['kind'] == kind]
    added = wrong = 0
    for x in rows:
        if x['both'] and x.get('both_art_is_top1_art'):
            added += 1
            wrong += not x['both_right']
        elif x['margin'] >= 0.05:
            added += 1
            wrong += not x['image']
    return 100 * (len(rows) - added) / len(rows), wrong, added


say(f'== {ft} against {base} ==')
ok1 = True
for kind in ('collection', 'scanned'):
    (rb, wb, ab), (rf, wf, af) = a2(base, kind), a2(ft, kind)
    better = rb - rf >= 0.3 and wf <= 1.1 * wb + 0.5
    ok1 = ok1 and better
    say(f'1. {kind:10} review {rb:.2f}% -> {rf:.2f}% ({rb - rf:+.2f} points), wrong additions {wb} -> {wf}   {"ok" if better else "not enough"}')
frames_b, frames_f = (load(n, 'result.json')['by_kind']['frame']['top1'] for n in (base, ft))
frames_ok = frames_f - frames_b >= 3
say(f'1. frames top-1 {frames_b}% -> {frames_f}% ({frames_f - frames_b:+.1f} points)   {"ok" if frames_ok else "not 3 points"}')
point1 = ok1 or frames_ok
say(f'   point 1: {"PASS" if point1 else "FAIL"}')

ub, uf = (load(n, 'result.json')['by_heldout']['unseen by the fine-tune'] for n in (base, ft))
point2 = uf['top1'] >= ub['top1'] - 1
say(f"2. held-out (unseen) cards, n={uf['n']}: top-1 {ub['top1']}% -> {uf['top1']}%, top-5 {ub['top5']}% -> {uf['top5']}%   point 2: {'PASS' if point2 else 'FAIL'}")
sb, sf = (load(n, 'result.json')['by_heldout']['seen by the fine-tune'] for n in (base, ft))
say(f"   (cards the fine-tune trained on, n={sf['n']}: top-1 {sb['top1']}% -> {sf['top1']}%, top-5 {sb['top5']}% -> {sf['top5']}%)")

hb, hf = (load(n, 'hard_rescore.json') for n in (base, ft))
review_b, review_f = (100 * h['strong_plus_image']['review'] / h['counted'] for h in (hb, hf))
point3 = review_f <= review_b + 1e-9 and hf['strong_plus_image']['wrong'] == 0
say(f"3. corrected hard cases (n={hf['counted']}): review {review_b:.1f}% -> {review_f:.1f}%, wrong additions {hb['strong_plus_image']['wrong']} -> "
    f"{hf['strong_plus_image']['wrong']}; image alone right card {100 * hb['image_card_right'] / hb['counted']:.1f}% -> "
    f"{100 * hf['image_card_right'] / hf['counted']:.1f}%   point 3: {'PASS' if point3 else 'FAIL'}")

say()
say(f"VERDICT: {'PASS - the fine-tune clears the bar' if point1 and point2 and point3 else 'FAIL - not worth it by the bar written beforehand'}")
(OUT / 'ft_verdict.txt').write_text('\n'.join(lines) + '\n')
