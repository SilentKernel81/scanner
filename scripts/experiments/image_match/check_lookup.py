#!/usr/bin/env python3
"""
The SHIPPED lookup (core/printing_check.PrintingCheck.lookup, with the real data/image_index and the real OCR reader for the strip) against the
experiment's measurements, so that what runs on the server is what was measured:
  1. the 129 AI-read pictures, scored against the FROZEN corrected truth (truth/hard_truth.json): the experiment's rule A2 + image alone got
     85.2% added with 0 wrong and 14.8% to review (128 pictures) with the colour hash;
  2. a sample of OCR-confirmed pictures (what 'lookup: all' would add): how often the picture and the strip alone agree with the printing the
     OCR confirmed - the labels there are the OCR's own, so a disagreement is only a pointer.

    venv/bin/python -I scripts/experiments/image_match/check_lookup.py [sample size, default 300]     (the repository's venv: the OCR reader)
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from card_scanner.config import Config
from card_scanner.core import card_ocr
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck, read_strip
from common import OUT, TRUTH, read

sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
database = CardDatabase()
ocr = card_ocr.CardOcr()
ocr.warm_up()
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'shadow', 'all',
                      strip_reader=lambda rgb: read_strip(ocr, rgb))

rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = [r for k in ('collection', 'scanned') for r in rows if r['kind'] == k]          # the order strip.py walked them in
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())

# 1. the AI-read pictures
paths, added, wrong, against = {}, 0, 0, {}
kept = 0
for r, x in zip(ordered, items):
    if not x['hard']:
        continue
    truth = frozen[Path(r['path']).name]
    if truth is None:                                    # a third card: left out, as in the experiment
        continue
    kept += 1
    answer = check.lookup(read(r['path']), r['truth'])   # r['truth'] is the label, the AI's answer
    paths[answer['path']] = paths.get(answer['path'], 0) + 1
    against[answer['against_resolved']] = against.get(answer['against_resolved'], 0) + 1
    if answer['printing']:
        added += 1
        wrong += answer['printing']['id'] != truth
print(f"AI-read pictures, {kept} kept: added by themselves {added} ({100 * added / kept:.1f}%), wrong {wrong} (of those {100 * wrong / max(added, 1):.1f}%), "
      f"to review {kept - added} ({100 * (kept - added) / kept:.1f}%)   paths {paths}")
print(f"   against what the AI resolved: {against}      (the experiment, colour hash + rule A2 + image alone: 85.2% added, 0 wrong, 14.8% review)")

# 2. OCR-confirmed pictures
easy = [r for r, x in zip(ordered, items) if not x['hard'] and r['kind'] in ('collection', 'scanned')]
picked = [easy[i] for i in np.random.default_rng(4).choice(len(easy), min(sample, len(easy)), replace=False)]
paths, against = {}, {}
from common import printings
P = printings()
for r in picked:
    answer = check.lookup(read(r['path']), r['truth'])
    paths[answer['path']] = paths.get(answer['path'], 0) + 1
    against[answer['against_resolved']] = against.get(answer['against_resolved'], 0) + 1
    if answer['against_resolved'] in ('same_card', 'other_card'):       # a disagreement with the printing the OCR confirmed: look at it
        label, chosen = P[r['truth']], answer['printing']
        print(f"   DISAGREES {Path(r['path']).name}: label {label['name']} [{label['set']} {label['number']}], the lookup says {chosen['name']} "
              f"[{chosen['set']} {chosen['number']}] ({answer['path']}, strip {answer['strip']}, lead {answer['lead']}, similarity {chosen['similarity']})")
print(f"OCR-confirmed pictures, {len(picked)} sampled: paths {paths}; against the printing the OCR confirmed {against}")
ocr.stop()
