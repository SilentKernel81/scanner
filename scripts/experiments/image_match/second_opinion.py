#!/usr/bin/env python3
"""
A cheap check on the AI's reads, with no image index: after the AI names a card, the OCR reads the TITLE of the same picture; if the two names
disagree the card goes to review. On the 129 AI-read pictures, with the labels corrected by eye (truth/hard_relabel.json): how many of the AI's
misreads would that catch, and how many correct reads would it wrongly send to review?

    venv/bin/python -I second_opinion.py        (the REPOSITORY's venv: it has the OCR reader and the project's name matching)
'AI wrong' = the verdict said 'image' or 'neither'; 'AI right' = everything else (the label agreed with the image, or 'label' / 'same_card').
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from card_scanner.core import card_ocr
from card_scanner.core.database import search_key
from common import OUT, TRUTH, printings, read

P = printings()
relabel = json.loads((TRUTH / 'hard_relabel.json').read_text())
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = [r for k in ('collection', 'scanned') for r in rows if r['kind'] == k]
hard = [r for r, x in zip(ordered, items) if x['hard']]
ocr = card_ocr.CardOcr()
ocr.warm_up()


def same_name(a, b):
    """The names are the same card's (search_key as the project matches names; the front face of a double-faced card is enough)"""
    a, b = search_key(a), search_key(b)
    return a == b or a.split('//')[0].strip() == b.split('//')[0].strip() or a in b or b in a


counts = {'wrong': [0, 0, 0], 'right': [0, 0, 0]}        # [cases, flagged (names disagree), the OCR read no name]
missed = []
for r in hard:
    verdict = relabel.get(Path(r['path']).name)
    ai_name = P[r['truth']]['name']                       # what the AI said
    group = 'wrong' if verdict in ('image', 'neither') else 'right'
    read_result = ocr.read_card(read(r['path']), 'mtg')
    counts[group][0] += 1
    if not read_result:
        counts[group][2] += 1
        continue
    if not same_name(read_result['name'], ai_name):
        counts[group][1] += 1
    elif group == 'wrong':
        missed.append((Path(r['path']).name, ai_name, read_result['name']))
for group, (n, flagged, noname) in counts.items():
    print(f"AI {group:5}: {n:>3} cases; the OCR title disagrees on {flagged} ({100 * flagged / max(n, 1):.0f}%); the OCR read no name on {noname}")
print('AI wrong and the OCR agreed with it anyway:', missed)
ocr.stop()
