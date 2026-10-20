#!/usr/bin/env python3
"""
Freezes the adjudicated truth of the 129 AI-read pictures as PRINTING IDS in truth/hard_truth.json, so every model is scored against the same
fixed answers (a Codex review of 2026-10-20 found that hard_rescore.py took the truth of an 'image' verdict from the model being scored: a
wrong pick of the fine-tune would have become its own ground truth).

The verdicts (truth/hard_relabel.json) were made by looking at the contact sheets of the COLOUR-HASH baseline (phash_rgb_card), so 'image'
means that baseline's first answer, and that is the id stored:
    'image'              -> the baseline's first printing for that picture
    'label' / 'same_card' / no verdict (the label and the baseline agreed) -> the label's printing (the strip's job to settle a same-card printing)
    'neither'            -> null: left out of the scoring

    venv/bin/python -I freeze_hard_truth.py          (writes once; refuses to overwrite a frozen file unless --again is given)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, TRUTH

BASELINE = 'phash_rgb_card'          # the run whose pictures the verdicts were made on
target = TRUTH / 'hard_truth.json'
if target.exists() and '--again' not in sys.argv:
    sys.exit(f'{target} is already frozen - the point is that it does not change; --again to write it anew')

relabel = json.loads((TRUTH / 'hard_relabel.json').read_text())
rows = json.loads((OUT / BASELINE / 'rows.json').read_text())
items = json.loads((OUT / BASELINE / 'strip_items.json').read_text())
ordered = [r for k in ('collection', 'scanned') for r in rows if r['kind'] == k]       # the order strip.py walked them in
assert len(ordered) == len(items)
frozen, counts = {}, {}
for r, x in zip(ordered, items):
    if not x['hard']:
        continue
    name = Path(r['path']).name
    verdict = relabel.get(name, 'agreed')
    frozen[name] = None if verdict == 'neither' else r['got'] if verdict == 'image' else r['truth']
    counts[verdict] = counts.get(verdict, 0) + 1
target.write_text(json.dumps({'_note': 'Adjudicated printing ids of the AI-read pictures (null = a third card, left out). Frozen 2026-10-20 from the '
                                       f'verdicts in hard_relabel.json against the {BASELINE} run; never derived from the model being scored.',
                              **frozen}, indent=1))
print(f'frozen {len(frozen)} pictures: {counts}')
