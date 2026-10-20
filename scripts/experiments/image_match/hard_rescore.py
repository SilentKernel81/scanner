#!/usr/bin/env python3
"""
Re-scores the 129 AI-read pictures against CORRECTED labels (truth/hard_relabel.json: my verdicts by eye on the pictures where the
image and the label disagreed) - the labels the AI made are wrong on some of them. The strip is read again for these pictures
only (a few seconds) so the printing the rule would add is known, not just whether it was right.

    venv/bin/python -I hard_rescore.py <embedding folder name>        (the REPOSITORY's venv: it has what card_scanner needs)

Corrected truth: the FROZEN printing ids of truth/hard_truth.json (freeze_hard_truth.py) - the same fixed answers for every model scored.
(An earlier version took the truth of an 'image' verdict from the model being scored, so a wrong pick of the model under test became its
own ground truth; a Codex review of 2026-10-20 found it.) 'neither' -> left out.
Rule A2 as in noai_analysis.py: strip and image candidates agree on set + number AND on the art of the image's best match -> add
that printing; else the image alone if it leads by 0.05; else review.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from card_scanner.core import card_ocr
from common import OUT, TRUTH, printings, read
from strip import crop, parse_strip

LEAD = 0.05
name = sys.argv[1]
P = printings()
relabel = json.loads((TRUTH / 'hard_relabel.json').read_text())      # only to show which verdict a wrong addition had
if not (TRUTH / 'hard_truth.json').exists():
    sys.exit('truth/hard_truth.json is missing: run freeze_hard_truth.py once, so every model is scored against the same fixed ids')
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
rows = json.loads((OUT / name / 'rows.json').read_text())
items = json.loads((OUT / name / 'strip_items.json').read_text())
ordered = [r for k in ('collection', 'scanned') for r in rows if r['kind'] == k]
hard = [r for r, x in zip(ordered, items) if x['hard']]
ocr = card_ocr.CardOcr()
ocr.warm_up()

result = {'strong_only': [0, 0, 0], 'strong_plus_image': [0, 0, 0]}      # [added, right, review]
image_right = image_card_right = counted = 0
by_verdict = {}
for r in hard:
    verdict = relabel.get(Path(r['path']).name, 'agreed')
    truth = frozen[Path(r['path']).name]          # fixed: never this model's own answer
    if truth is None:                             # a third card
        continue
    counted += 1
    image_right += r['got'] == truth
    image_card_right += P[r['got']]['oracle_id'] == P[truth]['oracle_id']
    lines = ocr.read_lines(crop(read(r['path']))) or []
    number, set_code, _ = parse_strip(lines, 1000)
    top5 = r['top5']
    both = [c for c in top5 if number and set_code and (P[c]['number'].lstrip('0') or '0') == number and P[c]['set'] == set_code]
    strong = both[0] if both and P[both[0]]['illustration_id'] and P[both[0]]['illustration_id'] == P[top5[0]]['illustration_id'] else None
    lead = r['sims'][0] - r['sims'][1]
    for key, allow_image in (('strong_only', False), ('strong_plus_image', True)):
        pick = strong or (top5[0] if allow_image and lead >= LEAD else None)
        if pick is None:
            result[key][2] += 1
        else:
            result[key][0] += 1
            result[key][1] += pick == truth
            if pick != truth:
                by_verdict.setdefault(key, []).append((Path(r['path']).name, verdict, P[pick]['name'], P[pick]['set'], P[pick]['number']))
print(f'{counted} hard cases kept ({len(hard) - counted} left out: a third card)')
print(f'image alone: exact printing right {100 * image_right / counted:.1f}%, right CARD {100 * image_card_right / counted:.1f}%  (against the corrected labels)')
for key, (added, right, review) in result.items():
    print(f'{key}: added {100 * added / counted:.1f}%, right {100 * right / max(added, 1):.1f}% ({added - right} wrong), review {100 * review / counted:.1f}%')
(OUT / name / 'hard_rescore.json').write_text(json.dumps({
    'counted': counted, 'image_right': image_right, 'image_card_right': image_card_right,
    **{key: {'added': v[0], 'right': v[1], 'wrong': v[0] - v[1], 'review': v[2]} for key, v in result.items()}}))
for key, wrong in by_verdict.items():
    print(f'\nwrong additions, {key}:')
    for w in wrong:
        print('  ', w)
ocr.stop()
